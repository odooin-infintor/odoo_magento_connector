"""Business sync history. Never persist raw API requests, responses or credentials."""
import json
from datetime import timedelta

from odoo import api, fields, models


class MagentoSyncMixin(models.AbstractModel):
    _name = 'in.magento.sync.mixin'
    _description = 'Magento Synchronization Context'

    def _job_prepare_context_before_enqueue_keys(self):
        return (*super()._job_prepare_context_before_enqueue_keys(), 'magento_sync_trigger')


class MagentoSyncLog(models.Model):
    _name = 'in.magento.sync.log'
    _description = 'Magento Sync Log'
    _order = 'id desc'
    _rec_name = 'summary'

    instance_id = fields.Many2one('in.magento.instance', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='instance_id.company_id', store=True, index=True)
    occurred_at = fields.Datetime(default=fields.Datetime.now, required=True, index=True)
    direction = fields.Selection([('export', 'Odoo → Magento'), ('import', 'Magento → Odoo')], required=True)
    operation = fields.Selection([('create', 'Create'), ('update', 'Update'), ('import', 'Import'),
                                  ('accounting', 'Accounting'), ('payment', 'Payment status'), ('return', 'Physical return'), ('tracking', 'Tracking'), ('customer', 'Customer'), ('poll', 'Fetch orders'), ('image', 'Image export'), ('skip', 'Skip'),
                                  ('export', 'Export attempt'), ('inventory', 'Inventory'), ('variants', 'Variant links'),
                                  ('attribute', 'Attribute publication'), ('shipment', 'Shipment'), ('cancellation', 'Cancellation'), ('fulfillment', 'Fulfillment check')], required=True)
    status = fields.Selection([('success', 'Success'), ('failed', 'Failed'), ('retry', 'Retry scheduled'),
                               ('skipped', 'Skipped')], required=True, index=True)
    trigger = fields.Selection([('manual', 'Manual'), ('automatic', 'Automatic')], required=True)
    user_id = fields.Many2one('res.users', string='Initiated by', ondelete='set null')
    summary = fields.Char(required=True)
    currency_code = fields.Char(string='Currency')
    reference = fields.Char(string='SKU / Magento reference', index=True)
    product_binding_id = fields.Many2one('in.magento.product.binding', ondelete='set null')
    template_binding_id = fields.Many2one('in.magento.template.binding', ondelete='set null')
    attribute_mapping_id = fields.Many2one('in.magento.attribute.mapping', ondelete='set null')
    accounting_id = fields.Many2one('in.magento.accounting', ondelete='set null')
    shipment_id = fields.Many2one('in.magento.shipment', ondelete='set null')
    category_binding_id = fields.Many2one('in.magento.category.binding', ondelete='set null')
    order_binding_id = fields.Many2one('in.magento.order.binding', ondelete='set null')
    customer_binding_id = fields.Many2one('in.magento.partner.binding', ondelete='set null')
    customer_address_id = fields.Many2one('in.magento.customer.address', ondelete='set null')
    product_id = fields.Many2one(related='product_binding_id.product_id')
    sale_order_id = fields.Many2one(related='order_binding_id.sale_order_id')
    job_id = fields.Many2one('queue.job', ondelete='set null', index=True)
    job_uuid = fields.Char(string='Job reference')
    attempt = fields.Integer()
    previous_source = fields.Selection([('magento', 'Read from Magento before update'),
        ('new', 'New record'), ('none', 'Not captured')], default='none', required=True)
    change_ids = fields.One2many('in.magento.sync.log.change', 'log_id', string='Field changes')
    error = fields.Text(string='Failure / retry details')

    @api.model
    def _record(self, record, *, direction, operation, summary, before=None, after=None,
                previous_source='none', status='success', job=None, error=None):
        record.ensure_one()
        instance = record if record._name == 'in.magento.instance' else record.instance_id
        # This internal helper elevates only creation of the read-only audit record.
        record.check_access('read')
        job = job or self.env['queue.job'].sudo().search([
            ('uuid', '=', record.env.context.get('job_uuid', ''))], limit=1)
        trigger = record.env.context.get('magento_sync_trigger', 'manual')
        values = dict(instance_id=instance.id, direction=direction, operation=operation, status=status,
                      trigger=trigger if trigger in ('manual', 'automatic') else 'manual',
                      summary=summary, previous_source=previous_source, error=error,
                      currency_code=instance.pricelist_id.currency_id.name,
                      user_id=(job.user_id or record.env.user).id, job_id=job.id,
                      job_uuid=job.uuid, attempt=job.retry if job else 0)
        if record._name == 'in.magento.product.binding':
            values.update(product_binding_id=record.id, reference=record.sku)
        elif record._name == 'in.magento.template.binding':
            values.update(template_binding_id=record.id, reference=record.sku)
        elif record._name == 'in.magento.attribute.mapping':
            values.update(attribute_mapping_id=record.id, reference=record.attribute_code)
        elif record._name == 'in.magento.accounting':
            values.update(accounting_id=record.id, order_binding_id=record.order_binding_id.id, reference=record.name)
        elif record._name == 'in.magento.shipment':
            values.update(shipment_id=record.id, order_binding_id=record.order_binding_id.id, reference=record.picking_id.name)
        elif record._name == 'in.magento.category.binding':
            values.update(category_binding_id=record.id, reference=record.external_id)
        elif record._name == 'in.magento.order.binding':
            values.update(order_binding_id=record.id, reference=record.name)
        elif record._name == 'in.magento.partner.binding':
            values.update(customer_binding_id=record.id, reference=record.external_id)
        elif record._name == 'in.magento.customer.address':
            values.update(customer_binding_id=record.binding_id.id, customer_address_id=record.id, reference=record.external_id)
        changes = []
        for key, value in (after or {}).items():
            known = before is not None and key in before
            old = before.get(key) if before is not None else None
            if known and old == value:
                continue
            changes.append(fields.Command.create({
                'field_name': key,
                'previous_value': json.dumps(old, ensure_ascii=False) if known else 'Not captured' if previous_source != 'new' else 'No previous record',
                'new_value': json.dumps(value, ensure_ascii=False),
            }))
        values['change_ids'] = changes
        return self.sudo().create(values)

    @api.autovacuum
    def _gc_sync_logs(self):
        for instance in self.env['in.magento.instance'].with_context(active_test=False).search([]):
            if instance.sync_log_retention_days:
                cutoff = fields.Datetime.now() - timedelta(days=instance.sync_log_retention_days)
                self.search([('instance_id', '=', instance.id), ('occurred_at', '<', cutoff)]).unlink()


class MagentoSyncLogChange(models.Model):
    _name = 'in.magento.sync.log.change'
    _description = 'Magento Synchronized Field Change'
    _order = 'id'

    log_id = fields.Many2one('in.magento.sync.log', required=True, ondelete='cascade', index=True)
    company_id = fields.Many2one(related='log_id.company_id', store=True, index=True)
    field_name = fields.Char(required=True, string='Field')
    previous_value = fields.Text(string='Previous value')
    new_value = fields.Text(string='New value')


class QueueJob(models.Model):
    _inherit = 'queue.job'

    def write(self, vals):
        # OCA writes failed/retry state in its recovery transaction, after the
        # business savepoint rolls back. Logging there survives that rollback.
        old_states = {job.id: job.state for job in self} if vals.get('state') in ('failed', 'pending') else {}
        result = super().write(vals)
        methods = {
            '_job_import_document': ('import', 'accounting'), '_job_sync_financials': ('import', 'payment'),
            '_job_financial_page': ('import', 'accounting'), '_job_export_tracking': ('export', 'tracking'),
            '_job_poll_customers': ('import', 'customer'), '_job_customer_page': ('import', 'customer'),
            '_job_import_customer': ('import', 'customer'), '_job_export_customer': ('export', 'customer'),
            '_job_customer_if_changed': ('export', 'customer'), '_job_export_address': ('export', 'customer'),
            '_job_export': ('export', 'export'), '_job_export_if_changed': ('export', 'export'),
            '_job_export_image': ('export', 'image'), '_job_import': ('import', 'import'),
            '_job_export_inventory': ('export', 'inventory'), '_job_export_inventory_batch': ('export', 'inventory'),
            '_job_export_prices': ('export', 'update'),
            '_job_set_publication': ('export', 'update'),
            '_job_link_variants': ('export', 'variants'),
            '_job_assign_attribute_set': ('export', 'attribute'),
            '_job_export_shipment': ('export', 'shipment'), '_job_sync_fulfillment': ('import', 'fulfillment'),
            '_job_poll_orders': ('import', 'poll'), '_job_poll_page': ('import', 'poll'),
        }
        models_allowed = ('in.magento.accounting', 'in.magento.instance', 'in.magento.product.binding',
                          'in.magento.category.binding', 'in.magento.order.binding', 'in.magento.template.binding',
                          'in.magento.attribute.mapping', 'in.magento.shipment', 'in.magento.partner.binding', 'in.magento.customer.address')
        for job in self.filtered(lambda j: j.id in old_states and j.model_name in models_allowed and j.method_name in methods):
            retrying = job.state == 'pending' and old_states[job.id] == 'started' and job.retry > 0
            failed = job.state == 'failed' and old_states[job.id] != 'failed'
            if not (retrying or failed):
                continue
            direction, operation = methods[job.method_name]
            for record in job.records.exists():
                if job.method_name == '_job_sync_fulfillment' and record.sale_order_id.state == 'cancel':
                    direction, operation = 'export', 'cancellation'
                # Classify known errors, never copy an exception body: it can
                # contain remote/customer data or credentials.
                message = 'The job will retry automatically.' if retrying else 'Synchronization failed.'
                source = str(job.result if retrying else job.exc_message or '').lower()
                reasons = {
                    'certificate verification failed': 'Magento TLS certificate verification failed.',
                    'authentication or api permission failed': 'Magento authentication or API permissions failed.',
                    'totals differ': 'Magento and Odoo order totals differ. Review taxes, discounts and currency.',
                    'explicit tax mapping': 'An order tax rate needs an explicit mapping.',
                    'before retrying': 'Review the record mapping and reconcile Magento before retrying.',
                    'connection failed': 'Magento is unreachable; an automatic retry is scheduled.',
                    'synchronization is disabled': 'Synchronization is disabled for this connection.',
                    'neutralized database': 'Synchronization is blocked on a neutralized database.',
                }
                for pattern, reason in reasons.items():
                    if pattern in source:
                        message = reason
                        break
                message += ' Open Background Job for details.'
                self.env['in.magento.sync.log']._record(
                    record, direction=direction, operation=operation,
                    status='retry' if retrying else 'failed', summary=message,
                    job=job, error=message)
        return result
