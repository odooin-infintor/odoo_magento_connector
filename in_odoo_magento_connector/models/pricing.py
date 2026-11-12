"""Bounded base-price updates, with per-product read-back acknowledgement."""
import hashlib
import json
import math

from odoo import fields, models, _
from odoo.exceptions import UserError
from odoo.tools.float_utils import float_compare

from ..services.client import MagentoError


class MagentoPricingInstance(models.Model):
    _inherit = 'in.magento.instance'

    batch_price_updates = fields.Boolean(default=True, string='Batch price-only updates',
        help='Automatic product synchronization groups price-only changes. Requires the Magento Catalog API permission; unsupported endpoints fall back to normal product exports.')

    def _price_store_id(self, client):
        self.ensure_one()
        if self.store_code == 'all':
            return 0
        stores = client.request('GET', 'store/storeViews')
        if not isinstance(stores, list):
            raise UserError(_('Magento returned invalid store views for price synchronization.'))
        matches = [row for row in stores if isinstance(row, dict) and row.get('code') == self.store_code]
        if len(matches) != 1 or type(matches[0].get('id')) is not int or matches[0]['id'] < 0:
            raise UserError(_('The configured export store code could not be resolved for price synchronization.'))
        return matches[0]['id']


class MagentoProductPricing(models.Model):
    _inherit = 'in.magento.product.binding'

    last_nonprice_fingerprint = fields.Char(readonly=True, copy=False)

    def _price_snapshot(self):
        self.ensure_one()
        data = self._fingerprint_data()
        full = hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest()
        price = data['product'].pop('price')
        return {'full': full, 'catalog': hashlib.sha256(json.dumps(data, sort_keys=True).encode()).hexdigest(),
                'price': price, 'type': data['product']['type_id']}

    def _catalog_change_kind(self):
        snapshot = self._price_snapshot()
        if snapshot['full'] == self.last_fingerprint:
            # Safe upgrade baseline: the complete current payload must still
            # match the previously accepted export, not merely its write date.
            if self.last_nonprice_fingerprint != snapshot['catalog']:
                self.last_nonprice_fingerprint = snapshot['catalog']
            return False
        if (self.instance_id.batch_price_updates and self.external_id and self.last_sync
                and self.last_nonprice_fingerprint == snapshot['catalog']
                and math.isfinite(snapshot['price']) and snapshot['price'] >= 0):
            return 'price'
        return 'full'

    def _job_export(self):
        result = super()._job_export()
        self.last_nonprice_fingerprint = self._price_snapshot()['catalog']
        return result

    def _queue_price_batches(self):
        for instance, records in self.grouped('instance_id').items():
            records = records.sorted('id').with_company(instance.company_id)
            for offset in range(0, len(records), 100):
                batch = records[offset:offset + 100]
                key = hashlib.sha256(','.join(map(str, batch.ids)).encode()).hexdigest()
                batch.with_delay(identity_key=f'magento:prices:{key}',
                    description=f'{instance.name}: update prices for {len(batch)} products')._job_export_prices()

    def _queue_full_price_fallback(self, *, eta=None, log=False):
        for record in self:
            record.with_company(record.company_id).with_delay(
                identity_key=f'magento:product:{record.id}', eta=eta)._job_export_if_changed()
            if log:
                self.env['in.magento.sync.log']._record(record, direction='export', operation='update', status='retry',
                    summary='Bulk price update was not acknowledged; a normal product export was queued.')

    def _price_remote_products(self, client):
        products = self._inventory_collection(client, 'products',
            [('entity_id', 'in', ','.join(self.mapped('external_id')))], len(self), 'entity_id')
        ids = set(self.mapped('external_id'))
        result = {}
        for row in products:
            if not isinstance(row, dict) or str(row.get('id')) not in ids or str(row['id']) in result:
                raise UserError(_('Magento returned unexpected product identities during price validation.'))
            result[str(row['id'])] = row
        return result

    def _job_export_prices(self):
        records = self.exists()
        if not records:
            return
        instance = records.instance_id
        instance.ensure_one()
        instance._check_enabled()
        if len(records) > 100:
            records._queue_price_batches()
            return
        locked = records.try_lock_for_update()
        (records - locked)._queue_full_price_fallback(eta=10)
        locked.invalidate_recordset()
        candidates = self.browse()
        snapshots = {}
        for record in locked.filtered(lambda row: row._publication_allowed()):
            try:
                kind = record._catalog_change_kind()
                snapshot = record._price_snapshot() if kind == 'price' else None
            except UserError:
                kind = 'full'
            if kind == 'price' and record.external_id.isdecimal():
                candidates |= record
                snapshots[record.id] = snapshot
            elif kind:
                record._queue_full_price_fallback()
        if not candidates:
            return
        with instance._client() as client:
            store_id = instance._price_store_id(client)
            before = candidates._price_remote_products(client)
            valid = candidates.filtered(lambda row: before.get(row.external_id, {}).get('sku') == row.sku
                and before.get(row.external_id, {}).get('type_id') == snapshots[row.id]['type'])
            (candidates - valid)._queue_full_price_fallback(log=True)
            if not valid:
                return
            for record in valid:
                old = before[record.external_id].get('price')
                if not isinstance(old, (int, float)) or not math.isfinite(old):
                    raise UserError(_('Magento returned an invalid base price.'))
            changed = valid.filtered(lambda row: float_compare(before[row.external_id]['price'],
                snapshots[row.id]['price'], precision_digits=4) != 0)
            after = before
            if changed:
                payload = {'prices': [{'sku': row.sku, 'store_id': store_id, 'price': snapshots[row.id]['price']} for row in changed]}
                try:
                    result = client.request('POST', 'products/base-prices', payload=payload, replay_safe=True)
                except MagentoError as error:
                    if error.status not in (400, 403, 404, 405):
                        raise
                    changed._queue_full_price_fallback(log=True)
                    valid -= changed
                    changed = self.browse()
                else:
                    # Magento returns HTTP 200 with a list of rejected rows,
                    # after applying valid rows. Never treat HTTP 200 alone as
                    # acknowledgement; verify every requested SKU's price.
                    if not isinstance(result, list):
                        raise UserError(_('Magento returned an invalid bulk price acknowledgement.'))
                    after = valid._price_remote_products(client)
            accepted = self.browse()
            for record in valid:
                remote = after.get(record.external_id, {})
                price = remote.get('price')
                if (remote.get('sku') == record.sku and remote.get('type_id') == snapshots[record.id]['type']
                        and isinstance(price, (int, float)) and math.isfinite(price)
                        and float_compare(price, snapshots[record.id]['price'], precision_digits=4) == 0):
                    accepted |= record
            (valid - accepted)._queue_full_price_fallback(log=True)
        for record in accepted:
            snapshot = snapshots[record.id]
            record.write({'last_sync': fields.Datetime.now(), 'last_fingerprint': snapshot['full'],
                          'last_nonprice_fingerprint': snapshot['catalog']})
            if record in changed:
                self.env['in.magento.sync.log']._record(record, direction='export', operation='update',
                    summary='Base price verified in Magento after bulk update.',
                    before={'price': before[record.external_id]['price']},
                    after={'price': after[record.external_id]['price']}, previous_source='magento')
