from contextlib import contextmanager
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.client import MagentoClient, MagentoError, MagentoRetryableError, search_params, validate_url


class MagentoInstance(models.Model):
    _name = "in.magento.instance"
    _inherit = "in.magento.sync.mixin"
    _description = "Magento Connection"
    _check_company_auto = True

    sync_log_ids = fields.One2many("in.magento.sync.log", "instance_id")
    sync_log_count = fields.Integer(compute="_compute_sync_log_count")
    sync_log_retention_days = fields.Integer(default=90, string="Keep sync logs (days)",
        help="Delete older logs automatically. Use 0 to retain logs indefinitely.")

    @api.depends("sync_log_ids")
    def _compute_sync_log_count(self):
        counts = dict(self.env['in.magento.sync.log']._read_group(
            [('instance_id', 'in', self.ids)], ['instance_id'], ['__count']))
        for record in self:
            record.sync_log_count = counts.get(record, 0)

    @api.constrains('sync_log_retention_days')
    def _check_log_retention(self):
        if any(record.sync_log_retention_days < 0 for record in self):
            raise ValidationError(_("Log retention days cannot be negative."))

    def action_sync_logs(self):
        self.ensure_one()
        self.check_access('read')
        action = self.env['ir.actions.actions']._for_xml_id('in_odoo_magento_connector.action_sync_logs')
        action['domain'] = [('instance_id', '=', self.id)]
        return action

    product_count = fields.Integer(compute='_compute_related_counts', string='Products')
    variant_count = fields.Integer(compute='_compute_related_counts', string='Variants')
    attribute_count = fields.Integer(compute='_compute_related_counts', string='Attributes')
    category_count = fields.Integer(compute='_compute_related_counts', string='Categories')
    sale_order_count = fields.Integer(compute='_compute_related_counts', string='Sale Orders')

    @api.depends('product_ids.product_id', 'template_ids.template_id', 'attribute_mapping_ids.attribute_id',
                 'category_ids.category_id', 'order_ids.sale_order_id')
    @api.depends_context('uid', 'allowed_company_ids')
    def _compute_related_counts(self):
        for record in self:
            for field, model, ids in (
                ('product_count', 'product.template', record._related_templates().ids),
                ('variant_count', 'product.product', record.product_ids.product_id.ids),
                ('attribute_count', 'product.attribute', record.attribute_mapping_ids.attribute_id.ids),
                ('category_count', 'product.category', record.category_ids.category_id.ids),
                ('sale_order_count', 'sale.order', record.order_ids.sale_order_id.ids),
            ):
                target = self.env[model].with_context(active_test=False)
                record[field] = target.search_count([('id', 'in', ids)]) if target.has_access('read') else 0

    def _related_records_action(self, action_xmlid, ids, name):
        self.ensure_one()
        self.check_access('read')
        action = self.env['ir.actions.actions']._for_xml_id(action_xmlid)
        self.env[action['res_model']].check_access('read')
        action.update(name=name, domain=[('id', 'in', ids)],
                      context={'active_test': False, 'create': False})
        # Clear action search defaults: imported quotations must be visible too.
        return action

    def _related_templates(self):
        self.ensure_one()
        return self.product_ids.product_id.product_tmpl_id | self.template_ids.template_id

    def action_related_products(self):
        return self._related_records_action('product.product_template_action_all',
            self._related_templates().ids, _('Products'))

    def action_related_variants(self):
        return self._related_records_action('product.product_normal_action',
            self.product_ids.product_id.ids, _('Product Variants'))

    def action_related_attributes(self):
        return self._related_records_action('product.attribute_action',
            self.attribute_mapping_ids.attribute_id.ids, _('Attributes'))

    def action_related_categories(self):
        return self._related_records_action('product.product_category_action_form',
            self.category_ids.category_id.ids, _('Categories'))

    def action_related_sale_orders(self):
        return self._related_records_action('sale.action_orders',
            self.order_ids.sale_order_id.ids, _('Sale Orders'))

    name = fields.Char(required=True)
    active = fields.Boolean(default=True)
    company_id = fields.Many2one("res.company", required=True, default=lambda s: s.env.company)
    base_url = fields.Char(required=True)
    enabled = fields.Boolean(string="Synchronization enabled", default=False)
    auth_type = fields.Selection([("oauth1", "OAuth 1.0a"), ("bearer", "Bearer token")], default="oauth1", required=True)
    consumer_key = fields.Char(groups="in_odoo_magento_connector.group_magento_manager", copy=False)
    consumer_secret = fields.Char(groups="in_odoo_magento_connector.group_magento_manager", copy=False)
    access_token = fields.Char(groups="in_odoo_magento_connector.group_magento_manager", copy=False)
    token_secret = fields.Char(groups="in_odoo_magento_connector.group_magento_manager", copy=False)
    store_code = fields.Char(default="all", required=True)
    store_ids = fields.Char(string="Import store IDs", help="Required comma-separated Magento store-view IDs, e.g. 1,2.")
    warehouse_id = fields.Many2one("stock.warehouse", required=True, check_company=True)
    pricelist_id = fields.Many2one("product.pricelist", required=True, check_company=True)
    shipping_product_id = fields.Many2one("product.product", check_company=True, domain="[('type', '=', 'service')]")
    attribute_set_id = fields.Integer(default=4, required=True)
    root_category_id = fields.Integer(default=2, required=True)
    publish_enabled = fields.Boolean(string="Publish new products as enabled", default=False)
    template_ids = fields.One2many('in.magento.template.binding', 'instance_id')
    attribute_mapping_ids = fields.One2many('in.magento.attribute.mapping', 'instance_id')
    inventory_source_ids = fields.One2many('in.magento.inventory.source', 'instance_id')
    auto_export_inventory = fields.Boolean(default=False)
    inventory_interval = fields.Integer(default=15, string='Inventory interval (minutes)')
    last_inventory_schedule = fields.Datetime(readonly=True, copy=False)
    auto_export_products = fields.Boolean(default=False)
    auto_import_orders = fields.Boolean(default=False)
    auto_confirm_orders = fields.Boolean(default=False)
    import_from = fields.Datetime(default=fields.Datetime.now, required=True)
    order_cursor = fields.Datetime(readonly=True, copy=False)
    order_batch_size = fields.Integer(default=50)
    last_order_poll = fields.Datetime(readonly=True, copy=False)
    order_interval = fields.Integer(default=5, string="Order interval (minutes)")
    connection_checked_at = fields.Datetime(readonly=True, copy=False)
    category_ids = fields.One2many("in.magento.category.binding", "instance_id")
    product_ids = fields.One2many("in.magento.product.binding", "instance_id")
    tax_mapping_ids = fields.One2many("in.magento.tax.mapping", "instance_id")
    order_ids = fields.One2many("in.magento.order.binding", "instance_id")

    def write(self, vals):
        for rec in self:
            changed = any(key in vals and vals[key] != (rec[key].id if key == "company_id" else rec[key])
                          for key in ("base_url", "company_id", "store_code"))
            if changed and (rec.product_ids or rec.category_ids or rec.order_ids or rec.template_ids or rec.inventory_source_ids or rec.attribute_mapping_ids):
                raise UserError(_("A mapped connection cannot change store URL, scope or company. Create a separate connection."))
        return super().write(vals)

    @api.constrains("base_url", "order_batch_size", "order_interval", "store_ids", "attribute_set_id", "root_category_id", "inventory_interval")
    def _check_configuration(self):
        for rec in self:
            try:
                validate_url(rec.base_url)
                if rec.store_ids:
                    if any(int(part.strip()) < 0 for part in rec.store_ids.split(",")):
                        raise ValueError("Store IDs must be nonnegative integers.")
            except ValueError as error:
                raise ValidationError(str(error)) from error
            if not 1 <= rec.order_batch_size <= 200 or rec.order_interval < 1:
                raise ValidationError(_("Batch size must be 1–200 and the interval at least one minute."))
            if rec.inventory_interval < 1:
                raise ValidationError(_("Inventory interval must be at least one minute."))
            if rec.attribute_set_id < 1 or rec.root_category_id < 1:
                raise ValidationError(_("Magento attribute set and root category IDs must be positive."))

    def _require_manager(self):
        if not self.env.su and not self.env.user.has_group("in_odoo_magento_connector.group_magento_manager"):
            raise AccessError(_("Only Magento connector managers may run synchronization."))
        self.check_access("write")

    def _check_enabled(self):
        self.ensure_one()
        if not self.active or not self.enabled:
            raise UserError(_("Synchronization is disabled for this connection."))
        if str(self.env["ir.config_parameter"].sudo().get_param("database.is_neutralized", "false")).lower() in ("true", "1"):
            raise UserError(_("Synchronization is blocked on a neutralized database."))

    @contextmanager
    def _client(self, *, store_code=None):
        self.ensure_one()
        self._require_manager()
        if not self.access_token or (self.auth_type == "oauth1" and not all((self.consumer_key, self.consumer_secret, self.token_secret))):
            raise UserError(_("Configure the Magento API credentials first."))
        try:
            with MagentoClient(self.base_url, store_code if store_code is not None else self.store_code, self.auth_type, self.access_token,
                               self.consumer_key or "", self.consumer_secret or "", self.token_secret or "") as client:
                yield client
        except MagentoRetryableError as error:
            raise RetryableJobError(str(error), seconds=error.seconds) from error
        except MagentoError as error:
            raise UserError(str(error)) from error

    def action_test_connection(self):
        self.ensure_one()
        with self._client() as client:
            stores = client.request("GET", "store/storeViews")
            if not isinstance(stores, list):
                raise UserError(_("Unexpected response from Magento store configuration."))
        self.connection_checked_at = fields.Datetime.now()
        return {"type": "ir.actions.client", "tag": "display_notification", "params": {
            "title": _("Connection successful"), "message": _("Found %s store views. Configure import store IDs before importing orders.", len(stores)),
            "type": "success", "sticky": False}}

    def action_import_orders(self):
        self._require_manager()
        for rec in self:
            rec._check_enabled()
            if not rec.store_ids:
                raise UserError(_("Configure the Magento store IDs to import."))
            rec.with_company(rec.company_id).with_delay(
                identity_key=f"magento:{rec.id}:orders", description=f"{rec.name}: fetch Magento orders", priority=5,
            )._job_poll_orders()
        return True

    def action_export_products(self):
        self._require_manager()
        self.product_ids.filtered(lambda row: row._publication_allowed()).action_export()
        return True

    def action_export_categories(self):
        self._require_manager()
        self.category_ids.action_export()
        return True

    def _job_poll_orders(self):
        """One bounded page per transaction; persist inbox before moving the cursor.

        Use entity-ID keyset pagination within a fixed date window. On failure,
        the inbox, next job and cursor all roll back together. A two-minute
        overlap permits safe repeat fetching; a full replay can use import_from.
        """
        self.ensure_one()
        self._check_enabled()
        start = max(self.import_from, (self.order_cursor or self.import_from) - timedelta(minutes=2))
        self._job_poll_page(fields.Datetime.to_string(start), fields.Datetime.to_string(fields.Datetime.now()), 0)

    def _job_poll_page(self, start, end, after_id):
        self.ensure_one()
        self._check_enabled()
        self.env.cr.execute("SELECT pg_try_advisory_xact_lock(%s, %s)", [527019, self.id])
        if not self.env.cr.fetchone()[0]:
            raise RetryableJobError("Another Magento import is running for this connection.", seconds=10)
        if not self.store_ids:
            raise UserError(_("Configure import store IDs first."))
        filters = [("created_at", "gteq", start), ("created_at", "lteq", end),
                   ("entity_id", "gt", after_id), ("store_id", "in", self.store_ids.replace(" ", ""))]
        with self._client() as client:
            payload = client.request("GET", "orders", params=search_params(filters, size=self.order_batch_size))
        if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
            raise UserError(_("Magento returned an invalid order collection."))
        items = payload["items"]
        ids = [int(item["entity_id"]) for item in items]
        if ids and (ids != sorted(set(ids)) or ids[0] <= after_id):
            raise UserError(_("Magento order pagination did not advance. No checkpoint was saved."))
        Inbox = self.env["in.magento.order.binding"].with_company(self.company_id)
        new_count = 0
        for item in items:
            if int(item["store_id"]) not in [int(v) for v in self.store_ids.split(",")]:
                raise UserError(_("Magento returned an order outside the configured store scope."))
            binding = Inbox.search([("instance_id", "=", self.id), ("external_id", "=", str(item["entity_id"]))], limit=1)
            if not binding:
                new_count += 1
                binding = Inbox.create({"instance_id": self.id, "external_id": str(item["entity_id"]),
                                        "name": item.get("increment_id") or str(item["entity_id"]), "payload": item})
            if not binding.sale_order_id:
                binding.with_delay(identity_key=f"magento:order:{binding.id}", priority=5)._job_import()
        if len(items) == self.order_batch_size:
            self.with_delay(description=f"{self.name}: fetch next Magento order page", priority=5)._job_poll_page(start, end, ids[-1])
        else:
            self.order_cursor = max(self.order_cursor or fields.Datetime.to_datetime(end), fields.Datetime.to_datetime(end))
        self.last_order_poll = fields.Datetime.now()
        if new_count:
            self.env['in.magento.sync.log']._record(self, direction='import', operation='poll',
                summary=f"Fetched {new_count} new order(s) into the inbox.")

    @api.model
    def _cron_schedule(self):
        if str(self.env["ir.config_parameter"].sudo().get_param("database.is_neutralized", "false")).lower() in ("true", "1"):
            return
        self = self.with_context(magento_sync_trigger="automatic")
        now = fields.Datetime.now()
        for rec in self.search([("enabled", "=", True), ("auto_import_orders", "=", True)]):
            if rec.store_ids and (not rec.last_order_poll or rec.last_order_poll <= now - timedelta(minutes=rec.order_interval)):
                rec.action_import_orders()
        products = self.env["in.magento.product.binding"].search([
            ("instance_id.active", "=", True),
            ("instance_id.enabled", "=", True), ("instance_id.auto_export_products", "=", True), ("external_id", "!=", False),
        ])
        self._schedule_catalog_exports(products, 'product')

        templates = self.env['in.magento.template.binding'].search([
            ('instance_id.active', '=', True), ('instance_id.enabled', '=', True),
            ('instance_id.auto_export_products', '=', True), ('external_id', '!=', False)])
        self._schedule_catalog_exports(templates, 'template')
        for instance in self.search([('enabled', '=', True), ('auto_export_inventory', '=', True)]):
            if not instance.last_inventory_schedule or instance.last_inventory_schedule <= now - timedelta(minutes=instance.inventory_interval):
                instance.action_export_inventory()
                instance.last_inventory_schedule = now

    @api.model
    def _schedule_catalog_exports(self, bindings, kind):
        """Compare the actual outgoing data before persisting a queue job.

        A write-date filter misses pricelist, category, attribute and image
        dependencies. Keep the worker's fingerprint check too: data can change
        again while a job waits. Company grouping preserves ORM prefetching.
        Invalid mappings still get a job so their errors remain visible without
        preventing valid products from being scheduled.
        """
        for company, records in bindings.grouped('company_id').items():
            prices = self.env['in.magento.product.binding']
            for record in records.with_company(company):
                if kind in ('product', 'template') and not record._publication_allowed():
                    continue
                try:
                    if kind == 'product':
                        change = record._catalog_change_kind()
                    else:
                        change = 'full' if record._fingerprint() != record.last_fingerprint else False
                except UserError:
                    change = 'full'
                if change == 'price':
                    prices |= record
                elif change:
                    record.with_delay(identity_key=f'magento:{kind}:{record.id}')._job_export_if_changed()
            prices._queue_price_batches()

    def action_export_inventory(self):
        self._require_manager()
        for record in self:
            record._check_enabled()
            record.product_ids.filtered(lambda row: row._publication_allowed() and row.sync_inventory and row.external_id and row.product_id.is_storable).action_export_inventory()
        return True

    def action_jobs(self):
        self.ensure_one()
        return {"type": "ir.actions.act_window", "name": _("Magento Jobs"), "res_model": "queue.job",
                "view_mode": "list,form", "domain": [("channel", "=", "root.magento"), ("company_id", "=", self.company_id.id)]}
