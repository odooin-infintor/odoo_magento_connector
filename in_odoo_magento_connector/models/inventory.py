import hashlib
import math
from urllib.parse import quote

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.client import MagentoError, search_params


class MagentoInventorySource(models.Model):
    _name = 'in.magento.inventory.source'
    _description = 'Magento Warehouse Source Mapping'
    _check_company_auto = True
    _rec_name = 'source_code'

    instance_id = fields.Many2one('in.magento.instance', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='instance_id.company_id', store=True)
    warehouse_id = fields.Many2one('stock.warehouse', required=True, check_company=True)
    location_id = fields.Many2one(related='warehouse_id.lot_stock_id', string='Stock location')
    source_code = fields.Char(required=True, help='Exact Magento MSI source code; e.g. default.')
    active = fields.Boolean(default=True)
    _source_unique = models.Constraint('UNIQUE(instance_id, source_code)', 'Map each Magento source once per connection.')
    _warehouse_unique = models.Constraint('UNIQUE(instance_id, warehouse_id)', 'Map each warehouse once per connection.')

    @api.constrains('source_code', 'warehouse_id', 'instance_id', 'active')
    def _check_source(self):
        for record in self:
            if not record.source_code or record.source_code != record.source_code.strip():
                raise ValidationError(_('Use the exact Magento source code without surrounding whitespace.'))
            if record.warehouse_id.company_id != record.company_id:
                raise ValidationError(_('The warehouse and Magento connection must belong to the same company.'))
            others = self.search([('instance_id', '=', record.instance_id.id), ('id', '!=', record.id)])
            for other in others:
                a, b = record.location_id.parent_path or '', other.location_id.parent_path or ''
                if record.active and a and b and (a.startswith(b) or b.startswith(a)):
                    raise ValidationError(_('Mapped warehouse stock locations cannot overlap.'))

    def action_test_source(self):
        for record in self:
            record.instance_id._require_manager()
            with record.instance_id._client() as client:
                source = client.request('GET', 'inventory/sources/' + quote(record.source_code, safe=''))
                if source.get('source_code') != record.source_code or not source.get('enabled'):
                    raise UserError(_('The Magento source is missing or disabled.'))
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Inventory source verified'), 'type': 'success', 'sticky': False}}


class MagentoProductInventory(models.Model):
    _inherit = 'in.magento.product.binding'

    sync_inventory = fields.Boolean(string='Synchronize inventory', default=False,
        help='Export physical on-hand stock to explicitly mapped Magento sources. Magento reservations are not changed.')
    last_inventory_sync = fields.Datetime(readonly=True, copy=False)

    def action_export_inventory(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            if not record._publication_allowed():
                raise UserError(_('Republish this Magento product before exporting inventory.'))
            if not record.sync_inventory:
                raise UserError(_('Enable Synchronize Inventory on the product mapping first.'))
            if not record.external_id:
                raise UserError(_('Publish the product before exporting inventory.'))
            if not record.product_id.is_storable:
                raise UserError(_('Only products with inventory tracking can export stock.'))
            if not record.instance_id.inventory_source_ids:
                raise UserError(_('Configure at least one warehouse/source mapping first.'))
        self._queue_inventory_batches()
        return True

    def _queue_inventory_batches(self):
        for instance, records in self.grouped('instance_id').items():
            size = max(1, 100 // max(1, len(instance.inventory_source_ids.filtered('active'))))
            batch = self.browse()
            length = 0
            for record in records.sorted('id').with_company(instance.company_id):
                # Magento's comma-separated "in" filter cannot represent a
                # literal comma in a SKU. Such products use the existing job.
                encoded_length = len(quote(record.sku, safe=''))
                if ',' in record.sku or encoded_length > 3000:
                    record._queue_inventory_single()
                    continue
                if batch and (len(batch) >= size or length + encoded_length > 3000):
                    batch._queue_inventory_batch()
                    batch, length = self.browse(), 0
                batch |= record
                length += encoded_length + 3
            batch._queue_inventory_batch()

    def _queue_inventory_single(self, eta=None):
        for record in self:
            record.with_company(record.company_id).with_delay(
                identity_key=f'magento:inventory:{record.id}', eta=eta)._job_export_inventory()

    def _queue_inventory_batch(self):
        if not self:
            return
        if len(self) == 1:
            self._queue_inventory_single()
            return
        key = hashlib.sha256(','.join(map(str, sorted(self.ids))).encode()).hexdigest()
        self.with_company(self.instance_id.company_id).with_delay(
            identity_key=f'magento:inventory-batch:{key}',
            description=f'{self.instance_id.name}: synchronize stock for {len(self)} products',
        )._job_export_inventory_batch()

    def _inventory_items(self):
        self.ensure_one()
        return self._inventory_payloads()[self.id]

    def _inventory_payloads(self):
        instance = self.instance_id
        instance.ensure_one()
        if any(not row.product_id.is_storable or row.product_id.type != 'consu' for row in self):
            raise UserError(_('Inventory export requires a tracked physical product.'))
        sources = instance.inventory_source_ids.filtered('active')
        if not sources:
            raise UserError(_('Configure at least one warehouse/source mapping first.'))
        items = {record.id: [] for record in self}
        for source in sources:
            # Physical company-owned stock only: no supplier/customer locations,
            # consigned stock, forecast receipts or reservation subtraction.
            totals = self.env['stock.quant']._read_group([
                ('product_id', 'in', self.product_id.ids), ('company_id', '=', instance.company_id.id),
                ('location_id', 'child_of', source.location_id.id), ('location_id.usage', '=', 'internal'),
                ('owner_id', '=', False),
            ], ['product_id'], ['quantity:sum'])
            quantities = dict(totals)
            for record in self:
                product = record.product_id
                quantity = float(quantities.get(product, 0))
                if not math.isfinite(quantity):
                    raise UserError(_('Inventory quantity must be finite.'))
                quantity = max(0.0, quantity) if product.active and product.sale_ok else 0.0
                items[record.id].append({'sku': record.sku, 'source_code': source.source_code,
                                       'quantity': quantity, 'status': int(quantity > 0)})
        return items

    @api.model
    def _inventory_collection(self, client, path, filters, limit, sort):
        """Read bounded collections completely, including server-side page caps."""
        rows, page, total = [], 1, None
        while True:
            response = client.request('GET', path, params=search_params(filters, page=page, size=limit, sort=sort))
            if (not isinstance(response, dict) or not isinstance(response.get('items'), list)
                    or type(response.get('total_count')) is not int):
                raise UserError(_('Magento returned an invalid inventory validation collection.'))
            count = response['total_count']
            if count < 0 or count > limit or (total is not None and total != count):
                raise UserError(_('Magento inventory validation pagination changed or exceeded its scope.'))
            total = count
            rows.extend(response['items'])
            if len(rows) > total or (len(rows) < total and not response['items']):
                raise UserError(_('Magento returned incomplete inventory validation data.'))
            if len(rows) == total:
                return rows
            page += 1

    def _job_export_inventory_batch(self):
        records = self.exists()
        if not records:
            return
        instance = records.instance_id
        instance.ensure_one()
        instance._check_enabled()
        instance._lock_stock_sync()
        locked = records.try_lock_for_update()
        (records - locked)._queue_inventory_single(eta=10)
        locked.invalidate_recordset()
        records = locked.filtered(lambda row: row.sync_inventory and row._publication_allowed())
        waiting = records.filtered(lambda row: not row.external_id)
        waiting._queue_inventory_single(eta=30)
        records -= waiting
        blocked = records._inventory_blocked_bindings()
        blocked._queue_inventory_single(eta=60)
        records -= blocked
        if not records:
            return
        sources = instance.inventory_source_ids.filtered('active')
        if len(records) * len(sources) > 100:
            records._queue_inventory_batches()
            return
        invalid = records.filtered(lambda row: not row.external_id.isdecimal()
            or not row.product_id.is_storable or row.product_id.type != 'consu' or ',' in row.sku)
        invalid._queue_inventory_single()
        records -= invalid
        if not records:
            return
        payloads = records._inventory_payloads()
        expected = {row.sku: row for row in records}
        before = {row.id: {} for row in records}
        after = {row.id: {} for row in records}
        changed = []
        with instance._client() as client:
            products = records._inventory_collection(client, 'products',
                [('entity_id', 'in', ','.join(records.mapped('external_id')))], len(records), 'entity_id')
            remote = {}
            ids = set(records.mapped('external_id'))
            for product in products:
                if (not isinstance(product, dict) or str(product.get('id')) not in ids
                        or str(product['id']) in remote):
                    raise UserError(_('Magento returned unexpected product identities during inventory validation.'))
                remote[str(product['id'])] = product
            mismatched = records.filtered(lambda row: remote.get(row.external_id, {}).get('sku') != row.sku
                or remote.get(row.external_id, {}).get('type_id') != 'simple')
            # Quarantine individual bad mappings using the existing diagnostic
            # job; healthy products in the batch can still synchronize.
            mismatched._queue_inventory_single()
            records -= mismatched
            if not records:
                return
            expected = {row.sku: row for row in records}
            stock = {}
            for source in sources:
                details = client.request('GET', 'inventory/sources/' + quote(source.source_code, safe=''))
                if details.get('source_code') != source.source_code or not details.get('enabled'):
                    raise UserError(_('A configured Magento inventory source is missing or disabled.'))
                rows = records._inventory_collection(client, 'inventory/source-items', [
                    ('sku', 'in', ','.join(expected)), ('source_code', 'eq', source.source_code)], len(records), 'sku')
                for row in rows:
                    if not isinstance(row, dict):
                        raise UserError(_('Magento returned unexpected inventory source items.'))
                    key = (row.get('sku'), row.get('source_code'))
                    if key[0] not in expected or key[1] != source.source_code or key in stock:
                        raise UserError(_('Magento returned unexpected inventory source items.'))
                    quantity = row.get('quantity')
                    if (not isinstance(quantity, (int, float)) or not math.isfinite(quantity)
                            or row.get('status') not in (0, 1)):
                        raise UserError(_('Magento returned invalid inventory quantities or status.'))
                    stock[key] = {key: row[key] for key in ('quantity', 'status')}
            for record in records:
                for item in payloads[record.id]:
                    old = stock.get((record.sku, item['source_code']))
                    new = {key: item[key] for key in ('quantity', 'status')}
                    if old != new:
                        changed.append(item)
                        if old is not None:
                            before[record.id][item['source_code']] = old
                        after[record.id][item['source_code']] = new
            if changed:
                try:
                    client.request('POST', 'inventory/source-items', payload={'sourceItems': changed}, replay_safe=True)
                except MagentoError as error:
                    if error.status != 400:
                        raise
                    # A validation rejection may concern just one SKU. Re-read
                    # and retry each product separately, including uncertain
                    # partial writes; never claim the batch was accepted.
                    retry = records.filtered(lambda row: bool(after[row.id]))
                    retry._queue_inventory_single()
                    for record in retry:
                        self.env['in.magento.sync.log']._record(record, direction='export', operation='inventory',
                            status='retry', summary='Magento rejected the stock batch; individual product checks queued.')
                    return
        records.last_inventory_sync = fields.Datetime.now()
        for record in records.filtered(lambda row: bool(after[row.id])):
            self.env['in.magento.sync.log']._record(record, direction='export', operation='inventory',
                summary='Warehouse on-hand quantities accepted by Magento in a stock batch.',
                before=before[record.id], after=after[record.id], previous_source='magento')

    def _job_export_inventory(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.sync_inventory or not self._publication_allowed():
            return
        if not self.external_id:
            raise RetryableJobError('Waiting for product publication before inventory export.', seconds=30)
        if not self.try_lock_for_update():
            raise RetryableJobError('Another product or inventory export is running.', seconds=10)
        self.invalidate_recordset()
        if not self._publication_allowed():
            return
        items = self._inventory_items()
        before, after = {}, {}
        changed = []
        with self.instance_id._client() as client:
            remote_product = client.request('GET', 'products/' + quote(self.sku, safe=''))
            if str(remote_product.get('id')) != self.external_id or remote_product.get('type_id') != 'simple':
                raise UserError(_('The remote product mapping changed. Reconcile it before inventory export.'))
            # Query each explicitly mapped source to avoid truncating a large
            # multi-source response or ever altering an unrelated source.
            for item in items:
                source = client.request('GET', 'inventory/sources/' + quote(item['source_code'], safe=''))
                if source.get('source_code') != item['source_code'] or not source.get('enabled'):
                    raise UserError(_('A configured Magento inventory source is missing or disabled.'))
                response = client.request('GET', 'inventory/source-items', params=search_params([
                    ('sku', 'eq', self.sku), ('source_code', 'eq', item['source_code'])], size=2, sort='source_code'))
                if not isinstance(response, dict) or not isinstance(response.get('items'), list):
                    raise UserError(_('Magento returned an invalid inventory collection.'))
                matches = response['items']
                if len(matches) > 1 or any(row.get('sku') != self.sku or row.get('source_code') != item['source_code'] for row in matches):
                    raise UserError(_('Magento returned unexpected inventory source items.'))
                old = {k: matches[0][k] for k in ('quantity', 'status')} if matches else None
                new = {k: item[k] for k in ('quantity', 'status')}
                if old != new:
                    changed.append(item)
                    if old is not None:
                        before[item['source_code']] = old
                    after[item['source_code']] = new
            if changed:
                # Absolute source quantities make this POST replay-safe. It
                # never inserts or adjusts inventory reservations.
                client.request('POST', 'inventory/source-items', payload={'sourceItems': changed}, replay_safe=True)
        self.last_inventory_sync = fields.Datetime.now()
        if changed:
            self.env['in.magento.sync.log']._record(self, direction='export', operation='inventory',
                summary='Warehouse on-hand quantities accepted by Magento.', before=before, after=after,
                previous_source='magento')
