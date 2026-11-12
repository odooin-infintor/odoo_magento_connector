import hashlib
import json
from urllib.parse import quote

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.client import MagentoError
from ..services.media import export_image, image_changed


class MagentoCategoryBinding(models.Model):
    _name = "in.magento.category.binding"
    _inherit = "in.magento.sync.mixin"
    _description = "Magento Category Mapping"
    _check_company_auto = True
    _rec_name = "category_id"

    instance_id = fields.Many2one("in.magento.instance", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    category_id = fields.Many2one("product.category", required=True, ondelete="restrict")
    parent_id = fields.Many2one("in.magento.category.binding", string="Storefront parent", ondelete="restrict",
                                domain="[('instance_id', '=', instance_id)]")
    external_id = fields.Char(string="Magento category ID", copy=False)
    last_sync = fields.Datetime(readonly=True, copy=False)
    last_fingerprint = fields.Char(readonly=True, copy=False)
    _category_unique = models.Constraint("UNIQUE(instance_id, category_id)", "This category is already mapped.")
    _remote_unique = models.Constraint("UNIQUE(instance_id, external_id)", "This Magento category is already mapped.")

    @api.constrains("parent_id", "instance_id", "external_id")
    def _check_mapping(self):
        if self._has_cycle("parent_id"):
            raise ValidationError(_("Category mappings cannot contain a cycle."))
        for rec in self:
            if rec.parent_id and rec.parent_id.instance_id != rec.instance_id:
                raise ValidationError(_("The parent category must belong to the same connection."))
            if rec.external_id and (not rec.external_id.isdigit() or int(rec.external_id) < 1):
                raise ValidationError(_("Magento category IDs must be positive integers."))

    def action_export(self):
        for rec in self:
            rec.instance_id._require_manager()
            rec.instance_id._check_enabled()
            if rec.parent_id and not rec.parent_id.external_id:
                rec.parent_id.action_export()
            rec.with_company(rec.company_id).with_delay(identity_key=f"magento:category:{rec.id}")._job_export()
        return True

    def _fingerprint(self):
        self.ensure_one()
        return hashlib.sha256(json.dumps([self.category_id.with_context(lang=None).name,
            self.parent_id.external_id or self.instance_id.root_category_id], sort_keys=True).encode()).hexdigest()

    def _job_export_if_changed(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.try_lock_for_update():
            raise RetryableJobError("Another category export is running.", seconds=10)
        self.invalidate_recordset()
        if self.external_id and self.last_fingerprint != self._fingerprint():
            self._job_export()

    def _job_export(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        # Row locks also detect stale REPEATABLE READ snapshots. An advisory
        # lock alone could allow a second remote create after another commit.
        if not self.try_lock_for_update():
            raise RetryableJobError("Another export is running for this category.", seconds=10)
        self.invalidate_recordset()
        # Parent and child use separate transactions so a child failure cannot
        # roll back the parent's binding after the parent was created remotely.
        if self.parent_id and not self.parent_id.external_id:
            raise RetryableJobError("Waiting for the parent category to be published.", seconds=30)
        parent_id = int(self.parent_id.external_id) if self.parent_id else self.instance_id.root_category_id
        payload = {"category": {"name": self.category_id.with_context(lang=None).name, "parent_id": parent_id,
                                "is_active": True, "include_in_menu": True}}
        previous = None
        updating = bool(self.external_id)
        with self.instance_id._client() as client:
            if self.external_id:
                previous = client.request("GET", f"categories/{self.external_id}")
                if str(previous.get('id')) != self.external_id:
                    raise UserError(_("The Magento category identity changed. Reconcile before retrying."))
                if previous.get('parent_id') != parent_id:
                    raise UserError(_("Move the category in Magento to its mapped parent before exporting."))
                payload["category"]["id"] = int(self.external_id)
                result = client.request("PUT", f"categories/{self.external_id}", payload=payload)
            else:
                # A failed/ambiguous create must be reconciled manually by ID.
                result = client.request("POST", "categories", payload=payload)
        if not isinstance(result, dict) or not result.get("id"):
            raise UserError(_("Magento did not return a category ID. Reconcile before retrying."))
        self.write({"external_id": str(result["id"]), "last_sync": fields.Datetime.now(), "last_fingerprint": self._fingerprint()})
        values = {key: value for key, value in payload['category'].items() if key != 'id'}
        self.env['in.magento.sync.log']._record(self, direction='export',
            operation='update' if updating else 'create', summary='Category accepted by Magento.',
            before={key: previous[key] for key in values if key in previous} if previous else None,
            after=values, previous_source='magento' if updating else 'new')


class MagentoProductBinding(models.Model):
    _name = "in.magento.product.binding"
    _inherit = "in.magento.sync.mixin"
    _description = "Magento Product Mapping"
    _check_company_auto = True
    _rec_name = "sku"

    instance_id = fields.Many2one("in.magento.instance", required=True, ondelete="cascade")
    company_id = fields.Many2one(related="instance_id.company_id", store=True)
    product_id = fields.Many2one("product.product", required=True, ondelete="restrict", check_company=True)
    sku = fields.Char(required=True, help="Stable Magento SKU. May differ from Odoo's internal reference.")
    external_id = fields.Char(string="Magento product ID", readonly=True, copy=False)
    category_ids = fields.Many2many("in.magento.category.binding", domain="[('instance_id', '=', instance_id)]")
    tax_class_id = fields.Integer(default=0)
    sync_image = fields.Boolean(default=False)
    last_image_fingerprint = fields.Char(readonly=True, copy=False)
    last_sync = fields.Datetime(readonly=True, copy=False)
    last_fingerprint = fields.Char(readonly=True, copy=False)
    _product_unique = models.Constraint("UNIQUE(instance_id, product_id)", "This Odoo variant is already mapped.")
    _sku_unique = models.Constraint("UNIQUE(instance_id, sku)", "This Magento SKU is already mapped.")
    _remote_unique = models.Constraint("UNIQUE(instance_id, external_id)", "This Magento product ID is already mapped.")

    @api.onchange("product_id")
    def _onchange_product(self):
        if self.product_id and not self.sku:
            self.sku = self.product_id.default_code

    @api.constrains("sku", "category_ids", "instance_id", "tax_class_id")
    def _check_mapping(self):
        for rec in self:
            if not rec.sku or rec.sku != rec.sku.strip():
                raise ValidationError(_("A nonempty SKU without leading or trailing whitespace is required."))
            if rec.category_ids.filtered(lambda c: c.instance_id != rec.instance_id):
                raise ValidationError(_("Product categories must belong to the same Magento connection."))
            if rec.tax_class_id < 0:
                raise ValidationError(_("Tax class IDs cannot be negative."))

    def write(self, vals):
        if any(rec.external_id for rec in self) and any(key in vals for key in ("sku", "product_id", "instance_id")):
            raise UserError(_("Published mappings cannot change SKU, product or connection. Create a new mapping instead."))
        return super().write(vals)

    def _payload(self):
        self.ensure_one()
        product = self.product_id.with_company(self.company_id)
        if product.product_tmpl_id.product_variant_count > 1 and not self.template_binding_id:
            raise UserError(_("Publish this product through a configurable parent mapping first."))
        if product.type not in ("consu", "service"):
            raise UserError(_("This product type is not supported for publishing."))
        if self.category_ids.filtered(lambda c: not c.external_id):
            raise UserError(_("Publish the mapped categories before exporting the product."))
        price = self.instance_id.pricelist_id._get_product_price(product, 1.0)
        attributes = [{"attribute_code": "description", "value": product.description_sale or ""},
                      {"attribute_code": "tax_class_id", "value": str(self.tax_class_id)},
                      {"attribute_code": "category_ids", "value": self.category_ids.mapped("external_id")}]
        values = {"sku": self.sku, "name": product.name, "price": price,
                  "attribute_set_id": self.instance_id.attribute_set_id,
                  "type_id": "virtual" if product.type == "service" else "simple",
                  "visibility": 4, "weight": product.weight, "custom_attributes": attributes}
        # New listings start disabled unless explicitly configured otherwise.
        # Existing listing status is changed only when the Odoo product is inactive.
        if not self.external_id:
            values["status"] = 1 if self.instance_id.publish_enabled and product.active and product.sale_ok else 2
        elif not product.active or not product.sale_ok:
            values["status"] = 2
        return {"product": values, "saveOptions": True}

    def _fingerprint(self):
        self.ensure_one()
        return hashlib.sha256(json.dumps(self._fingerprint_data(), sort_keys=True).encode()).hexdigest()

    def _fingerprint_data(self):
        self.ensure_one()
        data = self._payload()
        data["image"] = self.product_id.image_1920.decode() if self.sync_image and self.product_id.image_1920 else ""
        return data

    def action_link(self):
        """Explicitly adopt an existing SKU; linking never publishes or overwrites."""
        for rec in self:
            rec.instance_id._require_manager()
            with rec.instance_id._client() as client:
                remote = client.request("GET", f"products/{quote(rec.sku, safe='')}")
            expected = "virtual" if rec.product_id.type == "service" else "simple"
            if remote.get("type_id") != expected or remote.get("sku") != rec.sku or not remote.get("id"):
                raise UserError(_("The remote product type or SKU does not match this mapping."))
            rec.external_id = str(remote["id"])
        return True

    def action_export(self):
        for rec in self:
            rec.instance_id._require_manager()
            rec.instance_id._check_enabled()
            rec.with_company(rec.company_id).with_delay(identity_key=f"magento:product:{rec.id}")._job_export()
        return True

    def _job_export(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.try_lock_for_update():
            raise RetryableJobError("Another export is running for this product.", seconds=10)
        self.invalidate_recordset()
        payload = self._payload()
        path = f"products/{quote(self.sku, safe='')}"
        with self.instance_id._client() as client:
            try:
                remote = client.request("GET", path)
            except MagentoError as error:
                if error.status != 404:
                    raise
                remote = None
            if remote and not self.external_id:
                raise UserError(_("SKU already exists in Magento. Use Link Existing first; nothing was overwritten."))
            if self.external_id and (not remote or str(remote.get("id")) != self.external_id):
                raise UserError(_("The remote product mapping changed. Reconcile it before exporting."))
            if remote and remote.get("type_id") != payload["product"]["type_id"]:
                raise UserError(_("Remote product type differs. Export was stopped."))
            if remote:
                payload["product"]["id"] = int(self.external_id)
            previous_values = self._log_product_values(remote) if remote else None
            result = client.request("PUT" if remote else "POST", path if remote else "products", payload=payload)
            if not isinstance(result, dict) or not result.get("id"):
                raise UserError(_("Magento did not return a product ID. Link the remote product before retrying."))
            self.external_id = str(result["id"])
            # Image work is a separate job: a failed media request cannot roll
            # back the newly persisted product mapping.
            if self.sync_image and self.product_id.image_1920 and image_changed(self, self.product_id.image_1920):
                self.with_delay(identity_key=f"magento:image:{self.id}")._job_export_image()
        self.write({"last_sync": fields.Datetime.now(), "last_fingerprint": self._fingerprint()})
        self.env['in.magento.sync.log']._record(self, direction='export',
            operation='update' if remote else 'create', summary='Product accepted by Magento.',
            before=previous_values,
            after=self._log_product_values(payload['product']),
            previous_source='magento' if remote else 'new')

    @staticmethod
    def _log_product_values(product):
        # Explicit allowlist: never log extension data or raw response bodies.
        values = {key: product[key] for key in ('sku', 'name', 'price', 'weight', 'status',
            'visibility', 'type_id', 'attribute_set_id') if key in product}
        for attribute in product.get('custom_attributes', []):
            key = attribute.get('attribute_code')
            if key in ('description', 'tax_class_id', 'category_ids'):
                value = attribute.get('value')
                if key == 'tax_class_id':
                    value = str(value)
                elif key == 'category_ids' and isinstance(value, list):
                    value = sorted(str(item) for item in value)
                values[key] = value
        return values

    def _job_export_if_changed(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if self.external_id and self._fingerprint() != self.last_fingerprint:
            self._job_export()

    def _job_export_image(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.try_lock_for_update():
            raise RetryableJobError("Another media export is running for this product.", seconds=10)
        self.invalidate_recordset()
        if not self.sync_image or not self.product_id.image_1920 or not self.external_id:
            return
        export_image(self, self.product_id.image_1920.decode(), self.product_id.name)
