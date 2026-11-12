"""Guided configuration. No products/orders are published by this wizard."""
from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError
from ..services.client import search_params, MagentoError
from odoo.addons.queue_job.exception import RetryableJobError


class MagentoSetupOption(models.TransientModel):
    _name = 'in.magento.setup.option'
    _description = 'Discovered Magento Setup Choice'

    wizard_id = fields.Many2one('in.magento.setup', required=True, ondelete='cascade')
    name = fields.Char(required=True)
    kind = fields.Selection([('store', 'Store view'), ('all', 'All store views'),
        ('attribute', 'Attribute set'), ('root', 'Root category'), ('source', 'Inventory source')], required=True)
    remote_id = fields.Integer()
    code = fields.Char()
    website_id = fields.Integer()


class MagentoSetup(models.TransientModel):
    _name = 'in.magento.setup'
    _description = 'Guided Magento Setup'

    stage = fields.Selection([('connection', 'Connection'), ('scope', 'Magento scope'),
        ('mappings', 'Mappings'), ('automation', 'Automation'), ('review', 'Review')], default='connection', required=True)
    instance_id = fields.Many2one('in.magento.instance', readonly=True)
    name = fields.Char(required=True, default='Magento store')
    company_id = fields.Many2one('res.company', required=True, default=lambda s: s.env.company)
    base_url = fields.Char(required=True)
    warehouse_id = fields.Many2one('stock.warehouse', required=True)
    pricelist_id = fields.Many2one('product.pricelist', required=True)
    shipping_product_id = fields.Many2one('product.product', domain="[('type', '=', 'service')]")
    auth_type = fields.Selection([('oauth1', 'OAuth 1.0a'), ('bearer', 'Bearer token')], default='oauth1', required=True)
    consumer_key = fields.Char()
    consumer_secret = fields.Char()
    access_token = fields.Char()
    token_secret = fields.Char()
    option_ids = fields.One2many('in.magento.setup.option', 'wizard_id')
    export_store_id = fields.Many2one('in.magento.setup.option', string='Export store view')
    import_store_ids = fields.Many2many('in.magento.setup.option', string='Import order store views')
    attribute_option_id = fields.Many2one('in.magento.setup.option', string='Product attribute set')
    root_option_id = fields.Many2one('in.magento.setup.option', string='Root category')
    customer_store_option_id = fields.Many2one('in.magento.setup.option', string='Customer creation store view')
    source_option_id = fields.Many2one('in.magento.setup.option', string='Inventory source for this warehouse')
    discovery_note = fields.Text(readonly=True)
    tax_mapping_ids = fields.One2many(related='instance_id.tax_mapping_ids', readonly=False)
    inventory_source_ids = fields.One2many(related='instance_id.inventory_source_ids', readonly=False)
    enabled = fields.Boolean(related='instance_id.enabled')
    import_from = fields.Datetime(related='instance_id.import_from', readonly=False)
    auto_import_orders = fields.Boolean(related='instance_id.auto_import_orders', readonly=False)
    auto_confirm_orders = fields.Boolean(related='instance_id.auto_confirm_orders', readonly=False)
    order_interval = fields.Integer(related='instance_id.order_interval', readonly=False)
    auto_export_products = fields.Boolean(related='instance_id.auto_export_products', readonly=False)
    auto_export_categories = fields.Boolean(related='instance_id.auto_export_categories', readonly=False)
    auto_export_attributes = fields.Boolean(related='instance_id.auto_export_attributes', readonly=False)
    catalog_metadata_interval = fields.Integer(related='instance_id.catalog_metadata_interval', readonly=False)
    auto_export_inventory = fields.Boolean(related='instance_id.auto_export_inventory', readonly=False)
    inventory_interval = fields.Integer(related='instance_id.inventory_interval', readonly=False)
    auto_import_customers = fields.Boolean(related='instance_id.auto_import_customers', readonly=False)
    auto_export_customers = fields.Boolean(related='instance_id.auto_export_customers', readonly=False)
    customer_interval = fields.Integer(related='instance_id.customer_interval', readonly=False)
    auto_export_shipments = fields.Boolean(related='instance_id.auto_export_shipments', readonly=False)
    auto_export_cancellations = fields.Boolean(related='instance_id.auto_export_cancellations', readonly=False)
    auto_import_cancellations = fields.Boolean(related='instance_id.auto_import_cancellations', readonly=False)
    auto_export_tracking = fields.Boolean(related='instance_id.auto_export_tracking', readonly=False)
    fulfillment_interval = fields.Integer(related='instance_id.fulfillment_interval', readonly=False)
    import_invoices = fields.Boolean(related='instance_id.import_invoices', readonly=False)
    import_refunds = fields.Boolean(related='instance_id.import_refunds', readonly=False)
    import_payment_status = fields.Boolean(related='instance_id.import_payment_status', readonly=False)
    financial_journal_id = fields.Many2one(related='instance_id.financial_journal_id', readonly=False)
    auto_import_financials = fields.Boolean(related='instance_id.auto_import_financials', readonly=False)
    financial_interval = fields.Integer(related='instance_id.financial_interval', readonly=False)
    diagnostics_html = fields.Html(related='instance_id.diagnostics_html')
    diagnostics_checked_at = fields.Datetime(related='instance_id.diagnostics_checked_at')
    diagnostics_current = fields.Boolean(related='instance_id.diagnostics_current')
    worker_probe_completed_at = fields.Datetime(related='instance_id.worker_probe_completed_at')
    enable_after_setup = fields.Boolean(string='Enable synchronization when finishing', default=False)
    accept_warnings = fields.Boolean(string='I reviewed the remaining diagnostic warnings', default=False)

    @api.model
    def default_get(self, names):
        values = super().default_get(names)
        instance = self.env['in.magento.instance'].browse(values.get('instance_id')).exists()
        if instance:
            instance._require_manager()
            for field in ('name', 'company_id', 'base_url', 'warehouse_id', 'pricelist_id', 'shipping_product_id', 'auth_type'):
                if field in names:
                    values[field] = instance[field].id if instance._fields[field].type == 'many2one' else instance[field]
        return values

    def _guard(self):
        self.ensure_one()
        if not self.env.su and not self.env.user.has_group('in_odoo_magento_connector.group_magento_manager'):
            raise AccessError(_('Only Magento connector managers may configure connections.'))
        self.check_access('write')
        if self.instance_id:
            self.instance_id._require_manager()
        if self.company_id not in self.env.companies:
            raise AccessError(_('Select an allowed company.'))

    def _reopen(self):
        return {'type': 'ir.actions.act_window', 'name': _('Guided Magento Setup'),
            'res_model': self._name, 'res_id': self.id, 'view_mode': 'form', 'target': 'new'}

    def action_next(self):
        self._guard()
        if self.stage == 'connection':
            if self.warehouse_id.company_id != self.company_id or (self.pricelist_id.company_id and self.pricelist_id.company_id != self.company_id):
                raise UserError(_('Select a warehouse and pricelist for this company.'))
            values = {key: self[key].id if self._fields[key].type == 'many2one' else self[key]
                for key in ('name', 'company_id', 'base_url', 'warehouse_id', 'pricelist_id', 'shipping_product_id', 'auth_type')}
            credentials = {key: self[key] for key in ('consumer_key', 'consumer_secret', 'access_token', 'token_secret') if self[key]}
            values.update(credentials)
            if self.instance_id:
                self.instance_id.write(values)
            else:
                self.instance_id = self.env['in.magento.instance'].with_company(self.company_id).create(values)
            # Do not keep a second credential copy in a transient wizard.
            self.write({key: False for key in ('consumer_key', 'consumer_secret', 'access_token', 'token_secret')})
        elif self.stage == 'scope':
            required = self.export_store_id | self.attribute_option_id | self.root_option_id
            if len(required) != 3:
                raise UserError(_('Discover Magento settings and select an export store, attribute set and root category.'))
            selections = [(self.export_store_id, ('store', 'all')), (self.import_store_ids, ('store',)),
                (self.attribute_option_id, ('attribute',)), (self.root_option_id, ('root',)),
                (self.customer_store_option_id, ('store',))]
            for options, kinds in selections:
                if any(o.wizard_id != self or o.kind not in kinds for o in options):
                    raise UserError(_('Select choices discovered by this setup wizard.'))
            values = {'store_code': self.export_store_id.code, 'store_ids': ','.join(str(o.remote_id) for o in self.import_store_ids.sorted('remote_id')),
                'attribute_set_id': self.attribute_option_id.remote_id, 'root_category_id': self.root_option_id.remote_id}
            if self.customer_store_option_id:
                values.update(customer_store_id=self.customer_store_option_id.remote_id,
                    customer_website_id=self.customer_store_option_id.website_id)
            self.instance_id.write(values)
        stages = ['connection', 'scope', 'mappings', 'automation', 'review']
        if self.stage != 'review':
            self.stage = stages[stages.index(self.stage) + 1]
        return self._reopen()

    def action_back(self):
        self._guard()
        stages = ['connection', 'scope', 'mappings', 'automation', 'review']
        self.stage = stages[max(0, stages.index(self.stage) - 1)]
        return self._reopen()

    def action_discover(self):
        self._guard()
        if not self.instance_id:
            raise UserError(_('Save the connection step first.'))
        if str(self.env['ir.config_parameter'].sudo().get_param('database.is_neutralized', 'false')).lower() in ('true', '1'):
            raise UserError(_('Magento discovery is blocked on a neutralized database.'))
        with self.instance_id._client(store_code='all') as client:
            stores = client.request('GET', 'store/storeViews')
            groups = client.request('GET', 'store/storeGroups')
            sets = client.request('GET', 'products/attribute-sets/sets/list', params=search_params(size=100, sort='attribute_set_id'))
            try:
                sources = client.request('GET', 'inventory/sources', params=search_params(size=100, sort='source_code'))
            except (MagentoError, UserError, RetryableJobError):
                sources = None
        if (not isinstance(stores, list) or not isinstance(groups, list) or not isinstance(sets, dict)
                or not isinstance(sets.get('items'), list) or sets.get('total_count', len(sets['items'])) > 100):
            raise UserError(_('Invalid or oversized discovery response. Use Connection Settings for large catalogs of attribute sets.'))
        values = [{'kind': 'all', 'name': _('All store views (admin scope)'), 'code': 'all'}]
        for s in stores:
            if not isinstance(s, dict) or type(s.get('id')) is not int or not s.get('code'):
                raise UserError(_('Invalid store-view response.'))
            if s.get('is_active') and s['id'] > 0:
                values.append({'kind': 'store', 'remote_id': s['id'], 'code': s['code'], 'website_id': s.get('website_id', 0),
                    'name': f"{s.get('name', s['code'])} — {s['code']} (ID {s['id']})"})
        roots = set()
        for g in groups:
            if not isinstance(g, dict) or type(g.get('root_category_id')) is not int:
                raise UserError(_('Invalid store-group response.'))
            if g['root_category_id'] > 0 and g['root_category_id'] not in roots:
                roots.add(g['root_category_id'])
                values.append({'kind': 'root', 'remote_id': g['root_category_id'],
                    'name': f"{g.get('name', 'Store group')} — root ID {g['root_category_id']}"})
        for s in sets['items']:
            if not isinstance(s, dict) or type(s.get('attribute_set_id')) is not int or s['attribute_set_id'] < 1:
                raise UserError(_('Invalid attribute-set response.'))
            values.append({'kind': 'attribute', 'remote_id': s['attribute_set_id'],
                'name': f"{s.get('attribute_set_name', 'Attribute set')} (ID {s['attribute_set_id']})"})
        if isinstance(sources, dict) and isinstance(sources.get('items'), list):
            for s in sources['items']:
                if isinstance(s, dict) and s.get('enabled') and s.get('source_code'):
                    values.append({'kind': 'source', 'code': s['source_code'], 'name': f"{s.get('name', '')} — {s['source_code']}"})
        self.write({'export_store_id': False, 'import_store_ids': [Command.clear()], 'attribute_option_id': False,
            'root_option_id': False, 'customer_store_option_id': False, 'source_option_id': False})
        self.option_ids.unlink()
        choices = self.env['in.magento.setup.option'].create([dict(v, wizard_id=self.id) for v in values])
        instance = self.instance_id
        self.write({'export_store_id': choices.filtered(lambda o: o.kind in ('all', 'store') and o.code == instance.store_code)[:1].id,
            'import_store_ids': [Command.set(choices.filtered(lambda o: o.kind == 'store' and str(o.remote_id) in (instance.store_ids or '').replace(' ', '').split(',')).ids)],
            'attribute_option_id': choices.filtered(lambda o: o.kind == 'attribute' and o.remote_id == instance.attribute_set_id)[:1].id,
            'root_option_id': choices.filtered(lambda o: o.kind == 'root' and o.remote_id == instance.root_category_id)[:1].id,
            'customer_store_option_id': choices.filtered(lambda o: o.kind == 'store' and o.remote_id == instance.customer_store_id)[:1].id,
            'discovery_note': _('Choices loaded. Review existing selections; no synchronization was started. Inventory source discovery is limited to the first 100 sources; additional codes can be entered in Mappings.') if sources is not None else _('Store choices loaded. Inventory source discovery failed; check inventory API permissions or enter the source code in Mappings.')})
        return self._reopen()

    def action_add_source(self):
        self._guard()
        option = self.source_option_id
        if not option or option.wizard_id != self or option.kind != 'source':
            raise UserError(_('Choose a discovered inventory source first.'))
        mappings = self.instance_id.with_context(active_test=False).inventory_source_ids
        if mappings.filtered(lambda s: s.warehouse_id == self.warehouse_id or s.source_code == option.code):
            raise UserError(_('This warehouse or source already has a mapping. Review it in the table below.'))
        self.env['in.magento.inventory.source'].create({'instance_id': self.instance_id.id,
            'warehouse_id': self.warehouse_id.id, 'source_code': option.code})
        return self._reopen()

    def action_check(self):
        self._guard()
        self.instance_id.action_run_diagnostics()
        return self._reopen()

    def action_probe(self):
        self._guard()
        self.instance_id.action_probe_worker()
        return self._reopen()

    def action_refresh(self):
        self._guard()
        return self._reopen()

    def action_finish(self):
        self._guard()
        if self.stage != 'review':
            raise UserError(_('Complete the setup steps before finishing.'))
        instance = self.instance_id
        if self.enable_after_setup:
            neutral = str(self.env['ir.config_parameter'].sudo().get_param('database.is_neutralized', 'false')).lower() in ('true', '1')
            if neutral or not instance.active:
                raise UserError(_('Archived connections or neutralized databases cannot start synchronization.'))
            if not instance.diagnostics_checked_at or instance.diagnostics_signature != instance._diagnostic_signature() or instance.diagnostics_state == 'error':
                raise UserError(_('Run diagnostics for the current configuration and resolve errors before enabling synchronization.'))
            if instance.diagnostics_state == 'warning' and not self.accept_warnings:
                raise UserError(_('Review and acknowledge the remaining diagnostic warnings.'))
            instance.enabled = True
        instance.setup_completed_at = fields.Datetime.now()
        return instance.action_operations()
