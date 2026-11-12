import hashlib
import json
import re
from copy import deepcopy
from urllib.parse import quote

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.client import MagentoError, search_params


class MagentoAttribute(models.Model):
    _name = 'in.magento.attribute.mapping'
    _inherit = 'in.magento.sync.mixin'
    _description = 'Magento Variant Attribute Mapping'
    _rec_name = 'attribute_id'

    instance_id = fields.Many2one('in.magento.instance', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='instance_id.company_id', store=True)
    attribute_id = fields.Many2one('product.attribute', required=True, ondelete='restrict')
    attribute_code = fields.Char(required=True, help='Stable Magento global dropdown attribute code. Verify existing attributes before exporting.')
    external_id = fields.Integer(string='Magento attribute ID', readonly=True, copy=False)
    value_ids = fields.One2many('in.magento.attribute.value', 'mapping_id')
    verified_at = fields.Datetime(readonly=True, copy=False)
    last_sync = fields.Datetime(readonly=True, copy=False)
    last_fingerprint = fields.Char(readonly=True, copy=False)
    _attribute_unique = models.Constraint('UNIQUE(instance_id, attribute_id)', 'This attribute is already mapped.')
    _code_unique = models.Constraint('UNIQUE(instance_id, attribute_code)', 'This Magento attribute code is already mapped.')

    @api.constrains('attribute_code')
    def _check_code(self):
        for record in self:
            if not re.fullmatch('[a-z][a-z0-9_]{0,59}', record.attribute_code or ''):
                raise ValidationError(_('Use up to 60 lowercase letters, digits or underscores, starting with a letter.'))

    def write(self, vals):
        if any(record.external_id for record in self) and any(key in vals for key in ('instance_id', 'attribute_id', 'attribute_code')):
            raise UserError(_('A linked Magento attribute keeps its connection, Odoo attribute and code.'))
        return super().write(vals)

    def _publication_values(self):
        self.ensure_one()
        attribute = self.attribute_id.with_context(lang=None)
        values = attribute.value_ids
        if not attribute.active or attribute.create_variant != 'always' or not values or values.filtered('is_custom'):
            raise UserError(_('Publish an active attribute with instant, non-custom variant values.'))
        labels = [value.name.strip().casefold() for value in values]
        if not attribute.name.strip() or any(not label for label in labels) or len(set(labels)) != len(labels):
            raise UserError(_('Attribute values need nonempty, distinct names before publication.'))
        return values.sorted(lambda value: (value.sequence, value.id))

    def action_export(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            record._publication_values()
            record.with_company(record.company_id).with_delay(identity_key=f'magento:attribute:{record.id}')._job_export()
        return True

    @staticmethod
    def _check_remote_type(attribute):
        if attribute.get('frontend_input') != 'select' or attribute.get('scope') != 'global' or not attribute.get('is_user_defined'):
            raise UserError(_('Variant attributes must be user-defined global dropdown attributes in Magento.'))

    def _fingerprint(self):
        self.ensure_one()
        attribute = self.attribute_id.with_context(lang=None)
        return hashlib.sha256(json.dumps([attribute.name, attribute.active, attribute.create_variant,
            [(v.id, v.name, v.sequence, v.is_custom) for v in attribute.value_ids.sorted('id')]],
            sort_keys=True).encode()).hexdigest()

    def _job_export_if_changed(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another attribute export is running.', seconds=10)
        self.invalidate_recordset()
        if self.external_id and self.last_fingerprint != self._fingerprint():
            self._job_export()

    def _job_export(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another attribute export is running.', seconds=10)
        self.invalidate_recordset()
        values = self._publication_values()
        with self.instance_id._client(store_code='all') as client:
            try:
                remote = client.request('GET', 'products/attributes/' + quote(self.attribute_code, safe=''))
            except MagentoError as error:
                if error.status != 404:
                    raise
                remote = None
            if remote and not self.external_id:
                raise UserError(_('This Magento attribute already exists. Map its option IDs and use Verify in Magento before exporting.'))
            if self.external_id and (not remote or remote.get('attribute_id') != self.external_id):
                raise UserError(_('The Magento attribute identity changed. Reconcile before retrying.'))
            if remote:
                self._check_remote_type(remote)
            # Magento's repository deletes store labels of every submitted
            # option, but GET does not return all those labels. Submit only
            # NEW options: never round-trip existing option objects.
            payload = deepcopy(remote) if remote else {
                'attribute_code': self.attribute_code, 'frontend_input': 'select', 'scope': 'global',
                'is_user_defined': True, 'is_required': False, 'is_unique': False, 'is_visible': True,
            }
            existing_options = [row for row in (remote or {}).get('options', []) if row.get('value')]
            by_id = {int(row['value']): row for row in existing_options}
            options = []
            mappings = {row.value_id.id: row for row in self.value_ids}
            if any(row.external_id not in by_id for row in self.value_ids):
                raise UserError(_('A mapped Magento option is missing. Reconcile before retrying.'))
            new_values = values.browse()
            for value in values:
                mapping = mappings.get(value.id)
                if any(str(row.get('label', '')).strip().casefold() == value.name.strip().casefold()
                       and (not mapping or int(row['value']) != mapping.external_id)
                       for row in (remote or {}).get('options', []) if row.get('value')):
                    raise UserError(_('An existing Magento option already uses "%s". Map its option ID explicitly before exporting.', value.name))
                if mapping:
                    if by_id[mapping.external_id]['label'] != value.name:
                        raise UserError(_('The mapped option "%s" has a different Magento label. Update its admin label in Magento first; existing options are preserved to protect store translations.', value.name))
                else:
                    options.append({'label': value.name, 'sort_order': len(existing_options) + len(options)})
                    new_values |= value
            payload.update(default_frontend_label=self.attribute_id.with_context(lang=None).name, options=options)
            before = {'label': (remote or {}).get('default_frontend_label'),
                      'values': {str(row['value']): row['label'] for row in (remote or {}).get('options', []) if row.get('value')}}
            # The repository's POST supports create and update. Even an update
            # can create new options, so never mark this write replay-safe.
            result = client.request('POST', 'products/attributes', payload={'attribute': payload})
            if not result.get('attribute_id') or (self.external_id and result['attribute_id'] != self.external_id):
                raise UserError(_('Magento returned an unexpected attribute ID. Reconcile before retrying.'))
            self.external_id = result['attribute_id']
            returned = [row for row in result.get('options', []) if row.get('value')]
            for value in new_values:
                matches = [row for row in returned if row.get('label') == value.name]
                if len(matches) != 1:
                    raise UserError(_('Magento returned ambiguous option IDs. Reconcile before retrying.'))
                self.env['in.magento.attribute.value'].create({'mapping_id': self.id, 'value_id': value.id,
                    'external_id': int(matches[0]['value'])})
        self.write({'last_sync': fields.Datetime.now(), 'last_fingerprint': self._fingerprint()})
        self.env['in.magento.sync.log']._record(self, direction='export', operation='attribute',
            summary='Attribute and values accepted by Magento.', before=before if remote else None,
            after={'label': payload['default_frontend_label'], 'values': {str(row['value']): row['label'] for row in returned}},
            previous_source='magento' if remote else 'new')
        # A separate transaction commits remote IDs before assignment can fail.
        self.with_delay(identity_key=f'magento:attribute-set:{self.id}')._job_assign_attribute_set()

    def _job_assign_attribute_set(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another attribute export is running.', seconds=10)
        self.invalidate_recordset()
        if not self.external_id:
            raise RetryableJobError('Waiting for attribute publication.', seconds=30)
        with self.instance_id._client(store_code='all') as client:
            remote = client.request('GET', 'products/attributes/' + quote(self.attribute_code, safe=''))
            self._check_remote_type(remote)
            if remote.get('attribute_id') != self.external_id:
                raise UserError(_('The Magento attribute identity changed. Reconcile before retrying.'))
            set_id = self.instance_id.attribute_set_id
            members = client.request('GET', f'products/attribute-sets/{set_id}/attributes')
            if self.attribute_code not in [row.get('attribute_code') for row in members]:
                groups = client.request('GET', 'products/attribute-sets/groups/list',
                    params=search_params([('attribute_set_id', 'eq', set_id)], sort='attribute_group_id'))
                groups = [row for row in groups.get('items', []) if int(row.get('attribute_set_id', 0)) == set_id]
                if not groups:
                    raise UserError(_('The configured Magento attribute set has no attribute groups.'))
                group = next((row for row in groups if row.get('attribute_group_name') == 'General'), groups[0])
                client.request('POST', 'products/attribute-sets/attributes', payload={
                    'attributeSetId': set_id, 'attributeGroupId': int(group['attribute_group_id']),
                    'attributeCode': self.attribute_code, 'sortOrder': 90}, replay_safe=True)
                self.env['in.magento.sync.log']._record(self, direction='export', operation='attribute',
                    summary='Attribute assigned to Magento attribute set.', after={'attribute_set_id': set_id})
            self._verify_remote(client)

    def action_verify(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            with record.instance_id._client(store_code='all') as client:
                record._verify_remote(client)
        return True

    def _verify_remote(self, client):
        self.ensure_one()
        attribute = client.request('GET', 'products/attributes/' + quote(self.attribute_code, safe=''))
        self._check_remote_type(attribute)
        if self.external_id and attribute.get('attribute_id') != self.external_id:
            raise UserError(_('The Magento attribute identity changed. Reconcile before retrying.'))
        members = client.request('GET', f'products/attribute-sets/{self.instance_id.attribute_set_id}/attributes')
        if self.attribute_code not in [row.get('attribute_code') for row in members]:
            raise UserError(_('Assign the Magento attribute to the connection attribute set first.'))
        options = {str(option.get('value')) for option in attribute.get('options', []) if option.get('value')}
        for value in self.value_ids:
            if str(value.external_id) not in options:
                raise UserError(_('Magento option ID for %s does not exist on this attribute.', value.value_id.name))
        self.write({'external_id': int(attribute['attribute_id']), 'verified_at': fields.Datetime.now()})
        return attribute


class MagentoAttributeValue(models.Model):
    _name = 'in.magento.attribute.value'
    _description = 'Magento Variant Option Mapping'
    _rec_name = 'value_id'

    mapping_id = fields.Many2one('in.magento.attribute.mapping', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='mapping_id.company_id', store=True)
    attribute_id = fields.Many2one(related='mapping_id.attribute_id')
    value_id = fields.Many2one('product.attribute.value', required=True, ondelete='restrict')
    external_id = fields.Integer(string='Magento option ID', required=True)
    _value_unique = models.Constraint('UNIQUE(mapping_id, value_id)', 'This value is already mapped.')
    _option_unique = models.Constraint('UNIQUE(mapping_id, external_id)', 'Each Magento option must identify one Odoo value.')

    @api.constrains('value_id', 'mapping_id', 'external_id')
    def _check_value(self):
        for record in self:
            if record.value_id.attribute_id != record.attribute_id or record.external_id <= 0:
                raise ValidationError(_('Choose a value of the mapped attribute and a positive Magento option ID.'))
