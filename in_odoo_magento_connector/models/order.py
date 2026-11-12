import math
from copy import deepcopy

from odoo import api, fields, models, Command, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.queue_job.exception import RetryableJobError


def number(value):
    result = float(value or 0)
    if not math.isfinite(result):
        raise UserError(_("Magento supplied a non-finite monetary amount or quantity."))
    return result


class MagentoTaxMapping(models.Model):
    _name = "in.magento.tax.mapping"
    _description = "Magento Order Tax Mapping"
    _check_company_auto = True
    _rec_name = "name"

    name = fields.Char(required=True)
    instance_id = fields.Many2one("in.magento.instance", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    rate = fields.Float(required=True, digits=(12, 6))
    tax_ids = fields.Many2many("account.tax", check_company=True, domain="[('type_tax_use', '=', 'sale'), ('company_id', '=', company_id)]")
    _rate_unique = models.Constraint("UNIQUE(instance_id, rate)", "Map each tax rate only once per connection.")

    @api.constrains("tax_ids", "rate")
    def _check_tax(self):
        for rec in self:
            if rec.rate < 0 or rec.tax_ids.filtered(lambda t: t.amount_type != "percent" or t.price_include or t.include_base_amount or t.type_tax_use != "sale"):
                raise ValidationError(_("This release supports nonnegative, tax-exclusive, non-compounding sales percentage taxes only."))
            if abs(sum(rec.tax_ids.mapped("amount")) - rec.rate) > 0.000001:
                raise ValidationError(_("Mapped tax percentages must equal the Magento rate."))


class MagentoPartnerBinding(models.Model):
    _name = "in.magento.partner.binding"
    _description = "Magento Customer Mapping"
    _check_company_auto = True
    _rec_name = "partner_id"

    instance_id = fields.Many2one("in.magento.instance", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    external_id = fields.Char(required=True, string="Magento customer ID")
    partner_id = fields.Many2one("res.partner", required=True, check_company=True, ondelete="restrict")
    _customer_unique = models.Constraint("UNIQUE(instance_id, external_id)", "This Magento customer is already mapped.")


class MagentoOrderBinding(models.Model):
    _name = "in.magento.order.binding"
    _inherit = "in.magento.sync.mixin"
    _description = "Magento Order Inbox"
    _check_company_auto = True
    _order = "id desc"

    name = fields.Char(required=True, readonly=True)
    instance_id = fields.Many2one("in.magento.instance", required=True, ondelete="cascade", readonly=True)
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    external_id = fields.Char(required=True, readonly=True, string="Magento order ID")
    payload = fields.Json(required=True, readonly=True, copy=False, groups="in_odoo_magento_connector.group_magento_manager")
    sale_order_id = fields.Many2one("sale.order", readonly=True, check_company=True, ondelete="restrict", copy=False)
    state = fields.Selection([("pending", "Pending"), ("done", "Imported"), ("skipped", "Skipped")], default="pending", readonly=True)
    remote_status = fields.Char(readonly=True)
    imported_at = fields.Datetime(readonly=True)
    _order_unique = models.Constraint("UNIQUE(instance_id, external_id)", "This Magento order is already in the inbox.")
    _sale_unique = models.Constraint("UNIQUE(sale_order_id)", "This Odoo order is already mapped.")

    def action_retry(self):
        for rec in self:
            rec.instance_id._require_manager()
            rec.instance_id._check_enabled()
            if not rec.sale_order_id:
                rec.with_company(rec.company_id).with_delay(identity_key=f"magento:order:{rec.id}", priority=5)._job_import()
        return True

    def _taxes(self, rate):
        mapping = self.instance_id.tax_mapping_ids.filtered(lambda t: abs(t.rate - rate) < 0.000001)
        if len(mapping) != 1:
            raise UserError(_("Configure an explicit tax mapping for rate %s%%, including zero rates.", rate))
        return mapping.tax_ids

    def _address_values(self, address, kind, parent):
        country = self.env["res.country"].search([("code", "=", address.get("country_id"))], limit=1)
        if not country:
            raise UserError(_("Map a valid country for the Magento address."))
        state = self.env["res.country.state"]
        if address.get("region_code"):
            state = state.search([("country_id", "=", country.id), ("code", "=", address["region_code"])], limit=1)
        street = address.get("street") or []
        if isinstance(street, str):
            street = street.splitlines()
        return {"name": " ".join(filter(None, [address.get("firstname"), address.get("lastname")])) or parent.name,
                "parent_id": parent.id, "type": kind, "company_id": self.company_id.id,
                "street": street[0] if street else False, "street2": " ".join(street[1:]),
                "city": address.get("city"), "zip": address.get("postcode"), "country_id": country.id,
                "state_id": state.id, "phone": address.get("telephone"), "email": address.get("email")}

    def _partner(self, payload):
        customer_id = str(payload["customer_id"]) if payload.get("customer_id") else False
        Partner = self.env["res.partner"].with_company(self.company_id)
        bindings = self.env["in.magento.partner.binding"]
        binding = bindings.search([("instance_id", "=", self.instance_id.id), ("external_id", "=", customer_id)], limit=1) if customer_id else bindings
        if binding:
            return binding.partner_id
        address = payload.get("billing_address") or {}
        # Email alone is not a customer identity. Pre-existing customers may be
        # explicitly linked in the customer mappings before importing orders.
        partner = Partner.create({"name": " ".join(filter(None, [address.get("firstname"), address.get("lastname")])) or payload.get("customer_email") or _("Magento guest"),
                                  "email": payload.get("customer_email"), "company_id": self.company_id.id})
        if customer_id:
            bindings.create({"instance_id": self.instance_id.id, "external_id": customer_id, "partner_id": partner.id})
        return partner

    def _job_import(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        # Serialize customer mapping and order creation per connection. This is
        # also effective with multiple queue workers and manual retry requests.
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", [527019, self.instance_id.id])
        if not self.env.cr.fetchone()[0]:
            raise RetryableJobError("Another Magento import is running for this connection.", seconds=10)
        self.invalidate_recordset()
        if self.sale_order_id:
            return self.sale_order_id.id
        # Keep this atomic even when invoked by a test, shell or other module.
        with self.env.cr.savepoint():
            return self._import_order()

    def _processed_import_keys(self):
        return ('qty_shipped', 'qty_invoiced', 'qty_refunded', 'qty_canceled')

    def _allow_complete_virtual_import(self, payload):
        return False

    def _normalized_items(self, payload):
        items = payload.get('items', [])
        parents = {str(item.get('item_id')): item for item in items if not item.get('parent_item_id')}
        children = {}
        for item in items:
            if item.get('parent_item_id'):
                key = str(item['parent_item_id'])
                if key not in parents or parents[key].get('product_type') != 'configurable':
                    raise UserError(_('An order child has an unsupported or missing parent.'))
                children.setdefault(key, []).append(item)
        result = []
        for item in items:
            if any(number(item.get(key)) for key in self._processed_import_keys()):
                raise UserError(_('Previously processed order lines require a migration workflow.'))
            if item.get('parent_item_id'):
                continue
            if item.get('product_type') != 'configurable':
                result.append(item)
                continue
            variants = children.get(str(item.get('item_id')), [])
            if len(variants) != 1 or variants[0].get('product_type') != 'simple':
                raise UserError(_('A configurable order line must have exactly one simple child.'))
            child = variants[0]
            if number(child.get('qty_ordered')) != number(item.get('qty_ordered')):
                raise UserError(_('Configurable parent and child quantities differ.'))
            binding = self.env['in.magento.product.binding'].search([
                ('instance_id', '=', self.instance_id.id), ('sku', '=', child.get('sku'))], limit=1)
            if not binding or not binding.template_binding_id or str(child.get('product_id')) != binding.external_id or str(item.get('product_id')) != binding.template_binding_id.external_id:
                raise UserError(_('Map the configurable parent and its exact child variant before importing this order.'))
            # Magento carries commercial totals on the parent. Use the child
            # solely for variant identity; do not charge both lines.
            normalized = deepcopy(item)
            normalized.update(sku=child['sku'], name=child.get('name') or item.get('name'), product_type='simple')
            result.append(normalized)
        return result

    def _import_order(self):
        payload = self.payload
        if not isinstance(payload, dict) or str(payload.get("entity_id")) != self.external_id:
            raise UserError(_("The inbox payload does not match its Magento order ID."))
        allowed = [int(v.strip()) for v in (self.instance_id.store_ids or "").split(",") if v.strip()]
        if int(payload.get("store_id", -1)) not in allowed:
            raise UserError(_("The order belongs to an unconfigured Magento store."))
        if payload.get("state") in ("canceled", "closed"):
            if self.state != 'skipped':
                self.env['in.magento.sync.log']._record(self, direction='import', operation='skip',
                    status='skipped', summary='Canceled or closed Magento order skipped.')
            self.write({"state": "skipped", "remote_status": payload.get("status")})
            return False
        if payload.get("state") not in ("new", "processing", "pending_payment") and not self._allow_complete_virtual_import(payload):
            raise UserError(_("This release imports new/unfulfilled orders only; this order needs review."))
        if payload.get("order_currency_code") != self.instance_id.pricelist_id.currency_id.name:
            raise UserError(_("Order currency differs from the connection pricelist. No order was created."))
        items = self._normalized_items(payload)
        lines = []
        tax_total = number(payload.get("tax_amount"))
        net_total = number(payload.get("grand_total")) - tax_total
        for item in items:
            if item.get("parent_item_id") or item.get("product_type") not in ("simple", "virtual"):
                raise UserError(_("Only simple and virtual order lines are supported in this first release."))
            if any(number(item.get(k)) for k in self._processed_import_keys()):
                raise UserError(_("Previously processed order lines require a migration workflow."))
            qty, price = number(item.get("qty_ordered")), number(item.get("price"))
            discount = number(item.get("discount_amount"))
            if qty <= 0 or price < 0 or discount < 0 or discount > qty * price:
                raise UserError(_("Unsupported quantity, price or discount in Magento order."))
            if number(item.get("discount_tax_compensation_amount")):
                raise UserError(_("Tax-inclusive discount compensation requires a dedicated mapping workflow."))
            binding = self.env["in.magento.product.binding"].search([
                ("instance_id", "=", self.instance_id.id), ("sku", "=", item.get("sku")),
            ], limit=1)
            if not binding:
                raise UserError(_("Map Magento SKU %s to an Odoo product before retrying.", item.get("sku")))
            taxes = self._taxes(number(item.get("tax_percent")))
            lines.append(Command.create({"product_id": binding.product_id.id, "name": item.get("name") or binding.product_id.name,
                                         "in_magento_item_id": str(item['item_id']), "in_magento_sku": item.get('sku'),
                                         "product_uom_qty": qty, "product_uom_id": binding.product_id.uom_id.id,
                                         "price_unit": price, "discount": 100 * discount / (qty * price) if price else 0,
                                         "tax_ids": [Command.set(taxes.ids)]}))
        if not lines:
            raise UserError(_("The Magento order has no supported lines."))
        shipping = number(payload.get("shipping_amount")) - number(payload.get("shipping_discount_amount"))
        shipping_tax = number(payload.get("shipping_tax_amount"))
        if shipping < 0 or (not shipping and shipping_tax):
            raise UserError(_("Unsupported shipping tax or discount."))
        if shipping:
            product = self.instance_id.shipping_product_id
            if not product or product.type != "service":
                raise UserError(_("Configure a shipping service product before importing freight."))
            taxes = self._taxes(round(100 * shipping_tax / shipping, 6))
            lines.append(Command.create({"product_id": product.id, "name": payload.get("shipping_description") or product.name,
                                         "product_uom_qty": 1, "price_unit": shipping, "tax_ids": [Command.set(taxes.ids)]}))
        partner = self._partner(payload)
        invoice = self.env["res.partner"].create(self._address_values(payload.get("billing_address") or {}, "invoice", partner))
        assignments = payload.get("extension_attributes", {}).get("shipping_assignments", [])
        addresses = [a.get("shipping", {}).get("address") for a in assignments if a.get("shipping", {}).get("address")]
        if len(addresses) > 1:
            raise UserError(_("Multiple shipping addresses are not yet supported."))
        if not addresses and any(item.get("product_type") != "virtual" for item in items):
            raise UserError(_("A shipping address is required for physical products."))
        delivery = self.env["res.partner"].create(self._address_values(addresses[0], "delivery", partner)) if addresses else invoice
        order = self.env["sale.order"].with_company(self.company_id).create({
            "partner_id": partner.id, "partner_invoice_id": invoice.id, "partner_shipping_id": delivery.id,
            "company_id": self.company_id.id, "warehouse_id": self.instance_id.warehouse_id.id,
            "pricelist_id": self.instance_id.pricelist_id.id, "client_order_ref": self.name,
            "origin": f"Magento {self.instance_id.name}: {self.name}", "date_order": payload.get("created_at") or fields.Datetime.now(),
            "order_line": lines, "in_magento_payment_method": (payload.get("payment") or {}).get("method"),
            "in_magento_external_id": self.external_id,
        })
        currency = order.currency_id
        if (currency.compare_amounts(order.amount_total, number(payload.get("grand_total")))
                or currency.compare_amounts(order.amount_tax, tax_total)
                or currency.compare_amounts(order.amount_untaxed, net_total)):
            raise UserError(_("Magento and Odoo order totals differ. Check taxes, discounts and currency; no order was saved."))
        self.write({"sale_order_id": order.id, "state": "done", "remote_status": payload.get("status"), "imported_at": fields.Datetime.now()})
        if self.instance_id.auto_confirm_orders:
            if payload.get("state") != "processing" and not self._allow_complete_virtual_import(payload):
                raise UserError(_("Automatic confirmation requires a processing order. Review the payment state first."))
            order.action_confirm()
        self.env['in.magento.sync.log']._record(self, direction='import', operation='import',
            summary=f"Created Odoo quotation {order.name}.", previous_source='new',
            after={'order': order.name, 'currency': currency.name, 'total': order.amount_total,
                'tax': order.amount_tax, 'shipping': shipping, 'state': order.state,
                'lines': [{'sku': item.get('sku'), 'quantity': number(item.get('qty_ordered')),
                           'unit_price': number(item.get('price')), 'discount_amount': number(item.get('discount_amount'))}
                          for item in items]})
        return order.id


class SaleOrder(models.Model):
    _inherit = "sale.order"

    in_magento_external_id = fields.Char(readonly=True, copy=False)
    in_magento_payment_method = fields.Char(readonly=True, copy=False)
