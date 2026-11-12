"""Connection-scoped operations and read-only, queued readiness checks."""
import hashlib
import json
from datetime import datetime, time, timedelta
from urllib.parse import quote

from markupsafe import Markup
import pytz
from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.tools.misc import format_datetime
from odoo.addons.queue_job.exception import RetryableJobError
from ..services.client import MagentoError, search_params

MANAGER = 'in_odoo_magento_connector.group_magento_manager'
JOB_MODELS = ('in.magento.instance', 'in.magento.product.binding', 'in.magento.template.binding',
    'in.magento.category.binding', 'in.magento.attribute.mapping', 'in.magento.order.binding',
    'in.magento.partner.binding', 'in.magento.customer.address', 'in.magento.shipment', 'in.magento.accounting')
WAITING = ('pending', 'enqueued', 'wait_dependencies')
CONFIG_FIELDS = ('base_url', 'company_id', 'store_code', 'store_ids', 'auth_type', 'consumer_key',
    'consumer_secret', 'access_token', 'token_secret', 'warehouse_id', 'pricelist_id', 'shipping_product_id',
    'attribute_set_id', 'root_category_id', 'customer_website_id', 'customer_store_id',
    'financial_journal_id', 'import_invoices', 'import_refunds', 'import_payment_status',
    'auto_import_orders', 'auto_confirm_orders', 'auto_export_products', 'auto_export_categories',
    'auto_export_attributes', 'auto_export_inventory', 'auto_import_customers', 'auto_export_customers',
    'auto_export_shipments', 'auto_export_cancellations', 'auto_import_cancellations',
    'auto_export_tracking', 'auto_import_financials', 'order_interval', 'customer_interval',
    'catalog_metadata_interval', 'inventory_interval', 'fulfillment_interval', 'financial_interval')


class MagentoQueueOperations(models.Model):
    _inherit = 'queue.job'

    magento_instance_id = fields.Many2one('in.magento.instance', compute='_compute_magento_instance',
        store=True, index=True, readonly=True, ondelete='set null')

    @api.depends('records', 'model_name')
    def _compute_magento_instance(self):
        # Stored metadata also backfills existing jobs on module upgrade.
        # It contains only the connection identity, never arguments or secrets.
        for job in self:
            instance = self.env['in.magento.instance']
            if job.model_name in JOB_MODELS and job.records:
                records = job.records.sudo().exists()
                instance = records if job.model_name == 'in.magento.instance' else records.instance_id
            job.magento_instance_id = instance if len(instance) == 1 else False


class MagentoOperations(models.Model):
    _inherit = 'in.magento.instance'

    setup_completed_at = fields.Datetime(readonly=True, copy=False)
    diagnostics_checked_at = fields.Datetime(readonly=True, copy=False, groups=MANAGER)
    diagnostics_requested_at = fields.Datetime(readonly=True, copy=False, groups=MANAGER)
    diagnostics_signature = fields.Char(readonly=True, copy=False, groups=MANAGER)
    diagnostics_results = fields.Json(readonly=True, copy=False, groups=MANAGER)
    diagnostics_state = fields.Selection([('not_run', 'Not checked'), ('pass', 'Passed'),
        ('warning', 'Review'), ('error', 'Action required')], default='not_run', readonly=True, copy=False, groups=MANAGER)
    diagnostics_html = fields.Html(compute='_compute_diagnostics_html', sanitize=True, groups=MANAGER)
    diagnostics_current = fields.Boolean(compute='_compute_diagnostics_html', groups=MANAGER)
    worker_probe_requested_at = fields.Datetime(readonly=True, copy=False, groups=MANAGER)
    worker_probe_completed_at = fields.Datetime(readonly=True, copy=False, groups=MANAGER)
    failed_job_count = fields.Integer(compute='_compute_operations', groups=MANAGER)
    waiting_job_count = fields.Integer(compute='_compute_operations', groups=MANAGER)
    running_job_count = fields.Integer(compute='_compute_operations', groups=MANAGER)
    pending_order_count = fields.Integer(compute='_compute_operations', groups=MANAGER)
    oldest_waiting_at = fields.Datetime(compute='_compute_operations', groups=MANAGER)
    last_success_at = fields.Datetime(compute='_compute_operations', groups=MANAGER)
    successful_syncs_today = fields.Integer(compute='_compute_operations', groups=MANAGER,
        help='Successful Sync Log entries today in your timezone, not unique products or queue jobs.')
    recent_activity_ids = fields.Many2many('in.magento.sync.log', compute='_compute_recent_activity', groups=MANAGER)
    recent_activity_html = fields.Html(compute='_compute_recent_activity', sanitize=True, groups=MANAGER)
    operations_status = fields.Selection([('paused', 'Paused'), ('attention', 'Needs attention'),
        ('waiting', 'Work waiting'), ('ready', 'No outstanding jobs')], compute='_compute_operations', groups=MANAGER)

    @api.depends_context('tz', 'uid')
    def _compute_operations(self):
        counts = {(instance.id, state): count for instance, state, count in self.env['queue.job']._read_group(
            [('magento_instance_id', 'in', self.ids), ('state', 'in', (*WAITING, 'failed', 'started'))],
            ['magento_instance_id', 'state'], ['__count'])}
        oldest = {instance.id: date for instance, date in self.env['queue.job']._read_group(
            [('magento_instance_id', 'in', self.ids), ('state', 'in', WAITING)],
            ['magento_instance_id'], ['date_created:min'])}
        success = {instance.id: date for instance, date in self.env['in.magento.sync.log']._read_group(
            [('instance_id', 'in', self.ids), ('status', '=', 'success')],
            ['instance_id'], ['occurred_at:max'])}
        pending = {instance.id: count for instance, count in self.env['in.magento.order.binding']._read_group(
            [('instance_id', 'in', self.ids), ('state', '=', 'pending'), ('sale_order_id', '=', False)],
            ['instance_id'], ['__count'])}
        today = {instance.id: count for instance, count in self.env['in.magento.sync.log']._read_group(
            [('instance_id', 'in', self.ids), *self._today_success_domain()], ['instance_id'], ['__count'])}
        for record in self:
            record.failed_job_count = counts.get((record.id, 'failed'), 0)
            record.waiting_job_count = sum(counts.get((record.id, state), 0) for state in WAITING)
            record.running_job_count = counts.get((record.id, 'started'), 0)
            record.oldest_waiting_at = oldest.get(record.id)
            record.last_success_at = success.get(record.id)
            record.successful_syncs_today = today.get(record.id, 0)
            record.pending_order_count = pending.get(record.id, 0)
            record.operations_status = ('paused' if not record.enabled else 'attention' if
                record.failed_job_count or record.pending_order_count or record.diagnostics_state == 'error'
                else 'waiting' if record.waiting_job_count else 'ready')

    def _today_success_domain(self):
        # Localize both midnights independently: DST days can be 23 or 25 hours.
        zone = pytz.timezone(self.env.context.get('tz') or self.env.user.tz or 'UTC')
        today = fields.Datetime.context_timestamp(self, fields.Datetime.now()).date()
        start = zone.localize(datetime.combine(today, time.min)).astimezone(pytz.UTC).replace(tzinfo=None)
        end = zone.localize(datetime.combine(today + timedelta(days=1), time.min)).astimezone(pytz.UTC).replace(tzinfo=None)
        return [('status', '=', 'success'), ('occurred_at', '>=', start), ('occurred_at', '<', end)]

    @api.depends_context('tz', 'uid', 'lang')
    def _compute_recent_activity(self):
        Log = self.env['in.magento.sync.log']
        statuses = dict(Log._fields['status']._description_selection(self.env))
        operations = dict(Log._fields['operation']._description_selection(self.env))
        for record in self:
            # Bounded reads use the connection/time index, regardless of log retention.
            logs = Log.search([('instance_id', '=', record.id)], order='occurred_at desc, id desc', limit=10)
            record.recent_activity_ids = logs
            rows = []
            for log in logs[:3]:
                rows.append(Markup('<div class="border-top py-2"><div><strong>{}</strong> · {} · {}</div>'
                    '<div class="small text-muted">{} · {}</div><div class="small text-break">{}</div></div>').format(
                    log.reference or log.summary, operations[log.operation], statuses[log.status],
                    format_datetime(self.env, log.occurred_at),
                    _('Odoo → Magento') if log.direction == 'export' else _('Magento → Odoo'),
                    log.activity_details or log.summary))
            record.recent_activity_html = Markup('').join(rows) if rows else Markup('<span class="text-muted">{}</span>').format(_('No activity recorded yet.'))

    def action_successful_syncs_today(self):
        self.ensure_one()
        self._require_manager()
        return {'type': 'ir.actions.act_window', 'name': _('Successful Syncs Today'),
            'res_model': 'in.magento.sync.log', 'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id), *self._today_success_domain()]}

    def _diagnostic_signature(self):
        self.ensure_one()
        self._require_manager()
        values = {name: self[name].id if self._fields[name].type == 'many2one' else self[name]
                  for name in CONFIG_FIELDS}
        values['sources'] = [(s.id, s.active, s.warehouse_id.id, s.source_code)
            for s in self.with_context(active_test=False).inventory_source_ids.sorted('id')]
        values['taxes'] = [(t.id, t.rate, t.tax_ids.ids, t.tax_ids.mapped('amount'),
            t.tax_ids.mapped('amount_type')) for t in self.tax_mapping_ids.sorted('id')]
        return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()

    @api.depends(*CONFIG_FIELDS, 'diagnostics_results', 'diagnostics_checked_at', 'diagnostics_signature',
        'inventory_source_ids.active', 'inventory_source_ids.warehouse_id', 'inventory_source_ids.source_code',
        'tax_mapping_ids.rate', 'tax_mapping_ids.tax_ids', 'tax_mapping_ids.tax_ids.amount',
        'tax_mapping_ids.tax_ids.amount_type')
    def _compute_diagnostics_html(self):
        statuses = {'pass': _('Passed'), 'warning': _('Review'), 'error': _('Action required')}
        for record in self:
            record.diagnostics_current = bool(record.diagnostics_checked_at and
                record.diagnostics_signature == record._diagnostic_signature())
            rows = [Markup('<tr><td><strong>{}</strong></td><td>{}</td><td>{}</td></tr>').format(
                row['label'], statuses[row['status']],
                row['detail']) for row in record.diagnostics_results or []]
            record.diagnostics_html = Markup('<table class="table table-sm"><thead><tr><th>Check</th><th>Result</th><th>Next step / evidence</th></tr></thead><tbody>{}</tbody></table>').format(Markup('').join(rows)) if rows else False

    def action_open_setup(self):
        self.ensure_one()
        self._require_manager()
        return {'type': 'ir.actions.act_window', 'name': _('Guided Magento Setup'),
            'res_model': 'in.magento.setup', 'view_mode': 'form', 'target': 'new',
            'context': {'default_instance_id': self.id}}

    def action_open_settings(self):
        self.ensure_one()
        self._require_manager()
        return {'type': 'ir.actions.act_window', 'res_model': self._name, 'res_id': self.id,
            'views': [(self.env.ref('in_odoo_magento_connector.view_instance_form').id, 'form')], 'target': 'current'}

    def action_operations(self):
        self.ensure_one()
        self._require_manager()
        return {'type': 'ir.actions.act_window', 'name': _('Magento Operations'), 'res_model': self._name,
            'res_id': self.id, 'views': [(self.env.ref('in_odoo_magento_connector.view_operations_form').id, 'form')]}

    def action_jobs(self):
        self.ensure_one()
        self._require_manager()
        return {'type': 'ir.actions.act_window', 'name': _('Connection Background Jobs'),
            'res_model': 'queue.job', 'view_mode': 'list,form', 'domain': [('magento_instance_id', '=', self.id)]}

    def action_failed_jobs(self):
        action = self.action_jobs()
        action['domain'].append(('state', '=', 'failed'))
        return action

    def action_waiting_jobs(self):
        action = self.action_jobs()
        action['domain'].append(('state', 'in', WAITING))
        return action

    def action_pending_orders(self):
        self.ensure_one()
        self._require_manager()
        return {'type': 'ir.actions.act_window', 'name': _('Orders Awaiting Import'),
            'res_model': 'in.magento.order.binding', 'view_mode': 'list,form',
            'domain': [('instance_id', '=', self.id), ('state', '=', 'pending'), ('sale_order_id', '=', False)]}

    def action_probe_worker(self):
        self.ensure_one()
        self._require_manager()
        self.with_company(self.company_id).with_delay(identity_key=f'magento:probe:{self.id}',
            description=_('%s: verify background worker', self.name))._job_probe_worker()
        self.worker_probe_requested_at = fields.Datetime.now()
        return self.action_operations()

    def _job_probe_worker(self):
        self.ensure_one()
        self._require_manager()
        # Local acknowledgement only; intentionally works while sync is paused.
        self.worker_probe_completed_at = fields.Datetime.now()

    def action_run_diagnostics(self):
        self.ensure_one()
        self._require_manager()
        self.with_company(self.company_id).with_delay(identity_key=f'magento:diagnostics:{self.id}',
            description=_('%s: check configuration and API reads', self.name))._job_run_diagnostics()
        self.diagnostics_requested_at = fields.Datetime.now()
        return self.action_operations()

    def _job_run_diagnostics(self):
        self.ensure_one()
        self._require_manager()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another connection check is running.', seconds=10)
        self.invalidate_recordset()
        signature = self._diagnostic_signature()
        results = self._diagnostic_checks()
        state = 'error' if any(r['status'] == 'error' for r in results) else 'warning' if any(
            r['status'] == 'warning' for r in results) else 'pass'
        self.write({'diagnostics_results': results, 'diagnostics_state': state,
            'diagnostics_checked_at': fields.Datetime.now(), 'diagnostics_signature': signature})

    def _diagnostic_checks(self):
        results = []
        def add(code, label, status, detail):
            results.append(dict(code=code, label=label, status=status, detail=detail))
        neutral = str(self.env['ir.config_parameter'].sudo().get_param('database.is_neutralized', 'false')).lower() in ('true', '1')
        add('database', _('Database'), 'error' if neutral else 'pass',
            _('Database is neutralized; no Magento requests were made.') if neutral else _('Database allows synchronization.'))
        credentials = self.access_token and (self.auth_type == 'bearer' or all((self.consumer_key, self.consumer_secret, self.token_secret)))
        add('credentials', _('API credentials'), 'pass' if credentials else 'error',
            _('Required credential fields are filled. API access is checked below.') if credentials else _('Complete the credentials in Connection Settings.'))
        cron = self.env.ref('in_odoo_magento_connector.cron_schedule_magento').sudo()
        recent = cron.active and cron.lastcall and cron.lastcall >= fields.Datetime.now() - timedelta(minutes=5)
        add('cron', _('Synchronization scheduler'), 'pass' if recent else 'warning',
            _('Scheduler has run within five minutes.') if recent else _('Check that the Magento scheduler is active and Odoo cron workers are running.'))
        probe = self.worker_probe_completed_at
        confirmed = probe and self.worker_probe_requested_at and probe >= self.worker_probe_requested_at and probe >= fields.Datetime.now() - timedelta(minutes=15)
        add('worker', _('Background worker'), 'pass' if confirmed else 'warning',
            _('A requested probe completed within fifteen minutes; this is a snapshot, not continuous monitoring.') if confirmed else _('Click Test Worker, wait for completion, then rerun diagnostics.'))
        if self.auto_import_orders:
            add('order_setup', _('Order prerequisites'), 'pass' if self.store_ids and self.shipping_product_id and self.tax_mapping_ids else 'error',
                _('Import store IDs, shipping product and tax mappings are required. Validate every actual Magento rate, including zero, in a test order.'))
            add('currency', _('Order currency'), 'warning',
                _('Orders must use the connector pricelist currency (%s). Test checkout taxes and totals before production.', self.pricelist_id.currency_id.name))
        if self.import_invoices or self.import_refunds:
            add('journal', _('Accounting journal'), 'pass' if self.financial_journal_id else 'error',
                _('Select a sales journal before importing invoice or credit-memo drafts.'))
        sources = self.inventory_source_ids.filtered('active')
        if self.auto_export_inventory or self.auto_export_shipments:
            add('sources', _('Warehouse/source mappings'), 'pass' if sources else 'error',
                _('At least one enabled warehouse/source mapping is required. Enable inventory per intended product as well.'))
        if neutral or not credentials:
            return results
        # Each read is isolated so one missing permission does not hide the other results.
        def check(code, label, path, validate, params=None):
            try:
                with self._client() as client:
                    data = client.request('GET', path, params=params)
                valid = validate(data)
            except (UserError, RetryableJobError, MagentoError):
                add(code, label, 'error', _('Read check failed. Check HTTPS connectivity, API resource permissions and the configured IDs. No write was attempted.'))
                return None
            if not valid:
                add(code, label, 'error', _('The response does not match the configured scope or identity. Review the mapping.'))
                return None
            add(code, label, 'pass', _('Read access and the returned structure/identity were verified. Write access is not proven.'))
            return data
        stores = check('stores', _('Store views and scope'), 'store/storeViews', lambda d: isinstance(d, list) and all(
            isinstance(s, dict) and type(s.get('id')) is int for s in d))
        if stores is not None:
            ids = {s['id'] for s in stores if s.get('is_active')}
            configured = {int(v.strip()) for v in self.store_ids.split(',')} if self.store_ids else set()
            valid = configured.issubset(ids) and (self.store_code == 'all' or any(s.get('code') == self.store_code and s.get('is_active') for s in stores))
            add('scope', _('Selected store scope'), 'pass' if valid else 'error', _('Selected import IDs/export code must refer to active Magento store views.'))
            if self.auto_import_customers or self.auto_export_customers:
                valid = any(s.get('id') == self.customer_store_id and s.get('website_id') == self.customer_website_id and s.get('is_active') for s in stores)
                add('customer_scope', _('Customer website/store'), 'pass' if valid else 'error', _('The customer creation store must be active and belong to the selected website.'))
        check('attribute_set', _('Product attribute set'), f'products/attribute-sets/{self.attribute_set_id}',
            lambda d: isinstance(d, dict) and d.get('attribute_set_id') == self.attribute_set_id)
        check('category', _('Root category'), f'categories/{self.root_category_id}',
            lambda d: isinstance(d, dict) and d.get('id') == self.root_category_id)
        checks = [('products', _('Product read access'), True, 'id'),
            ('orders', _('Order read access'), self.auto_import_orders or self.auto_import_financials or self.auto_import_cancellations or self.auto_export_shipments or self.auto_export_cancellations, 'entity_id'),
            ('customers/search', _('Customer read access'), self.auto_import_customers or self.auto_export_customers, 'id'),
            ('invoices', _('Invoice read access'), self.import_invoices, 'entity_id'),
            ('creditmemos', _('Credit-memo read access'), self.import_refunds, 'entity_id'),
            ('shipments', _('Shipment read access'), self.auto_export_shipments or self.auto_export_tracking, 'entity_id')]
        for path, label, enabled, key in checks:
            if enabled:
                params = search_params(size=1, sort='entity_id')
                params['fields'] = f'items[{key}],total_count'
                # Magento's fields projection serializes an empty items array
                # as null. Accept it only with an explicit zero total; a null
                # collection for a nonempty result is still malformed.
                check(path, label, path, lambda d: isinstance(d, dict) and (
                    isinstance(d.get('items'), list) or (
                        'items' in d and d['items'] is None
                        and type(d.get('total_count')) is int and d['total_count'] == 0)), params)
        for source in sources:
            check(f'source:{source.id}', _('Inventory source: %s', source.source_code),
                'inventory/sources/' + quote(source.source_code, safe=''),
                lambda d, code=source.source_code: isinstance(d, dict) and d.get('source_code') == code and bool(d.get('enabled')))
        add('writes', _('Write permissions and business validation'), 'warning',
            _('Read-only diagnostics cannot prove product, price, stock, shipment or cancellation write permissions. Complete a controlled publication/order test before production.'))
        return results


class MagentoActivityLog(models.Model):
    _inherit = 'in.magento.sync.log'

    _operations_recent_idx = models.Index('(instance_id, occurred_at DESC, id DESC)')
    activity_details = fields.Char(compute='_compute_activity_details', string='Changes')

    @api.depends('change_ids.field_name', 'change_ids.previous_value', 'change_ids.new_value', 'status')
    def _compute_activity_details(self):
        def compact(value, inventory=False):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                pass
            if inventory and isinstance(value, dict) and 'quantity' in value:
                value = value['quantity']
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return f'{value:g}'
            text = str(value) if value is not None else '—'
            return text if len(text) <= 80 else text[:77] + '…'
        for log in self:
            # Failed attempts never claim that proposed changes were committed.
            changes = log.change_ids if log.status == 'success' else self.env['in.magento.sync.log.change']
            parts = []
            inventory = log.operation == 'inventory'
            for change in changes[:3]:
                label = _('Stock (%s)', change.field_name) if inventory else change.field_name
                parts.append(f'{label}: {compact(change.previous_value, inventory)} → {compact(change.new_value, inventory)}')
            if len(changes) > 3:
                parts.append(_('and %s more', len(changes) - 3))
            log.activity_details = '; '.join(parts)
