"""Update only the connector-owned tracking row on an acknowledged shipment."""
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError
from odoo.addons.queue_job.exception import RetryableJobError


class MagentoTrackingInstance(models.Model):
    _inherit = 'in.magento.instance'

    auto_export_tracking = fields.Boolean(default=False)
    last_tracking_schedule = fields.Datetime(readonly=True, copy=False)

    @api.model
    def _cron_schedule(self):
        super()._cron_schedule()
        if str(self.env['ir.config_parameter'].sudo().get_param('database.is_neutralized', 'false')).lower() in ('true', '1'):
            return
        now = fields.Datetime.now()
        for instance in self.search([('enabled', '=', True), ('auto_export_tracking', '=', True)]):
            if instance.last_tracking_schedule and instance.last_tracking_schedule > now - timedelta(minutes=instance.fulfillment_interval):
                continue
            instance.shipment_ids.filtered('external_id').with_context(magento_sync_trigger='automatic').action_sync_tracking()
            instance.last_tracking_schedule = now


class MagentoShipmentTracking(models.Model):
    _inherit = 'in.magento.shipment'

    tracking_external_id = fields.Char(readonly=True, copy=False)
    tracking_snapshot = fields.Json(readonly=True, copy=False)
    tracking_synced_at = fields.Datetime(readonly=True, copy=False)

    def _tracking_values(self):
        self.ensure_one()
        picking = self.picking_id
        number = picking.in_magento_tracking_number or (picking.carrier_tracking_ref if 'carrier_tracking_ref' in picking._fields else False)
        return {'track_number': number.strip(), 'title': picking.in_magento_tracking_title or 'Odoo delivery',
                'carrier_code': 'custom'} if number and number.strip() else False

    @staticmethod
    def _track_values(track):
        return {key: track.get(key) for key in ('track_number', 'title', 'carrier_code')}

    def action_sync_tracking(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            if not record.external_id:
                raise UserError(_('Synchronize the shipment before updating tracking.'))
            record.with_company(record.company_id).with_delay(identity_key=f'magento:tracking:{record.id}')._job_export_tracking()
        return True

    def _job_export_tracking(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another shipment/tracking update is running.', seconds=10)
        self.invalidate_recordset()
        if not self.external_id:
            raise RetryableJobError('Waiting for shipment synchronization.', seconds=30)
        desired = self._tracking_values()
        previous = self.tracking_snapshot or False
        if desired == previous and (self.tracking_external_id or not desired):
            return
        with self.instance_id._client() as client:
            shipment = client.request('GET', f'shipment/{self.external_id}')
            if str(shipment.get('entity_id')) != self.external_id or str(shipment.get('order_id')) != self.order_binding_id.external_id:
                raise UserError(_('Magento shipment identity changed. Reconcile before retrying.'))
            tracks = shipment.get('tracks') or []
            owned = [row for row in tracks if str(row.get('entity_id')) == self.tracking_external_id] if self.tracking_external_id else []
            if not self.tracking_external_id:
                # Recover an initial shipment track, or a lost response to a
                # track creation, only when the exact values identify one row.
                candidates = [row for row in tracks if self._track_values(row) == (previous or desired)]
                if len(candidates) > 1:
                    raise UserError(_('Multiple Magento tracking rows match. Reconcile before retrying.'))
                owned = candidates
                if previous and not owned:
                    raise UserError(_('The original shipment tracking changed in Magento. Reconcile before retrying.'))
            if self.tracking_external_id and not owned and desired:
                raise UserError(_('The mapped Magento tracking row is missing. Reconcile before retrying.'))
            old = self._track_values(owned[0]) if owned else False
            if owned and previous and old not in (previous, desired):
                raise UserError(_('Tracking was changed in Magento. Reconcile before overwriting it.'))
            if owned and (str(owned[0].get('parent_id')) != self.external_id or
                          str(owned[0].get('order_id')) != self.order_binding_id.external_id):
                raise UserError(_('Tracking belongs to a different shipment/order.'))
            if not desired:
                if owned:
                    if client.request('DELETE', f"shipment/track/{owned[0]['entity_id']}", replay_safe=True) is not True:
                        raise UserError(_('Magento did not confirm tracking removal. Reconcile before retrying.'))
                remote_id = False
            elif old == desired:
                remote_id = str(owned[0]['entity_id'])
            else:
                entity = dict(desired, parent_id=int(self.external_id), order_id=int(self.order_binding_id.external_id))
                if owned:
                    entity['entity_id'] = int(owned[0]['entity_id'])
                result = client.request('POST', 'shipment/track', payload={'entity': entity}, replay_safe=bool(owned))
                if (not isinstance(result, dict) or not result.get('entity_id') or self._track_values(result) != desired or
                        str(result.get('parent_id')) != self.external_id or
                        str(result.get('order_id')) != self.order_binding_id.external_id or
                        (owned and str(result['entity_id']) != str(owned[0]['entity_id']))):
                    raise UserError(_('Unexpected tracking response. Reconcile before retrying.'))
                remote_id = str(result['entity_id'])
        self.sudo().write({'tracking_external_id': remote_id, 'tracking_snapshot': desired,
                          'tracking_synced_at': fields.Datetime.now()})
        if desired != previous:
            self.env['in.magento.sync.log']._record(self, direction='export', operation='tracking',
                summary='Shipment tracking synchronized with Magento.',
                before=previous or {}, after=desired or {'track_number': False}, previous_source='magento')


class MagentoPickingTracking(models.Model):
    _inherit = 'stock.picking'

    def action_sync_magento_tracking(self):
        shipments = self.env['in.magento.shipment'].search([('picking_id', 'in', self.ids), ('external_id', '!=', False)])
        if not shipments:
            raise UserError(_('No synchronized Magento shipment exists for this delivery.'))
        return shipments.action_sync_tracking()
