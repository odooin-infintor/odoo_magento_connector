"""Registration import and opt-in Odoo-owned customer/address exports."""
import hashlib
import json
from copy import deepcopy
from datetime import timedelta

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.client import search_params


def private_changes(before, after):
    changed = [key for key, value in after.items() if before is None or (before.get(key) or '') != (value or '')]
    return ({key: 'Value hidden' for key in changed} if before is not None else None,
            {key: 'Updated (value hidden)' for key in changed})


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class MagentoCustomerInstance(models.Model):
    _inherit = 'in.magento.instance'

    customer_ids = fields.One2many('in.magento.partner.binding', 'instance_id')
    customer_website_id = fields.Integer(string='Customer website ID', default=1)
    customer_store_id = fields.Integer(string='Customer creation store-view ID', default=1)
    auto_import_customers = fields.Boolean(string='Import new customer registrations', default=False)
    auto_export_customers = fields.Boolean(string='Export customer changes', default=False)
    customer_import_from = fields.Datetime(default=fields.Datetime.now, required=True)
    customer_cursor = fields.Datetime(readonly=True, copy=False)
    customer_interval = fields.Integer(default=5, string='Customer interval (minutes)')
    last_customer_schedule = fields.Datetime(readonly=True, copy=False)
    customer_count = fields.Integer(compute='_compute_customer_count')

    @api.depends('customer_ids.partner_id')
    def _compute_customer_count(self):
        for record in self:
            record.customer_count = self.env['res.partner'].with_context(active_test=False).search_count([
                ('id', 'in', record.customer_ids.partner_id.ids)])

    def action_related_customers(self):
        return self._related_records_action('base.action_partner_form', self.customer_ids.partner_id.ids, _('Customers'))

    @api.constrains('customer_website_id', 'customer_store_id', 'customer_interval')
    def _check_customers(self):
        for record in self:
            if min(record.customer_website_id, record.customer_store_id, record.customer_interval) < 1:
                raise ValidationError(_('Customer website, store-view and interval must be positive.'))

    def write(self, vals):
        for record in self:
            if record.customer_ids and any(key in vals and vals[key] != record[key] for key in (
                    'base_url', 'store_code', 'customer_website_id')):
                raise UserError(_('A customer-mapped connection keeps its Magento URL, scope and website.'))
            if record.customer_ids and 'company_id' in vals and vals['company_id'] != record.company_id.id:
                raise UserError(_('A customer-mapped connection keeps its company.'))
        return super().write(vals)

    def _lock_customer_sync(self):
        self.ensure_one()
        # Shared with order import so a registration and its first order cannot
        # create two local partners concurrently.
        self.env.cr.execute('SELECT pg_try_advisory_xact_lock(%s, %s)', [527019, self.id])
        if not self.env.cr.fetchone()[0]:
            raise RetryableJobError('Another customer/order import is running.', seconds=10)

    def action_import_customers(self):
        for record in self:
            record._require_manager()
            record._check_enabled()
            record.with_company(record.company_id).with_delay(identity_key=f'magento:customers:{record.id}')._job_poll_customers()
        return True

    def action_export_customers(self):
        self._require_manager()
        self.customer_ids.filtered('sync_customer').action_export()
        return True

    def _job_poll_customers(self):
        self.ensure_one()
        self._check_enabled()
        start = max(self.customer_import_from, (self.customer_cursor or self.customer_import_from) - timedelta(minutes=2))
        self._job_customer_page(fields.Datetime.to_string(start), fields.Datetime.to_string(fields.Datetime.now()), 0)

    def _job_customer_page(self, start, end, after_id):
        self.ensure_one()
        self._check_enabled()
        self._lock_customer_sync()
        with self._client(store_code='all') as client:
            result = client.request('GET', 'customers/search', params=search_params([
                ('website_id', 'eq', self.customer_website_id), ('created_at', 'gteq', start),
                ('created_at', 'lteq', end), ('entity_id', 'gt', after_id)], size=self.order_batch_size))
        if not isinstance(result, dict) or not isinstance(result.get('items'), list):
            raise UserError(_('Magento returned an invalid customer collection.'))
        items = result['items']
        ids = [int(item['id']) for item in items]
        if ids and (ids != sorted(set(ids)) or ids[0] <= after_id):
            raise UserError(_('Customer pagination did not advance.'))
        for item in items:
            if int(item.get('website_id', 0)) != self.customer_website_id:
                raise UserError(_('Magento returned a customer outside the configured website.'))
            if not self.customer_ids.filtered(lambda b: b.external_id == str(item['id'])):
                # Store IDs, not private customer payloads, in the job queue.
                self.with_delay(identity_key=f'magento:customer-import:{self.id}:{item["id"]}')._job_import_customer(int(item['id']))
        if len(items) == self.order_batch_size:
            self.with_delay()._job_customer_page(start, end, ids[-1])
        else:
            self.customer_cursor = max(self.customer_cursor or fields.Datetime.to_datetime(end), fields.Datetime.to_datetime(end))

    def _job_import_customer(self, customer_id):
        self.ensure_one()
        self._check_enabled()
        self._lock_customer_sync()
        Binding = self.env['in.magento.partner.binding'].with_company(self.company_id)
        if Binding.search_count([('instance_id', '=', self.id), ('external_id', '=', str(customer_id))]):
            return  # Existing Odoo customers and order snapshots are never overwritten.
        with self._client(store_code='all') as client:
            remote = client.request('GET', f'customers/{int(customer_id)}')
        if str(remote.get('id')) != str(customer_id) or remote.get('website_id') != self.customer_website_id:
            raise UserError(_('Customer identity or website differs. Reconcile before retrying.'))
        if not remote.get('firstname') or not remote.get('lastname') or not remote.get('email'):
            raise UserError(_('Magento customer name or email is missing.'))
        if self.env['res.partner'].with_context(active_test=False).search_count([
                ('email', '=ilike', remote['email']), ('company_id', 'in', [False, self.company_id.id])]):
            raise UserError(_('An Odoo contact already uses this email. Create an explicit Customer Mapping before retrying; no contact was merged or duplicated.'))
        with self.env.cr.savepoint():
            partner = self.env['res.partner'].with_company(self.company_id).create({
                'name': remote['firstname'] + ' ' + remote['lastname'], 'email': remote['email'],
                'company_id': self.company_id.id, 'vat': remote.get('taxvat') or False})
            binding = Binding.create({'instance_id': self.id, 'partner_id': partner.id,
                'external_id': str(customer_id), 'sync_customer': True,
                'verified_at': fields.Datetime.now()})
            for address in remote.get('addresses', []):
                values = binding._address_import_values(address)
                child = self.env['res.partner'].create(values)
                row = self.env['in.magento.customer.address'].create({'binding_id': binding.id,
                    'partner_id': child.id, 'external_id': str(address['id'])})
                row.last_fingerprint = fingerprint(row._values())
            binding.write({'last_fingerprint': fingerprint(binding._profile()), 'last_sync': fields.Datetime.now()})
            self.env['in.magento.sync.log']._record(binding, direction='import', operation='customer',
                summary='Magento registration imported; Odoo owns subsequent customer updates.')

    @api.model
    def _cron_schedule(self):
        super()._cron_schedule()
        if str(self.env['ir.config_parameter'].sudo().get_param('database.is_neutralized', 'false')).lower() in ('true', '1'):
            return
        now = fields.Datetime.now()
        for record in self.with_context(magento_sync_trigger='automatic').search([('enabled', '=', True)]):
            if record.last_customer_schedule and record.last_customer_schedule > now - timedelta(minutes=record.customer_interval):
                continue
            if record.auto_import_customers:
                record.action_import_customers()
            if record.auto_export_customers:
                record.customer_ids.with_company(record.company_id)._schedule_customer_exports()
            if record.auto_import_customers or record.auto_export_customers:
                record.last_customer_schedule = now


class MagentoCustomerBinding(models.Model):
    _name = 'in.magento.partner.binding'
    _inherit = ['in.magento.partner.binding', 'in.magento.sync.mixin']

    external_id = fields.Char(required=False, string='Magento customer ID', copy=False)
    sync_customer = fields.Boolean(default=False, string='Synchronize customer details')
    verified_at = fields.Datetime(readonly=True, copy=False)
    last_sync = fields.Datetime(readonly=True, copy=False)
    last_fingerprint = fields.Char(readonly=True, copy=False)
    address_ids = fields.One2many('in.magento.customer.address', 'binding_id')

    @api.constrains('external_id', 'partner_id')
    def _check_customer_binding(self):
        for record in self:
            if record.external_id and (not record.external_id.isdigit() or int(record.external_id) < 1):
                raise ValidationError(_('Magento customer IDs must be positive integers.'))
            if record.partner_id.parent_id:
                raise ValidationError(_('Map a top-level contact as the customer, and its children as addresses.'))

    def write(self, vals):
        for record in self:
            if record.verified_at and any(key in vals and vals[key] != (record[key].id if key.endswith('_id') and key != 'external_id' else record[key])
                                         for key in ('instance_id', 'partner_id', 'external_id')):
                raise UserError(_('A verified customer mapping keeps its identity.'))
        return super().write(vals)

    @staticmethod
    def _names(name):
        parts = (name or '').strip().rsplit(None, 1)
        if len(parts) != 2:
            raise UserError(_('Magento requires first and last names. Enter both in the Odoo contact name.'))
        return parts

    def _profile(self):
        self.ensure_one()
        partner = self.partner_id
        first, last = self._names(partner.name)
        email = (partner.email or '').strip()
        if not partner.active or '@' not in email or any(c in email for c in '\r\n<> '):
            raise UserError(_('Use an active contact with a plain valid email address.'))
        return {'firstname': first, 'lastname': last, 'email': email, 'taxvat': partner.vat or ''}

    def _remote(self, client):
        remote = client.request('GET', f'customers/{int(self.external_id)}')
        if str(remote.get('id')) != self.external_id or remote.get('website_id') != self.instance_id.customer_website_id:
            raise UserError(_('Customer identity or website differs. Reconcile before retrying.'))
        return remote

    def action_link(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            if not record.external_id:
                raise UserError(_('Enter the existing Magento customer ID first.'))
            with record.instance_id._client(store_code='all') as client:
                remote = record._remote(client)
            if (remote.get('email') or '').casefold() != (record.partner_id.email or '').strip().casefold():
                raise UserError(_('The email addresses differ. Verify the customer identity before linking.'))
            record.verified_at = fields.Datetime.now()
        return True

    def action_export(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            if not record.sync_customer:
                raise UserError(_('Enable Synchronize customer details on this mapping first.'))
            record.with_company(record.company_id).with_delay(identity_key=f'magento:customer:{record.id}')._job_export_customer()
        return True

    def _lock(self):
        self.instance_id._check_enabled()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another customer export is running.', seconds=10)
        self.invalidate_recordset()
        if not self.sync_customer:
            raise UserError(_('Customer synchronization is disabled for this mapping.'))

    def _job_customer_if_changed(self):
        self.ensure_one()
        self._lock()
        if self.external_id and self.verified_at:
            if self.last_fingerprint != fingerprint(self._profile()):
                self._job_export_customer()
            else:
                self._queue_addresses()

    def _schedule_customer_exports(self):
        for binding in self.filtered(lambda b: b.sync_customer and b.external_id and b.verified_at):
            try:
                changed = binding.last_fingerprint != fingerprint(binding._profile())
            except UserError:
                # Preserve a visible per-record job error without aborting cron.
                changed = True
            if changed:
                binding.with_delay(identity_key=f'magento:customer:{binding.id}')._job_customer_if_changed()
            else:
                # Addresses are independent: an unchanged profile must not hide
                # a changed or newly opted-in address.
                binding._queue_addresses()

    def _queue_addresses(self):
        for row in self.address_ids.filtered('sync_address'):
            try:
                changed = not row.external_id or row.last_fingerprint != fingerprint(row._values())
            except UserError:
                changed = True
            if changed:
                row.with_delay(identity_key=f'magento:customer-address:{row.id}')._job_export_address()

    def _job_export_customer(self):
        self.ensure_one()
        self._lock()
        values = self._profile()
        previous = None
        with self.instance_id._client(store_code='all') as client:
            if self.external_id:
                if not self.verified_at:
                    raise UserError(_('Verify the existing customer mapping before exporting.'))
                remote = self._remote(client)
                previous = remote
                payload = deepcopy(remote)
                payload.update(values)
                # Omit addresses completely here. Address changes have separate
                # transactions so their failure cannot lose a new customer ID.
                payload.pop('addresses', None)
                result = client.request('PUT', f'customers/{self.external_id}', payload={'customer': payload})
                if str(result.get('id')) != self.external_id:
                    raise UserError(_('Unexpected customer identity. Reconcile before retrying.'))
            else:
                stores = client.request('GET', 'store/storeViews')
                if not any(s.get('id') == self.instance_id.customer_store_id and s.get('website_id') == self.instance_id.customer_website_id for s in stores):
                    raise UserError(_('The customer creation store must belong to the configured website.'))
                existing = client.request('GET', 'customers/search', params=search_params([
                    ('email', 'eq', values['email']), ('website_id', 'eq', self.instance_id.customer_website_id)]))
                if existing.get('items'):
                    raise UserError(_('A Magento account already uses this email. Enter its customer ID and Verify / Link Existing before exporting.'))
                payload = dict(values, website_id=self.instance_id.customer_website_id, store_id=self.instance_id.customer_store_id)
                result = client.request('POST', 'customers', payload={'customer': payload})
                if not result.get('id') or result.get('website_id') != self.instance_id.customer_website_id:
                    raise UserError(_('Unexpected customer response. Reconcile before retrying.'))
                self.external_id = str(result['id'])
                self.verified_at = fields.Datetime.now()
        self.write({'last_sync': fields.Datetime.now(), 'last_fingerprint': fingerprint(values)})
        before, after = private_changes(previous, values)
        self.env['in.magento.sync.log']._record(self, direction='export', operation='customer',
            summary='Customer profile accepted by Magento (personal values omitted from history).',
            before=before, after=after, previous_source='magento' if previous else 'new')
        self._queue_addresses()

    def _address_import_values(self, address):
        country = self.env['res.country'].search([('code', '=', address.get('country_id'))], limit=1)
        if not country or not address.get('id'):
            raise UserError(_('Magento customer address requires a valid country and ID.'))
        region = address.get('region') or {}
        state = self.env['res.country.state'].search([('country_id', '=', country.id), ('code', '=', region.get('region_code'))], limit=1) if region.get('region_code') else self.env['res.country.state']
        street = address.get('street') or []
        return {'parent_id': self.partner_id.id, 'company_id': self.company_id.id,
            'name': ' '.join(filter(None, [address.get('firstname'), address.get('lastname')])),
            'type': 'invoice' if address.get('default_billing') else 'delivery',
            'street': street[0] if street else False, 'street2': '\n'.join(street[1:]),
            'city': address.get('city'), 'zip': address.get('postcode'), 'phone': address.get('telephone'),
            'country_id': country.id, 'state_id': state.id}


class MagentoCustomerAddress(models.Model):
    _name = 'in.magento.customer.address'
    _inherit = 'in.magento.sync.mixin'
    _description = 'Magento Customer Address Mapping'
    _check_company_auto = True
    _rec_name = 'partner_id'

    binding_id = fields.Many2one('in.magento.partner.binding', required=True, ondelete='cascade')
    instance_id = fields.Many2one(related='binding_id.instance_id', store=True)
    company_id = fields.Many2one(related='binding_id.company_id', store=True)
    partner_id = fields.Many2one('res.partner', required=True, ondelete='restrict', check_company=True)
    external_id = fields.Char(string='Magento address ID', copy=False)
    sync_address = fields.Boolean(default=True)
    last_fingerprint = fields.Char(readonly=True, copy=False)
    last_sync = fields.Datetime(readonly=True, copy=False)
    _address_unique = models.Constraint('UNIQUE(binding_id, external_id)', 'This Magento address is already mapped.')
    _partner_unique = models.Constraint('UNIQUE(binding_id, partner_id)', 'This Odoo address is already mapped.')

    @api.constrains('binding_id', 'partner_id', 'external_id')
    def _check_address(self):
        for record in self:
            if record.partner_id != record.binding_id.partner_id and record.partner_id.parent_id != record.binding_id.partner_id:
                raise ValidationError(_('Choose the mapped customer or one of its direct addresses.'))
            if record.external_id and (not record.external_id.isdigit() or int(record.external_id) < 1):
                raise ValidationError(_('Magento address IDs must be positive integers.'))
            # Never export/mutate order address snapshots through this workflow.
            if self.env['sale.order'].sudo().search_count(['|', ('partner_invoice_id', '=', record.partner_id.id), ('partner_shipping_id', '=', record.partner_id.id)]):
                raise ValidationError(_('This address is used on an order. Create a separate customer master address.'))

    def write(self, vals):
        if any(key in vals for key in ('binding_id', 'partner_id', 'external_id')):
            if any(record.last_sync for record in self):
                raise UserError(_('An exported customer address keeps its mapping identity.'))
            vals = dict(vals, last_fingerprint=False)
        return super().write(vals)

    def _values(self):
        p = self.partner_id
        first, last = self.binding_id._names(p.name or self.binding_id.partner_id.name)
        if not p.active or not p.country_id or not p.street or not p.city or not p.phone:
            raise UserError(_('A customer address needs an active contact, country, street, city and phone.'))
        return {'firstname': first, 'lastname': last, 'street': [v for v in [p.street, p.street2] if v],
                'city': p.city, 'postcode': p.zip or '', 'telephone': p.phone,
                'country_id': p.country_id.code, 'region': {'region_code': p.state_id.code or '', 'region': p.state_id.name or ''}}

    def _job_export_address(self):
        self.ensure_one()
        self.binding_id._lock()
        self.invalidate_recordset()
        if not self.sync_address:
            return
        if not self.binding_id.external_id or not self.binding_id.verified_at:
            raise UserError(_('Publish or verify the customer before exporting its addresses.'))
        values = self._values()
        if self.external_id and self.last_fingerprint == fingerprint(values):
            return
        with self.instance_id._client(store_code='all') as client:
            remote = self.binding_id._remote(client)
            addresses = deepcopy(remote.get('addresses') or [])
            if self.external_id:
                matches = [a for a in addresses if str(a.get('id')) == self.external_id]
                if len(matches) != 1:
                    raise UserError(_('The mapped customer address is missing. Reconcile before retrying.'))
                target = matches[0]
            else:
                if any(all(a.get(k) == v for k, v in values.items() if k != 'region') for a in addresses):
                    raise UserError(_('A matching Magento address exists. Map its ID before retrying.'))
                target = {}
                addresses.append(target)
            previous = deepcopy(target) if self.external_id else None
            target.update(values)
            target.pop('region_id', None)  # Magento resolves the region by its code/name.
            old_ids = {str(a.get('id')) for a in addresses if a.get('id')}
            payload = deepcopy(remote)
            payload['addresses'] = addresses
            # A PUT containing a new address is not safe to replay after timeout.
            result = client.request('PUT', f'customers/{self.binding_id.external_id}', payload={'customer': payload},
                                    replay_safe=bool(self.external_id))
            if str(result.get('id')) != self.binding_id.external_id:
                raise UserError(_('Unexpected customer identity. Reconcile before retrying.'))
            if not self.external_id:
                created = [a for a in result.get('addresses', []) if str(a.get('id')) not in old_ids]
                if len(created) != 1 or not created[0].get('id'):
                    raise UserError(_('Could not identify the created address. Reconcile before retrying.'))
                self.external_id = str(created[0]['id'])
        self.write({'last_sync': fields.Datetime.now(), 'last_fingerprint': fingerprint(values)})
        before, after = private_changes(previous, values)
        self.env['in.magento.sync.log']._record(self, direction='export', operation='customer',
            summary='Customer address accepted by Magento (personal values omitted from history).',
            before=before, after=after, previous_source='magento' if previous else 'new')
