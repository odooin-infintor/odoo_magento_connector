"""Odoo deliveries -> Magento shipments; guarded whole-order cancellations."""
import uuid
from collections import defaultdict

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.client import search_params
from .order import number


class MagentoFulfillmentInstance(models.Model):
    _inherit = 'in.magento.instance'

    auto_export_shipments = fields.Boolean(default=False)
    auto_export_cancellations = fields.Boolean(default=False)
    auto_import_cancellations = fields.Boolean(default=False)
    fulfillment_interval = fields.Integer(default=5, string='Fulfillment interval (minutes)')
    last_fulfillment_schedule = fields.Datetime(readonly=True, copy=False)
    shipment_ids = fields.One2many('in.magento.shipment', 'instance_id')

    @api.constrains('fulfillment_interval')
    def _check_fulfillment_interval(self):
        if any(record.fulfillment_interval < 1 for record in self):
            raise ValidationError(_('Fulfillment interval must be at least one minute.'))

    def _lock_stock_sync(self):
        self.ensure_one()
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s, %s)', [527020, self.id])
        if not self.env.cr.fetchone()[0]:
            raise RetryableJobError('Another shipment or inventory synchronization is running.', seconds=10)

    def action_sync_fulfillment(self):
        self._require_manager()
        for instance in self:
            instance._check_enabled()
            instance.order_ids.filtered('sale_order_id').action_sync_fulfillment()
        return True

    @api.model
    def _cron_schedule(self):
        from datetime import timedelta
        super()._cron_schedule()
        if str(self.env['ir.config_parameter'].sudo().get_param('database.is_neutralized', 'false')).lower() in ('true', '1'):
            return
        now = fields.Datetime.now()
        for instance in self.search([('enabled', '=', True)]):
            if not (instance.auto_export_shipments or instance.auto_export_cancellations or instance.auto_import_cancellations):
                continue
            if instance.last_fulfillment_schedule and instance.last_fulfillment_schedule > now - timedelta(minutes=instance.fulfillment_interval):
                continue
            for order in instance.order_ids.filtered(lambda row: row.sale_order_id and not row.cancellation_synced_at):
                order.with_company(instance.company_id).with_context(magento_sync_trigger='automatic').with_delay(
                    identity_key=f'magento:fulfillment:{order.id}')._job_sync_fulfillment(
                        ship=instance.auto_export_shipments, export_cancel=instance.auto_export_cancellations,
                        import_cancel=instance.auto_import_cancellations)
            instance.last_fulfillment_schedule = now


class MagentoFulfillmentOrder(models.Model):
    _inherit = 'in.magento.order.binding'

    shipment_ids = fields.One2many('in.magento.shipment', 'order_binding_id')
    remote_state = fields.Char(readonly=True)
    cancellation_synced_at = fields.Datetime(readonly=True, copy=False)

    def action_sync_fulfillment(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            if not record.sale_order_id:
                raise UserError(_('Import the order before synchronizing fulfillment.'))
            record.with_company(record.company_id).with_context(magento_sync_trigger='manual').with_delay(
                identity_key=f'magento:fulfillment:{record.id}')._job_sync_fulfillment()
        return True

    def _remote_order(self, client):
        self.ensure_one()
        remote = client.request('GET', f'orders/{self.external_id}')
        if str(remote.get('entity_id')) != self.external_id or str(remote.get('store_id')) != str(self.payload.get('store_id')):
            raise UserError(_('The Magento order identity or store changed. Reconcile before retrying.'))
        return remote

    def _check_local_cancellation(self):
        order = self.sale_order_id
        if order.invoice_ids or order.picking_ids.filtered(lambda row: row.state == 'done') or self.shipment_ids.filtered('external_id'):
            raise UserError(_('Orders with deliveries or invoices need a return/refund review; automatic cancellation is blocked.'))

    def _check_remote_cancellation(self, remote):
        if remote.get('state') in ('complete', 'closed') or number(remote.get('total_paid')) or any(
                number(item.get(key)) for item in remote.get('items', []) for key in ('qty_shipped', 'qty_invoiced', 'qty_refunded')):
            raise UserError(_('Magento has processed this order. Review its payment, delivery or refund before cancellation.'))
        if remote.get('state') != 'canceled' and any(number(item.get('qty_canceled')) for item in remote.get('items', [])):
            raise UserError(_('Partial Magento cancellations require review.'))

    def _ensure_item_links(self):
        """Backfill earlier imports only when each SKU identifies one line."""
        self.ensure_one()
        items = self._normalized_items(self.payload)
        for item in items:
            if self.sale_order_id.order_line.filtered(lambda line: line.in_magento_item_id == str(item['item_id'])):
                continue
            binding = self.instance_id.product_ids.filtered(lambda row: row.sku == item.get('sku'))
            candidates = self.sale_order_id.order_line.filtered(lambda line: not line.display_type and
                not line.in_magento_item_id and line.product_id == binding.product_id)
            if len(candidates) != 1 or sum(row.get('sku') == item.get('sku') for row in items) != 1:
                raise UserError(_('An older imported order has ambiguous item links. Map its order-item IDs before shipping.'))
            candidates.write({'in_magento_item_id': str(item['item_id']), 'in_magento_sku': item['sku']})

    def _prepare_shipments(self, pickings=None):
        self.ensure_one()
        self._ensure_item_links()
        Shipment = self.env['in.magento.shipment']
        deliveries = self.sale_order_id.picking_ids if pickings is None else pickings
        if deliveries - self.sale_order_id.picking_ids:
            raise UserError(_('The delivery does not belong to this Magento order.'))
        for picking in deliveries.filtered(lambda row: row.state == 'done' and row.picking_type_id.code == 'outgoing'):
            record = Shipment.search([('picking_id', '=', picking.id)], limit=1)
            if not record:
                record = Shipment.sudo().create({'order_binding_id': self.id, 'picking_id': picking.id}).with_env(self.env)
            if not record.external_id:
                record.with_delay(identity_key=f'magento:shipment:{record.id}')._job_export_shipment()

    def _job_sync_fulfillment(self, ship=True, export_cancel=True, import_cancel=True):
        self.ensure_one()
        self.instance_id._check_enabled()
        self.instance_id._lock_stock_sync()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another order synchronization is running.', seconds=10)
        self.invalidate_recordset()
        if not self.sale_order_id:
            return
        with self.instance_id._client() as client:
            remote = self._remote_order(client)
            self.write({'remote_state': remote.get('state'), 'remote_status': remote.get('status')})
            if remote.get('state') == 'canceled':
                self._check_local_cancellation()
                self._check_remote_cancellation(remote)
                if self.sale_order_id.state != 'cancel':
                    if not import_cancel:
                        return
                    previous_state = self.sale_order_id.state
                    self.sale_order_id.action_cancel()
                    self.env['in.magento.sync.log']._record(self, direction='import', operation='cancellation',
                        summary='Magento cancellation applied to Odoo.', before={'state': previous_state}, after={'state': 'cancel'}, previous_source='magento')
                elif not self.cancellation_synced_at:
                    self.env['in.magento.sync.log']._record(self, direction='import', operation='cancellation',
                        summary='Cancellation confirmed in both systems.', before={'state': 'canceled'}, after={'state': 'canceled'}, previous_source='magento')
                self.cancellation_synced_at = fields.Datetime.now()
                return
            if self.sale_order_id.state == 'cancel':
                if not export_cancel:
                    return
                self._check_local_cancellation()
                self._check_remote_cancellation(remote)
                if client.request('POST', f'orders/{self.external_id}/cancel') is not True:
                    raise UserError(_('Magento refused cancellation. Review the order before retrying.'))
                self.write({'remote_state': 'canceled', 'remote_status': 'canceled', 'cancellation_synced_at': fields.Datetime.now()})
                self.env['in.magento.sync.log']._record(self, direction='export', operation='cancellation',
                    summary='Odoo cancellation accepted by Magento.', before={'state': remote.get('state')}, after={'state': 'canceled'}, previous_source='magento')
                return
        if ship and self.sale_order_id.state == 'sale':
            self._prepare_shipments()


class MagentoShipment(models.Model):
    _name = 'in.magento.shipment'
    _inherit = 'in.magento.sync.mixin'
    _description = 'Magento Shipment Mapping'
    _check_company_auto = True
    _rec_name = 'picking_id'

    order_binding_id = fields.Many2one('in.magento.order.binding', required=True, ondelete='restrict')
    instance_id = fields.Many2one(related='order_binding_id.instance_id', store=True)
    company_id = fields.Many2one(related='order_binding_id.company_id', store=True)
    picking_id = fields.Many2one('stock.picking', required=True, check_company=True, ondelete='restrict')
    external_id = fields.Char(readonly=True, copy=False)
    request_key = fields.Char(default=lambda self: str(uuid.uuid4()), required=True, readonly=True, copy=False)
    source_code = fields.Char(readonly=True)
    exported_at = fields.Datetime(readonly=True)
    items = fields.Json(readonly=True, copy=False)
    _picking_unique = models.Constraint('UNIQUE(picking_id)', 'This delivery already has a Magento shipment mapping.')
    _shipment_unique = models.Constraint('UNIQUE(instance_id, external_id)', 'This Magento shipment is already mapped.')

    @api.constrains('order_binding_id', 'picking_id')
    def _check_picking(self):
        for record in self:
            orders = record.picking_id.move_ids.sale_line_id.order_id
            if orders != record.order_binding_id.sale_order_id:
                raise ValidationError(_('A Magento shipment must contain deliveries for exactly its mapped Odoo order.'))

    def action_export(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            record.with_company(record.company_id).with_delay(identity_key=f'magento:shipment:{record.id}')._job_export_shipment()
        return True

    def _shipment_payload(self):
        self.ensure_one()
        picking = self.picking_id
        if picking.state != 'done' or picking.picking_type_id.code != 'outgoing' or picking.location_dest_id.usage != 'customer':
            raise UserError(_('Only completed outgoing customer deliveries can be exported as shipments.'))
        source = self.instance_id.inventory_source_ids.filtered(lambda row: row.active and row.warehouse_id == picking.picking_type_id.warehouse_id)
        if len(source) != 1:
            raise UserError(_('Map the delivery warehouse to exactly one active Magento inventory source.'))
        quantities = defaultdict(float)
        for move in picking.move_ids.filtered(lambda row: row.state == 'done' and row.quantity > 0):
            line = move.sale_line_id
            if (not line.in_magento_item_id or line.order_id != self.order_binding_id.sale_order_id or
                    move.product_id != line.product_id or move.location_id.usage != 'internal' or move.location_dest_id.usage != 'customer' or
                    move.origin_returned_move_id or move.move_line_ids.filtered('owner_id')):
                raise UserError(_('Shipment contains an unmapped item, return, dropship or consigned stock. Review it before export.'))
            quantities[int(line.in_magento_item_id)] += move.product_uom._compute_quantity(move.quantity, line.product_id.uom_id, round=False)
        if not quantities:
            raise UserError(_('The delivery has no shipped Magento items.'))
        payload = {'items': [{'order_item_id': key, 'qty': value} for key, value in sorted(quantities.items())],
            'notify': False, 'appendComment': True,
            'comment': {'comment': f'Odoo shipment {self.request_key}', 'is_visible_on_front': 0},
            'arguments': {'extension_attributes': {'source_code': source.source_code}}}
        tracking = picking.in_magento_tracking_number or (picking.carrier_tracking_ref if 'carrier_tracking_ref' in picking._fields else False)
        if tracking:
            payload['tracks'] = [{'track_number': tracking, 'title': picking.in_magento_tracking_title or 'Odoo delivery', 'carrier_code': 'custom'}]
        return payload

    def _find_remote_shipment(self, client, payload):
        page, matches = 1, []
        while True:
            result = client.request('GET', 'shipments', params=search_params([
                ('order_id', 'eq', self.order_binding_id.external_id)], page=page, size=100))
            rows = result.get('items', [])
            for row in rows:
                if str(row.get('order_id')) != self.order_binding_id.external_id:
                    raise UserError(_('Magento returned a shipment for an unexpected order.'))
                comments = client.request('GET', f"shipment/{row['entity_id']}/comments").get('items', [])
                if any(comment.get('comment') == payload['comment']['comment'] for comment in comments):
                    matches.append(row)
            if page * 100 >= int(result.get('total_count', len(rows))):
                break
            if not rows:
                raise UserError(_('Magento returned an incomplete shipment collection.'))
            page += 1
        if len(matches) > 1:
            raise UserError(_('Multiple Magento shipments contain this delivery reference. Reconcile before retrying.'))
        if matches:
            match = matches[0]
            actual = {int(row['order_item_id']): number(row['qty']) for row in match.get('items', []) if number(row.get('qty'))}
            expected = {row['order_item_id']: row['qty'] for row in payload['items']}
            if actual != expected or (match.get('extension_attributes') or {}).get('source_code') != payload['arguments']['extension_attributes']['source_code']:
                raise UserError(_('The recovered shipment quantities or source differ. Reconcile before retrying.'))
            return str(match['entity_id'])
        return False

    def _job_export_shipment(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        self.instance_id._lock_stock_sync()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another shipment export is running.', seconds=10)
        self.invalidate_recordset()
        if self.external_id:
            return
        payload = self._shipment_payload()
        with self.instance_id._client() as client:
            remote_id = self._find_remote_shipment(client, payload)
            if not remote_id:
                remote = self.order_binding_id._remote_order(client)
                if remote.get('state') in ('canceled', 'closed', 'holded', 'complete'):
                    raise UserError(_('Magento order is canceled, closed, on hold or complete. Review before shipment.'))
                items = {int(row['item_id']): row for row in remote.get('items', [])}
                acknowledged = defaultdict(float)
                for shipment in self.order_binding_id.shipment_ids.filtered('external_id'):
                    for item in shipment.items or []:
                        acknowledged[int(item['order_item_id'])] += number(item['qty'])
                for item in payload['items']:
                    remote_item = items.get(item['order_item_id'])
                    line = self.order_binding_id.sale_order_id.order_line.filtered(lambda row: row.in_magento_item_id == str(item['order_item_id']))
                    if not remote_item or len(line) != 1 or remote_item.get('sku') != line.in_magento_sku:
                        raise UserError(_('Magento shipment item identity changed. Reconcile before retrying.'))
                    remaining = number(remote_item.get('qty_ordered')) - number(remote_item.get('qty_shipped')) - number(remote_item.get('qty_canceled')) - number(remote_item.get('qty_refunded'))
                    if item['qty'] <= 0 or item['qty'] > remaining + 0.000001:
                        raise UserError(_('Delivered quantity exceeds Magento remaining quantity. Reconcile before retrying.'))
                    if number(remote_item.get('qty_canceled')) or number(remote_item.get('qty_refunded')):
                        raise UserError(_('Partially canceled or refunded Magento items require review before shipping.'))
                    if abs(number(remote_item.get('qty_shipped')) - acknowledged[item['order_item_id']]) > 0.000001:
                        raise UserError(_('Magento has shipments not acknowledged by Odoo. Reconcile before retrying.'))
                remote_id = client.request('POST', f'order/{self.order_binding_id.external_id}/ship', payload=payload)
                if isinstance(remote_id, bool) or not str(remote_id).isdigit() or int(remote_id) <= 0:
                    raise UserError(_('Magento returned no shipment ID. Reconcile before retrying.'))
        self.sudo().write({'external_id': str(remote_id), 'source_code': payload['arguments']['extension_attributes']['source_code'],
            'items': payload['items'], 'tracking_snapshot': (payload.get('tracks') or [False])[0], 'exported_at': fields.Datetime.now()})
        self.env['in.magento.sync.log']._record(self, direction='export', operation='shipment',
            summary='Odoo delivery recorded as Magento shipment.', after={'shipment_id': str(remote_id),
                'delivery': self.picking_id.name, 'source': self.source_code, 'items': payload['items']})
        # Magento deducted source quantity and compensated its reservation.
        # Refresh absolute Odoo stock afterwards, in a separate transaction.
        products = self.instance_id.product_ids.filtered(lambda row: row.product_id in self.picking_id.move_ids.product_id and row.sync_inventory)
        for product in products:
            product.with_delay(identity_key=f'magento:inventory:{product.id}')._job_export_inventory()


class MagentoFulfillmentInventory(models.Model):
    _inherit = 'in.magento.product.binding'

    def _job_export_inventory(self):
        self.ensure_one()
        self.instance_id._lock_stock_sync()
        if self._inventory_blocked_bindings():
            raise RetryableJobError('Waiting for completed Odoo deliveries to synchronize with Magento before replacing physical stock.', seconds=60)
        return super()._job_export_inventory()

    def _inventory_blocked_bindings(self):
        if not self:
            return self
        self.instance_id.ensure_one()
        moves = self.env['stock.move'].search([('product_id', 'in', self.product_id.ids), ('state', '=', 'done'),
            ('quantity', '>', 0), ('picking_id.picking_type_id.code', '=', 'outgoing'), ('location_dest_id.usage', '=', 'customer'),
            ('sale_line_id.order_id', 'in', self.instance_id.order_ids.sale_order_id.ids)])
        acknowledged = self.env['in.magento.shipment'].search([('instance_id', '=', self.instance_id.id),
            ('external_id', '!=', False), ('picking_id', 'in', moves.picking_id.ids)]).picking_id
        blocked = moves.filtered(lambda move: move.picking_id not in acknowledged).product_id
        return self.filtered(lambda row: row.product_id in blocked)


class MagentoFulfillmentSaleLine(models.Model):
    _inherit = 'sale.order.line'

    in_magento_item_id = fields.Char(readonly=True, copy=False)
    in_magento_sku = fields.Char(readonly=True, copy=False)


class MagentoFulfillmentPicking(models.Model):
    _inherit = 'stock.picking'

    in_magento_tracking_number = fields.Char(string='Magento tracking number', copy=False)
    in_magento_tracking_title = fields.Char(string='Magento carrier title', copy=False)
    in_magento_order_binding_id = fields.Many2one('in.magento.order.binding', compute='_compute_magento_order', groups='in_odoo_magento_connector.group_magento_user')
    in_magento_shipment_ids = fields.One2many('in.magento.shipment', 'picking_id', groups='in_odoo_magento_connector.group_magento_user')

    @api.depends('move_ids.sale_line_id.order_id')
    def _compute_magento_order(self):
        for picking in self:
            orders = picking.move_ids.sale_line_id.order_id
            picking.in_magento_order_binding_id = self.env['in.magento.order.binding'].search([
                ('sale_order_id', 'in', orders.ids)], limit=1) if len(orders) == 1 else False

    def action_sync_magento_shipment(self):
        for picking in self:
            binding = self.env['in.magento.order.binding'].search([('sale_order_id', 'in', picking.move_ids.sale_line_id.order_id.ids)])
            if len(binding) != 1 or picking.state != 'done':
                raise UserError(_('Select a completed delivery for one imported Magento order.'))
            binding.instance_id._require_manager()
            binding.instance_id._check_enabled()
            binding._prepare_shipments(pickings=picking)
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Magento shipment synchronization queued'), 'type': 'success', 'sticky': False,
            'message': _('Check Magento Shipments, Sync Logs or Background Jobs for completion.')}}
