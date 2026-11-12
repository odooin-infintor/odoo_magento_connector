"""Explicit scalar catalog fields; no implicit EAV creation or option guessing."""
import math
import re
from datetime import timedelta
from urllib.parse import quote

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError


class MagentoCatalogField(models.Model):
    _name = 'in.magento.catalog.field'
    _description = 'Magento Catalog Field Mapping'
    _rec_name = 'attribute_code'

    instance_id = fields.Many2one('in.magento.instance', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='instance_id.company_id', store=True)
    field_id = fields.Many2one('ir.model.fields', required=True, ondelete='cascade',
        domain="[('model', '=', 'product.product'), ('ttype', 'in', ['char', 'text', 'integer', 'float', 'boolean', 'selection'])]")
    attribute_code = fields.Char(required=True)
    verified_at = fields.Datetime(readonly=True, copy=False)
    external_id = fields.Integer(readonly=True, copy=False)
    frontend_input = fields.Char(readonly=True, copy=False)
    _code_unique = models.Constraint('UNIQUE(instance_id, attribute_code)', 'Map each Magento catalog field only once.')

    @api.constrains('field_id', 'attribute_code', 'instance_id')
    def _check_mapping(self):
        reserved = {'sku', 'name', 'price', 'weight', 'status', 'visibility', 'type_id',
                    'attribute_set_id', 'description', 'tax_class_id', 'category_ids'}
        for record in self:
            field = self.env['product.product']._fields.get(record.field_id.name)
            if record.field_id.model != 'product.product' or not field or field.type not in (
                    'char', 'text', 'integer', 'float', 'boolean', 'selection') or field.groups:
                raise ValidationError(_('Choose an unrestricted scalar field of Product Variant.'))
            if not re.fullmatch('[a-z][a-z0-9_]{0,59}', record.attribute_code or '') or record.attribute_code in reserved:
                raise ValidationError(_('Use an unreserved Magento custom attribute code.'))
            if record.instance_id.attribute_mapping_ids.filtered(lambda a: a.attribute_code == record.attribute_code):
                raise ValidationError(_('This code is already managed as a variant attribute.'))

    def write(self, vals):
        if any(key in vals for key in ('field_id', 'attribute_code', 'instance_id')):
            vals = dict(vals, verified_at=False, external_id=False, frontend_input=False)
        return super().write(vals)

    def _verify(self, client):
        self.ensure_one()
        remote = client.request('GET', 'products/attributes/' + quote(self.attribute_code, safe=''))
        if not remote.get('is_user_defined') or remote.get('frontend_input') not in ('text', 'textarea', 'boolean'):
            raise UserError(_('Custom catalog fields must be existing user-defined text, textarea or boolean Magento attributes.'))
        if remote.get('frontend_input') == 'boolean' and self.field_id.ttype != 'boolean':
            raise UserError(_('A Magento boolean requires an Odoo boolean field.'))
        if self.external_id and remote.get('attribute_id') != self.external_id:
            raise UserError(_('The custom attribute identity changed. Reconcile before retrying.'))
        members = client.request('GET', f'products/attribute-sets/{self.instance_id.attribute_set_id}/attributes')
        if self.attribute_code not in [row.get('attribute_code') for row in members]:
            raise UserError(_('Assign the custom attribute to the configured Magento attribute set first.'))
        self.write({'external_id': remote['attribute_id'], 'frontend_input': remote['frontend_input'],
                    'verified_at': fields.Datetime.now()})

    def action_verify(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            with record.instance_id._client(store_code='all') as client:
                record._verify(client)
        return True

    def _value(self, product):
        self.ensure_one()
        self._check_mapping()
        if not self.verified_at:
            raise UserError(_('Verify custom catalog field %s before exporting products.', self.attribute_code))
        # ORM read enforces field access before data can be published.
        value = product.with_context(lang=None).read([self.field_id.name])[0][self.field_id.name]
        if self.field_id.ttype == 'boolean':
            return int(bool(value))
        if self.field_id.ttype in ('float', 'integer'):
            if not math.isfinite(value):
                raise UserError(_('A custom catalog field contains a non-finite number.'))
            return value
        return value or ''


class MagentoCatalogInstance(models.Model):
    _inherit = 'in.magento.instance'

    catalog_field_ids = fields.One2many('in.magento.catalog.field', 'instance_id')
    auto_export_categories = fields.Boolean(default=False)
    auto_export_attributes = fields.Boolean(default=False)
    catalog_metadata_interval = fields.Integer(default=5, string='Categories / attributes interval (minutes)')
    last_metadata_schedule = fields.Datetime(readonly=True, copy=False)

    @api.constrains('catalog_metadata_interval')
    def _check_metadata_interval(self):
        if any(record.catalog_metadata_interval < 1 for record in self):
            raise ValidationError(_('The category / attribute interval must be at least one minute.'))

    @api.model
    def _cron_schedule(self):
        super()._cron_schedule()
        if str(self.env['ir.config_parameter'].sudo().get_param('database.is_neutralized', 'false')).lower() in ('true', '1'):
            return
        now = fields.Datetime.now()
        for instance in self.with_context(magento_sync_trigger='automatic').search([('enabled', '=', True)]):
            if instance.last_metadata_schedule and instance.last_metadata_schedule > now - timedelta(minutes=instance.catalog_metadata_interval):
                continue
            for enabled, records, kind in (
                    (instance.auto_export_categories, instance.category_ids, 'category'),
                    (instance.auto_export_attributes, instance.attribute_mapping_ids, 'attribute')):
                if enabled:
                    instance._schedule_catalog_exports(records.filtered('external_id'), kind)
            if instance.auto_export_categories or instance.auto_export_attributes:
                instance.last_metadata_schedule = now


class MagentoCatalogProduct(models.Model):
    _inherit = 'in.magento.product.binding'

    def _payload(self):
        payload = super()._payload()
        for mapping in self.instance_id.catalog_field_ids:
            if mapping.attribute_code in [a['attribute_code'] for a in payload['product']['custom_attributes']]:
                raise UserError(_('A custom catalog code conflicts with a managed product attribute.'))
            payload['product']['custom_attributes'].append({'attribute_code': mapping.attribute_code,
                'value': mapping._value(self.product_id.with_company(self.company_id))})
        return payload

    def _job_export(self):
        if self.instance_id.catalog_field_ids:
            self.instance_id._check_enabled()
            with self.instance_id._client(store_code='all') as client:
                for mapping in self.instance_id.catalog_field_ids:
                    mapping._verify(client)
        return super()._job_export()

    def _log_product_values(self, product):
        values = super()._log_product_values(product)
        codes = set(self.instance_id.catalog_field_ids.mapped('attribute_code')) if self else set()
        for attribute in product.get('custom_attributes', []):
            if attribute.get('attribute_code') in codes:
                values['custom:' + attribute['attribute_code']] = str(attribute.get('value', ''))
        return values
