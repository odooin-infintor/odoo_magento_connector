"""Import financial documents as drafts; never capture/refund or register money."""
from collections import defaultdict
from datetime import timedelta

from odoo import api, fields, models, Command, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.client import search_params
from .order import number


class MagentoAccountingInstance(models.Model):
    _inherit = 'in.magento.instance'

    import_invoices = fields.Boolean(string='Import Magento invoices', default=False)
    import_refunds = fields.Boolean(string='Import Magento credit memos', default=False)
    import_payment_status = fields.Boolean(string='Read Magento payment status', default=False)
    auto_import_financials = fields.Boolean(string='Automatic financial synchronization', default=False)
    financial_interval = fields.Integer(default=5, string='Financial interval (minutes)')
    financial_recent_days = fields.Integer(default=30, string='Recent order window (days)',
        help='Active orders and orders placed within this window use the normal financial interval.')
    financial_history_hours = fields.Integer(default=24, string='Older completed orders interval (hours)',
        help='Keep checking older completed, closed and canceled orders for late invoices or refunds.')
    financial_poll_limit = fields.Integer(default=200, string='Orders per scheduled financial pass',
        help='Maximum orders queued per pass. Reserve part of each pass for historical checks; manual synchronization bypasses this limit.')
    last_financial_schedule = fields.Datetime(readonly=True, copy=False)
    financial_journal_id = fields.Many2one('account.journal', check_company=True,
        domain="[('type', '=', 'sale'), ('company_id', '=', company_id)]", string='Invoice journal')
    accounting_ids = fields.One2many('in.magento.accounting', 'instance_id')
    accounting_count = fields.Integer(compute='_compute_accounting_count')

    def _compute_accounting_count(self):
        for record in self:
            record.accounting_count = len(record.accounting_ids)

    @api.constrains('financial_interval', 'financial_journal_id', 'import_refunds', 'import_invoices',
                    'financial_recent_days', 'financial_history_hours', 'financial_poll_limit')
    def _check_financial_config(self):
        for record in self:
            if record.financial_interval < 1:
                raise ValidationError(_('Financial interval must be at least one minute.'))
            if record.financial_recent_days < 1 or record.financial_history_hours < 1 or not 4 <= record.financial_poll_limit <= 1000:
                raise ValidationError(_('Use positive financial polling windows and a pass limit between 4 and 1,000 orders.'))
            if record.financial_journal_id and record.financial_journal_id.type != 'sale':
                raise ValidationError(_('Select a sales journal for Magento invoices.'))
            if record.import_refunds and not record.import_invoices:
                raise ValidationError(_('Enable invoice imports before importing credit memos.'))

    def action_view_accounting(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Magento Accounting'),
                'res_model': 'in.magento.accounting', 'view_mode': 'list,form',
                'domain': [('instance_id', '=', self.id)]}

    def action_sync_financials(self):
        self._require_manager()
        for instance in self:
            instance._check_enabled()
            instance.order_ids.filtered('sale_order_id').action_sync_financials()
        return True

    @api.model
    def _cron_schedule(self):
        super()._cron_schedule()
        if str(self.env['ir.config_parameter'].sudo().get_param('database.is_neutralized', 'false')).lower() in ('true', '1'):
            return
        now = fields.Datetime.now()
        for instance in self.search([('enabled', '=', True), ('auto_import_financials', '=', True)]):
            if not instance.last_financial_schedule or instance.last_financial_schedule <= now - timedelta(minutes=instance.financial_interval):
                instance.with_context(magento_sync_trigger='automatic')._schedule_due_financials(now)
                instance.last_financial_schedule = now

    def _schedule_due_financials(self, now):
        self.ensure_one()
        self._check_enabled()
        if not (self.import_invoices or self.import_refunds or self.import_payment_status):
            return
        base = [('instance_id', '=', self.id), ('sale_order_id', '!=', False)]
        terminal = ['complete', 'closed', 'canceled']
        recent = now - timedelta(days=self.financial_recent_days)
        fast_domain = base + ['|', ('remote_state', 'not in', terminal), ('sale_order_id.date_order', '>=', recent),
            '|', ('financial_checked_at', '=', False),
            ('financial_checked_at', '<=', now - timedelta(minutes=self.financial_interval))]
        history_domain = base + [('remote_state', 'in', terminal), ('sale_order_id.date_order', '<', recent),
            '|', ('financial_checked_at', '=', False),
            ('financial_checked_at', '<=', now - timedelta(hours=self.financial_history_hours))]
        Orders = self.env['in.magento.order.binding'].with_company(self.company_id)
        limit = self.financial_poll_limit
        # Oldest due first, with capacity for both groups even under a backlog.
        fast = Orders.search(fast_domain, order='financial_checked_at ASC NULLS FIRST, id', limit=limit)
        history = Orders.search(history_domain, order='financial_checked_at ASC NULLS FIRST, id', limit=limit)
        reserved = max(1, limit // 4)
        selected = fast[:limit - reserved] | history[:reserved]
        selected |= ((fast | history) - selected)[:limit - len(selected)]
        selected.action_sync_financials()


class MagentoAccountingOrder(models.Model):
    _inherit = 'in.magento.order.binding'

    accounting_ids = fields.One2many('in.magento.accounting', 'order_binding_id')
    payment_snapshot = fields.Json(readonly=True, copy=False)
    payment_checked_at = fields.Datetime(readonly=True, copy=False)
    financial_checked_at = fields.Datetime(readonly=True, copy=False, index=True,
        help='Last successful order financial check. Document jobs continue independently and retain their retries.')
    payment_currency_id = fields.Many2one(related='sale_order_id.currency_id')
    magento_paid = fields.Monetary(compute='_compute_reported_payment', currency_field='payment_currency_id', string='Magento reported paid')
    magento_refunded = fields.Monetary(compute='_compute_reported_payment', currency_field='payment_currency_id', string='Magento reported refunded')
    magento_due = fields.Monetary(compute='_compute_reported_payment', currency_field='payment_currency_id', string='Magento reported due')

    @api.depends('payment_snapshot')
    def _compute_reported_payment(self):
        for record in self:
            data = record.payment_snapshot or {}
            record.magento_paid = data.get('total_paid', 0)
            record.magento_refunded = data.get('total_refunded', 0)
            record.magento_due = data.get('total_due', 0)

    def _allow_complete_virtual_import(self, payload):
        return bool(self.instance_id.import_invoices and payload.get('state') == 'complete' and payload.get('items') and
            all(row.get('product_type') == 'virtual' for row in payload['items']))

    def _processed_import_keys(self):
        keys = super()._processed_import_keys()
        return tuple(key for key in keys if key != 'qty_invoiced') if self.instance_id.import_invoices or self.sale_order_id else keys

    def action_sync_financials(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            if not record.sale_order_id:
                raise UserError(_('Import the sales order first.'))
            record.with_company(record.company_id).with_delay(identity_key=f'magento:finance:{record.id}')._job_sync_financials()
        return True

    def _job_sync_financials(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.sale_order_id:
            raise RetryableJobError('Waiting for order import.', seconds=30)
        if not self.try_lock_for_update():
            raise RetryableJobError('Another financial check is running.', seconds=10)
        with self.instance_id._client() as client:
            remote = self._remote_order(client)
        if remote.get('order_currency_code') != self.sale_order_id.currency_id.name:
            raise UserError(_('Magento order currency changed. Reconcile before retrying.'))
        if self.instance_id.import_payment_status:
            snapshot = {key: number(remote.get(key)) for key in ('total_paid', 'total_refunded', 'total_due')}
            snapshot.update(method=(remote.get('payment') or {}).get('method'), state=remote.get('state'), status=remote.get('status'))
            if snapshot != self.payment_snapshot:
                self.env['in.magento.sync.log']._record(self, direction='import', operation='payment',
                    summary='Magento reported payment status updated; Odoo payments are unchanged.',
                    before=self.payment_snapshot or {}, after=snapshot, previous_source='magento')
            self.write({'payment_snapshot': snapshot, 'payment_checked_at': fields.Datetime.now()})
        for kind, enabled in [('invoice', self.instance_id.import_invoices), ('creditmemo', self.instance_id.import_refunds)]:
            if enabled:
                self.with_delay(identity_key=f'magento:finance:{self.id}:{kind}:0')._job_financial_page(kind, 0)
        self.write({'financial_checked_at': fields.Datetime.now(),
                    'remote_state': remote.get('state'), 'remote_status': remote.get('status')})

    def _job_financial_page(self, kind, after_id=0):
        self.ensure_one()
        self.instance_id._check_enabled()
        if kind not in ('invoice', 'creditmemo'):
            raise UserError(_('Unsupported Magento accounting document.'))
        if not (self.instance_id.import_invoices if kind == 'invoice' else self.instance_id.import_refunds):
            return
        if not self.try_lock_for_update():
            raise RetryableJobError('Another financial import is running.', seconds=10)
        with self.instance_id._client() as client:
            data = client.request('GET', 'invoices' if kind == 'invoice' else 'creditmemos',
                params=search_params([('order_id', 'eq', int(self.external_id)), ('entity_id', 'gt', after_id)], size=self.instance_id.order_batch_size))
        if not isinstance(data, dict) or not isinstance(data.get('items'), list):
            raise UserError(_('Invalid financial document collection.'))
        ids = [int(row['entity_id']) for row in data['items']]
        if ids and (ids != sorted(set(ids)) or ids[0] <= after_id):
            raise UserError(_('Financial pagination did not advance.'))
        Documents = self.env['in.magento.accounting']
        for row in data['items']:
            if str(row.get('order_id')) != self.external_id:
                raise UserError(_('Magento returned a document for a different order.'))
            document = Documents.search([('instance_id', '=', self.instance_id.id), ('kind', '=', kind), ('external_id', '=', str(row['entity_id']))], limit=1)
            if not document:
                document = Documents.sudo().create({'order_binding_id': self.id, 'kind': kind,
                    'external_id': str(row['entity_id']), 'name': row.get('increment_id') or str(row['entity_id'])}).with_env(self.env)
            document.action_import()
        if len(ids) == self.instance_id.order_batch_size:
            self.with_delay(identity_key=f'magento:finance:{self.id}:{kind}:{ids[-1]}')._job_financial_page(kind, ids[-1])


class MagentoAccounting(models.Model):
    _name = 'in.magento.accounting'
    _inherit = 'in.magento.sync.mixin'
    _description = 'Magento Accounting Document'
    _check_company_auto = True
    _order = 'id desc'

    name = fields.Char(required=True, readonly=True)
    order_binding_id = fields.Many2one('in.magento.order.binding', required=True, readonly=True, ondelete='restrict')
    instance_id = fields.Many2one(related='order_binding_id.instance_id', store=True)
    company_id = fields.Many2one(related='instance_id.company_id', store=True)
    kind = fields.Selection([('invoice', 'Invoice'), ('creditmemo', 'Credit memo')], required=True, readonly=True)
    external_id = fields.Char(required=True, readonly=True)
    move_id = fields.Many2one('account.move', check_company=True, readonly=True, copy=False, ondelete='restrict', string='Odoo document')
    imported_at = fields.Datetime(readonly=True, copy=False)
    source_snapshot = fields.Json(readonly=True, copy=False, groups='in_odoo_magento_connector.group_magento_manager')
    return_picking_ids = fields.One2many('stock.picking', 'in_magento_creditmemo_id', readonly=True)
    _identity_unique = models.Constraint('UNIQUE(instance_id, kind, external_id)', 'This Magento document is already mapped.')
    _move_unique = models.Constraint('UNIQUE(move_id)', 'This Odoo accounting document is already mapped.')

    def action_import(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            record.with_company(record.company_id).with_delay(identity_key=f'magento:document:{record.id}')._job_import_document()
        return True

    def _snapshot(self, data):
        # Store financial identifiers/totals only, never customer or payment credentials.
        keys = ('entity_id', 'order_id', 'invoice_id', 'state', 'order_currency_code', 'grand_total',
                'tax_amount', 'subtotal', 'discount_amount', 'shipping_amount', 'shipping_tax_amount',
                'adjustment_positive', 'adjustment_negative', 'discount_tax_compensation_amount',
                'shipping_discount_tax_compensation_amount', 'created_at')
        snapshot = {key: data.get(key) for key in keys}
        snapshot['items'] = [{key: row.get(key) for key in ('order_item_id', 'qty', 'price', 'row_total', 'discount_amount', 'tax_amount', 'discount_tax_compensation_amount')}
                             for row in data.get('items', [])]
        return snapshot

    def _job_import_document(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        enabled = self.instance_id.import_invoices if self.kind == 'invoice' else self.instance_id.import_refunds
        if not enabled:
            return
        order_binding = self.order_binding_id
        if not order_binding.try_lock_for_update():
            raise RetryableJobError('Another accounting import is running for this order.', seconds=10)
        self.invalidate_recordset()
        with self.instance_id._client() as client:
            data = client.request('GET', f"{'invoices' if self.kind == 'invoice' else 'creditmemo'}/{self.external_id}")
        if not isinstance(data, dict) or str(data.get('entity_id')) != self.external_id or str(data.get('order_id')) != order_binding.external_id:
            raise UserError(_('Magento accounting identity changed. Reconcile before retrying.'))
        snapshot = self._snapshot(data)
        if self.move_id:
            if {k: v for k, v in snapshot.items() if k != 'state'} != {k: v for k, v in self.source_snapshot.items() if k != 'state'} or int(data.get('state') or 0) == 3:
                raise UserError(_('An imported Magento document changed. Review both accounting documents before retrying.'))
            return self.move_id.id
        # Atomic even when invoked directly outside the queue worker.
        with self.env.cr.savepoint():
            return self._import_document(snapshot)

    def _import_document(self, data):
        order_binding = self.order_binding_id
        order = order_binding.sale_order_id
        if not order or order.state == 'cancel':
            raise UserError(_('Import an active sales order before accounting documents.'))
        if data.get('order_currency_code') != order.currency_id.name:
            raise UserError(_('Accounting document currency differs from the Odoo order.'))
        if int(data.get('state') or 0) not in ((1, 2) if self.kind == 'invoice' else (2,)):
            raise UserError(_('Only active invoices and completed credit memos can be imported.'))
        if not self.instance_id.financial_journal_id:
            raise UserError(_('Configure a sales journal before importing accounting documents.'))
        if any(number(data.get(key)) for key in ('adjustment_positive', 'adjustment_negative',
                'discount_tax_compensation_amount', 'shipping_discount_tax_compensation_amount')):
            raise UserError(_('Refund adjustments or tax compensation require an accounting review.'))
        unmanaged = order.invoice_ids.filtered(lambda m: m.state != 'cancel') - order_binding.accounting_ids.move_id
        if unmanaged:
            raise UserError(_('This order already has an unlinked Odoo accounting document. Reconcile it before importing to avoid duplicates.'))
        order_binding._ensure_item_links()
        original = self.env['in.magento.accounting']
        if self.kind == 'creditmemo' and data.get('invoice_id'):
            original = self.search([('instance_id', '=', self.instance_id.id), ('kind', '=', 'invoice'), ('external_id', '=', str(data['invoice_id']))], limit=1)
            if not original.move_id:
                raise RetryableJobError('Import the original Magento invoice first.', seconds=60)
            if original.order_binding_id != order_binding:
                raise UserError(_('Credit memo invoice belongs to a different order.'))
        invoices = order_binding.accounting_ids.filtered(lambda d: d.kind == 'invoice' and d.move_id)
        if self.kind == 'creditmemo' and not invoices:
            raise RetryableJobError('Import the original Magento invoice first.', seconds=60)
        lines, quantities, seen = [], {}, set()
        order_items = {str(row['item_id']): row for row in order_binding.payload['items']}
        siblings = order_binding.accounting_ids.filtered(lambda d: d != self and d.kind == self.kind and d.move_id)
        for row in data['items']:
            key = str(row['order_item_id'])
            if key in seen or key not in order_items:
                raise UserError(_('Duplicate or unknown order item on accounting document.'))
            seen.add(key)
            source = order_items[key]
            qty = number(row.get('qty'))
            if qty < 0:
                raise UserError(_('Negative accounting quantities require review.'))
            if not qty:
                continue
            # Configurable children have zero commercial amounts, parent carries totals.
            if source.get('parent_item_id'):
                if source.get('product_type') != 'simple' or any(number(row.get(k)) for k in ('row_total', 'tax_amount', 'discount_amount')):
                    raise UserError(_('Unsupported priced child on accounting document.'))
                if not any(str(item['order_item_id']) == str(source['parent_item_id']) and number(item.get('qty')) == qty for item in data['items']):
                    raise UserError(_('Configurable accounting parent/child quantities differ.'))
                continue
            sale_line = order.order_line.filtered(lambda line: line.in_magento_item_id == key)
            if len(sale_line) != 1:
                raise UserError(_('Map each accounting order item to one sales line before retrying.'))
            used = sum(number(item.get('qty')) for sibling in siblings for item in (sibling.source_snapshot or {}).get('items', []) if str(item['order_item_id']) == key)
            cap = number(source['qty_ordered'])
            if self.kind == 'creditmemo':
                invoiced_qty = sum(number(item.get('qty')) for inv in invoices for item in inv.source_snapshot['items'] if str(item['order_item_id']) == key)
                if qty + used > invoiced_qty + 0.000001:
                    raise UserError(_('Refund quantity exceeds the imported invoices. Review invoice synchronization first.'))
                cap = min(cap, invoiced_qty)
            if original:
                cap = sum(number(item.get('qty')) for item in original.source_snapshot['items'] if str(item['order_item_id']) == key)
                used = sum(number(item.get('qty')) for sibling in siblings if str((sibling.source_snapshot or {}).get('invoice_id')) == str(data['invoice_id']) for item in sibling.source_snapshot['items'] if str(item['order_item_id']) == key)
            if qty + used > cap + 0.000001:
                raise UserError(_('Accounting quantity exceeds the original order/invoice.'))
            gross, discount = number(row.get('row_total')), number(row.get('discount_amount'))
            if gross < 0 or discount < 0 or discount > gross or number(row.get('discount_tax_compensation_amount')):
                raise UserError(_('Unsupported accounting discount or tax compensation.'))
            if order.currency_id.compare_amounts(gross, number(source.get('price')) * qty):
                raise UserError(_('Accounting unit price differs from the imported order.'))
            values = sale_line._prepare_invoice_line(quantity=qty, price_unit=gross / qty,
                discount=100 * discount / gross if gross else 0)
            lines.append(Command.create(values))
            quantities[key] = qty
        shipping, shipping_tax = number(data.get('shipping_amount')), number(data.get('shipping_tax_amount'))
        # Magento shipping_amount is its invoiced/refunded net freight amount.
        if shipping < 0 or (not shipping and shipping_tax):
            raise UserError(_('Unsupported accounting freight amount.'))
        freight_cap = number((original.source_snapshot if original else order_binding.payload).get('shipping_amount'))
        freight_siblings = siblings.filtered(lambda d: str((d.source_snapshot or {}).get('invoice_id')) == str(data['invoice_id'])) if original else siblings
        if self.kind == 'creditmemo':
            all_refunded_shipping = sum(number(d.source_snapshot.get('shipping_amount')) for d in siblings)
            invoiced_shipping = sum(number(d.source_snapshot.get('shipping_amount')) for d in invoices)
            if shipping + all_refunded_shipping > invoiced_shipping + 0.000001:
                raise UserError(_('Refund freight exceeds the imported invoices.'))
        if shipping + sum(number(d.source_snapshot.get('shipping_amount')) for d in freight_siblings) > freight_cap + 0.000001:
            raise UserError(_('Accounting freight exceeds the original order/invoice.'))
        if shipping:
            freight = order.order_line.filtered(lambda l: not l.display_type and not l.in_magento_item_id and l.product_id == self.instance_id.shipping_product_id)
            if len(freight) != 1:
                raise UserError(_('Map the order shipping line before importing freight.'))
            lines.append(Command.create(freight._prepare_invoice_line(quantity=1, price_unit=shipping, discount=0,
                tax_ids=[Command.set(order_binding._taxes(round(100 * shipping_tax / shipping, 6)).ids)])))
        if not lines:
            raise UserError(_('Accounting document has no supported lines.'))
        total_cap = number(order_binding.payload.get('grand_total')) if self.kind == 'invoice' else sum(number(inv.source_snapshot.get('grand_total')) for inv in invoices)
        total_used = sum(number(doc.source_snapshot.get('grand_total')) for doc in siblings)
        if order.currency_id.compare_amounts(total_used + number(data.get('grand_total')), total_cap) > 0:
            raise UserError(_('Accounting totals exceed the original order or imported invoices.'))
        values = order._prepare_invoice()
        values.update(move_type='out_invoice' if self.kind == 'invoice' else 'out_refund',
            journal_id=self.instance_id.financial_journal_id.id, invoice_line_ids=lines,
            invoice_date=fields.Date.to_date((data.get('created_at') or fields.Date.today().isoformat())[:10]),
            ref=f'Magento {self.kind} {self.name}', transaction_ids=[Command.clear()],
            reversed_entry_id=original.move_id.id if original else False)
        move = self.env['account.move'].with_company(self.company_id).create(values)
        currency = move.currency_id
        if (currency.compare_amounts(move.amount_total, number(data.get('grand_total'))) or
                currency.compare_amounts(move.amount_tax, number(data.get('tax_amount')))):
            raise UserError(_('Magento and Odoo accounting totals differ. Check taxes and discounts; no document was saved.'))
        # Validate individual row taxes too: a matching grand total alone is insufficient.
        for row in data['items']:
            key = str(row['order_item_id'])
            if key in quantities:
                aml = move.invoice_line_ids.filtered(lambda line: key in line.sale_line_ids.mapped('in_magento_item_id'))
                if len(aml) != 1 or currency.compare_amounts(aml.price_total - aml.price_subtotal, number(row.get('tax_amount'))):
                    raise UserError(_('Magento and Odoo line tax totals differ.'))
        self.sudo().write({'move_id': move.id, 'source_snapshot': data, 'imported_at': fields.Datetime.now()})
        self.env['in.magento.sync.log']._record(self, direction='import', operation='accounting',
            summary=f'Created draft Odoo {self.kind} for Magento {self.name}.', previous_source='new',
            after={'document_type': self.kind, 'total': move.amount_total, 'tax': move.amount_tax, 'state': 'draft'})
        return move.id

    def action_prepare_return(self):
        """Explicit review action. Reserve return moves, never validate receipts."""
        self.ensure_one()
        self.instance_id._require_manager()
        self.instance_id._check_enabled()
        if self.kind != 'creditmemo' or not self.move_id:
            raise UserError(_('Import a credit memo before preparing its physical return.'))
        if not self.order_binding_id.try_lock_for_update():
            raise UserError(_('Another return is being prepared. Try again shortly.'))
        self.invalidate_recordset()
        if self.return_picking_ids:
            return self._return_action()
        with self.env.cr.savepoint():
            required = {str(row['order_item_id']): number(row.get('qty')) for row in self.source_snapshot['items'] if number(row.get('qty')) > 0}
            plans = defaultdict(dict)
            for line in self.order_binding_id.sale_order_id.order_line.filtered(lambda l: l.in_magento_item_id in required and l.product_id.type == 'consu'):
                remaining = required[line.in_magento_item_id]
                moves = line.move_ids.filtered(lambda m: m.state == 'done' and m.location_id.usage == 'internal' and m.location_dest_id.usage == 'customer' and not m.origin_returned_move_id).sorted('id')
                for move in moves:
                    if move.product_uom != line.product_uom_id:
                        raise UserError(_('Return UoM differs from the imported sales line. Review manually.'))
                    returned = sum(r.quantity if r.state == 'done' else r.product_uom_qty for r in move.returned_move_ids.filtered(lambda r: r.state != 'cancel'))
                    qty = min(remaining, max(0, move.quantity - returned))
                    if qty > 0:
                        plans[move.picking_id][move.id] = qty
                        remaining -= qty
                if remaining > 0.000001:
                    raise UserError(_('The refunded quantity has not been delivered or is already being returned. Review the physical return separately.'))
            if not plans:
                raise UserError(_('This credit memo has no delivered physical products to return.'))
            for picking, quantities in plans.items():
                wizard = self.env['stock.return.picking'].with_company(self.company_id).create({'picking_id': picking.id})
                for line in wizard.product_return_moves:
                    line.quantity = quantities.get(line.move_id.id, 0)
                returned = wizard._create_return()
                returned.in_magento_creditmemo_id = self
            self.env['in.magento.sync.log']._record(self, direction='import', operation='return',
                summary='Prepared physical return transfers for warehouse review; receipts are not validated.')
        return self._return_action()

    def _return_action(self):
        return {'type': 'ir.actions.act_window', 'name': _('Magento Returns'), 'res_model': 'stock.picking',
                'view_mode': 'list,form', 'domain': [('id', 'in', self.return_picking_ids.ids)]}


class MagentoReturnPicking(models.Model):
    _inherit = 'stock.picking'

    in_magento_creditmemo_id = fields.Many2one('in.magento.accounting', readonly=True, copy=False, check_company=True, ondelete='restrict')
