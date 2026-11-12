import hashlib
import json
from urllib.parse import quote

from odoo import api, fields, models, _
from odoo.exceptions import UserError, ValidationError
from odoo.addons.queue_job.exception import RetryableJobError

from ..services.client import MagentoError
from ..services.media import export_image, image_changed


class MagentoTemplateBinding(models.Model):
    _name = 'in.magento.template.binding'
    _inherit = 'in.magento.sync.mixin'
    _description = 'Magento Configurable Product Mapping'
    _check_company_auto = True
    _rec_name = 'sku'

    instance_id = fields.Many2one('in.magento.instance', required=True, ondelete='cascade')
    company_id = fields.Many2one(related='instance_id.company_id', store=True)
    template_id = fields.Many2one('product.template', required=True, check_company=True, ondelete='restrict')
    sku = fields.Char(required=True, help='Dedicated SKU for the configurable parent; different from every child SKU.')
    external_id = fields.Char(readonly=True, copy=False)
    child_ids = fields.One2many('in.magento.product.binding', 'template_binding_id')
    category_ids = fields.Many2many('in.magento.category.binding')
    sync_image = fields.Boolean(default=False)
    last_image_fingerprint = fields.Char(readonly=True, copy=False)
    tax_class_id = fields.Integer(default=0)
    last_sync = fields.Datetime(readonly=True, copy=False)
    last_fingerprint = fields.Char(readonly=True, copy=False)
    linked_at = fields.Datetime(readonly=True, copy=False)
    _template_unique = models.Constraint('UNIQUE(instance_id, template_id)', 'This template is already mapped.')
    _sku_unique = models.Constraint('UNIQUE(instance_id, sku)', 'This configurable SKU is already mapped.')
    _remote_unique = models.Constraint('UNIQUE(instance_id, external_id)', 'This configurable product is already mapped.')

    @api.constrains('sku', 'instance_id', 'category_ids', 'tax_class_id')
    def _check_mapping(self):
        for record in self:
            if not record.sku or record.sku.strip() != record.sku or record.tax_class_id < 0:
                raise ValidationError(_('Use a nonempty parent SKU without surrounding spaces and a nonnegative tax class.'))
            if record.category_ids.filtered(lambda row: row.instance_id != record.instance_id):
                raise ValidationError(_('Categories must belong to the configurable product connection.'))
            if self.env['in.magento.product.binding'].search_count([('instance_id', '=', record.instance_id.id), ('sku', '=', record.sku)]):
                raise ValidationError(_('Parent and child/simple SKUs must be different.'))

    def write(self, vals):
        if any(record.external_id for record in self) and any(key in vals for key in ('sku', 'template_id', 'instance_id')):
            raise UserError(_('Published configurable mappings cannot change SKU, template or connection.'))
        return super().write(vals)

    def _variant_configuration(self, verify=False, client=None):
        self.ensure_one()
        template = self.template_id
        if template.type != 'consu':
            raise UserError(_('Configurable products must contain physical product variants.'))
        lines = template.attribute_line_ids
        if not lines or any(line.attribute_id.create_variant != 'always' or line.value_ids.filtered('is_custom') for line in lines):
            raise UserError(_('Configurable products require instant, non-custom variant attributes. Dynamic and no-variant options are not supported.'))
        active = template.product_variant_ids
        if set(active.ids) != set(self.child_ids.filtered(lambda row: row.product_id.active).product_id.ids):
            raise UserError(_('Publish the template again to include all current variants before synchronization.'))
        if not active and not template.active:
            return []
        if not active:
            raise UserError(_('The configurable product has no active variants.'))
        mappings = self.env['in.magento.attribute.mapping'].search([
            ('instance_id', '=', self.instance_id.id), ('attribute_id', 'in', lines.attribute_id.ids)])
        if set(mappings.attribute_id.ids) != set(lines.attribute_id.ids):
            raise UserError(_('Configure all variant attribute and option mappings on the Magento connection first.'))
        options = []
        for mapping in mappings.sorted('id'):
            if verify:
                mapping._verify_remote(client)
            if not mapping.external_id:
                raise UserError(_('Verify each Magento attribute mapping before publishing variants.'))
            values = active.product_template_attribute_value_ids.product_attribute_value_id.filtered(lambda value: value.attribute_id == mapping.attribute_id)
            mapped = mapping.value_ids.filtered(lambda value: value.value_id in values)
            if set(mapped.value_id.ids) != set(values.ids):
                raise UserError(_('Map every value of attribute %s to its Magento option ID.', mapping.attribute_id.name))
            options.append({'attribute_id': str(mapping.external_id), 'label': mapping.attribute_id.name,
                'position': len(options), 'is_use_default': True,
                'values': [{'value_index': value.external_id} for value in mapped.sorted('external_id')]})
        return options

    def _payload(self):
        self.ensure_one()
        if self.category_ids.filtered(lambda row: not row.external_id):
            raise UserError(_('Publish mapped categories before the configurable product.'))
        product = {'sku': self.sku, 'name': self.template_id.name, 'type_id': 'configurable',
                   'attribute_set_id': self.instance_id.attribute_set_id, 'visibility': 4,
                   'custom_attributes': [
                       {'attribute_code': 'description', 'value': self.template_id.description_sale or ''},
                       {'attribute_code': 'tax_class_id', 'value': str(self.tax_class_id)},
                       {'attribute_code': 'category_ids', 'value': self.category_ids.mapped('external_id')}],
                   'extension_attributes': {'stock_item': {'is_in_stock': True}}}
        if not self.external_id:
            product['status'] = int(bool(self.instance_id.publish_enabled and self.template_id.active and self.template_id.sale_ok)) or 2
        elif not self.template_id.active or not self.template_id.sale_ok:
            product['status'] = 2
        return {'product': product}

    def _fingerprint(self):
        return hashlib.sha256(json.dumps({'payload': self._payload(), 'options': self._variant_configuration(),
            'image': self.template_id.image_1920.decode() if self.sync_image and self.template_id.image_1920 else '',
            'children': [(child.sku, child.product_id.active, child.product_id.sale_ok) for child in self.child_ids.sorted('id')]}, sort_keys=True).encode()).hexdigest()

    def action_link(self):
        for record in self:
            record.instance_id._require_manager()
            with record.instance_id._client() as client:
                remote = client.request('GET', 'products/' + quote(record.sku, safe=''))
            if remote.get('type_id') != 'configurable' or remote.get('sku') != record.sku or not remote.get('id'):
                raise UserError(_('The Magento parent SKU is not a configurable product.'))
            record.external_id = str(remote['id'])
        return True

    def action_export(self):
        for record in self:
            record.instance_id._require_manager()
            record.instance_id._check_enabled()
            record._variant_configuration()
            record.with_company(record.company_id).with_delay(identity_key=f'magento:template:{record.id}')._job_export()
        return True

    def _job_export(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another configurable product export is running.', seconds=10)
        self.invalidate_recordset()
        with self.instance_id._client() as client:
            self._variant_configuration(verify=True, client=client)
            path = 'products/' + quote(self.sku, safe='')
            try:
                remote = client.request('GET', path)
            except MagentoError as error:
                if error.status != 404:
                    raise
                remote = None
            if remote and not self.external_id:
                raise UserError(_('Configurable SKU already exists. Use Link Existing before exporting.'))
            if self.external_id and (not remote or str(remote.get('id')) != self.external_id or remote.get('type_id') != 'configurable'):
                raise UserError(_('The remote configurable mapping changed. Reconcile before retrying.'))
            payload = self._payload()
            result = client.request('PUT' if remote else 'POST', path if remote else 'products', payload=payload)
            if not result.get('id'):
                raise UserError(_('Magento returned no parent product ID. Reconcile before retrying.'))
            self.external_id = str(result['id'])
        self.write({'last_sync': fields.Datetime.now(), 'last_fingerprint': self._fingerprint()})
        self.env['in.magento.sync.log']._record(self, direction='export', operation='update' if remote else 'create',
            summary='Configurable parent accepted by Magento.', before=self.env['in.magento.product.binding']._log_product_values(remote) if remote else None,
            after=self.env['in.magento.product.binding']._log_product_values(payload['product']), previous_source='magento' if remote else 'new')
        if self.sync_image and self.template_id.image_1920 and image_changed(self, self.template_id.image_1920):
            self.with_delay(identity_key=f'magento:template-image:{self.id}')._job_export_image()
        self.child_ids.action_export()
        if self.template_id.active:
            self.with_delay(identity_key=f'magento:variant-links:{self.id}', priority=30, max_retries=20)._job_link_variants()

    def _job_link_variants(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another configurable export is running.', seconds=10)
        self.invalidate_recordset()
        if not self.external_id or self.child_ids.filtered(lambda row: not row.external_id):
            raise RetryableJobError('Waiting for parent and variant publication.', seconds=30)
        with self.instance_id._client() as client:
            options = self._variant_configuration(verify=True, client=client)
            path = 'products/' + quote(self.sku, safe='')
            remote = client.request('GET', path)
            if str(remote.get('id')) != self.external_id or remote.get('type_id') != 'configurable':
                raise UserError(_('The configurable parent mapping changed.'))
            extensions = remote.get('extension_attributes') or {}
            previous_links = extensions.get('configurable_product_links', [])
            all_ids = [int(child.external_id) for child in self.child_ids]
            desired_ids = [int(child.external_id) for child in self.child_ids if child.product_id.active and child.product_id.sale_ok]
            if set(previous_links) - set(all_ids):
                raise UserError(_('Magento contains unmanaged variants. Reconcile the parent before linking.'))
            previous_options = extensions.get('configurable_product_options', [])
            previous_by_attribute = {str(option['attribute_id']): option for option in previous_options}
            if set(previous_by_attribute) - {option['attribute_id'] for option in options}:
                raise UserError(_('Changing configurable attribute axes requires explicit reconciliation.'))
            for option in options:
                old = previous_by_attribute.get(option['attribute_id'])
                if old:
                    option['id'] = old['id']
            for child in self.child_ids.filtered(lambda row: row.product_id.active and row.product_id.sale_ok):
                child_remote = client.request('GET', 'products/' + quote(child.sku, safe=''))
                if str(child_remote.get('id')) != child.external_id or child_remote.get('type_id') != 'simple':
                    raise UserError(_('A Magento variant mapping changed.'))
                values = {row['attribute_code']: str(row.get('value')) for row in child_remote.get('custom_attributes', [])}
                if any(values.get(row['attribute_code']) != str(row['value']) for row in child._variant_attributes()):
                    raise RetryableJobError('Waiting for variant attribute updates.', seconds=30)
            client.request('PUT', path, payload={'product': {'sku': self.sku, 'extension_attributes': {
                'configurable_product_options': options, 'configurable_product_links': desired_ids}}})
        self.linked_at = fields.Datetime.now()
        self.env['in.magento.sync.log']._record(self, direction='export', operation='variants',
            summary='Variant choices and child links accepted by Magento.',
            before={'child_product_ids': previous_links}, after={'child_product_ids': desired_ids, 'options': options}, previous_source='magento')

    def _job_export_image(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another configurable product export is running.', seconds=10)
        self.invalidate_recordset()
        if self.sync_image and self.external_id and self.template_id.image_1920:
            export_image(self, self.template_id.image_1920.decode(), self.template_id.name)

    def _job_export_if_changed(self):
        self.ensure_one()
        self.instance_id._check_enabled()
        if self.external_id and self._fingerprint() != self.last_fingerprint:
            self._job_export()


class MagentoVariantProduct(models.Model):
    _inherit = 'in.magento.product.binding'

    template_binding_id = fields.Many2one('in.magento.template.binding', string='Configurable parent', ondelete='restrict', check_company=True)

    @api.constrains('template_binding_id', 'product_id', 'instance_id', 'sku')
    def _check_variant_parent(self):
        for record in self:
            if record.template_binding_id and (record.template_binding_id.instance_id != record.instance_id or record.template_binding_id.template_id != record.product_id.product_tmpl_id):
                raise ValidationError(_('The variant and configurable parent must share a template and connection.'))
            if self.env['in.magento.template.binding'].search_count([('instance_id', '=', record.instance_id.id), ('sku', '=', record.sku)]):
                raise ValidationError(_('Variant/simple and configurable parent SKUs must differ.'))

    def _variant_attributes(self):
        self.ensure_one()
        if not self.template_binding_id or not self.product_id.active:
            return []
        self.template_binding_id._variant_configuration()
        result = []
        for value in self.product_id.product_template_attribute_value_ids:
            mapping = self.env['in.magento.attribute.value'].search([
                ('mapping_id.instance_id', '=', self.instance_id.id),
                ('value_id', '=', value.product_attribute_value_id.id)], limit=1)
            if not mapping:
                raise UserError(_('Map every variant attribute value before publication.'))
            result.append({'attribute_code': mapping.mapping_id.attribute_code, 'value': str(mapping.external_id)})
        return result

    def _payload(self):
        payload = super()._payload()
        if self.template_binding_id:
            payload['product']['visibility'] = 1
            payload['product']['custom_attributes'] += self._variant_attributes()
        return payload

    def _job_export(self):
        if self.template_binding_id and not self.template_binding_id.external_id:
            raise RetryableJobError('Waiting for configurable parent publication.', seconds=30)
        # Include explicitly linked SKUs on their first successful export.
        initial_export = not self.last_sync
        result = super()._job_export()
        # Price/name/image changes do not alter stock. Stock has its own timer
        # and shipment follow-up; publication and disabling still update it.
        if (self.sync_inventory and self.product_id.is_storable
                and (initial_export or not self.product_id.active or not self.product_id.sale_ok)):
            self.with_delay(identity_key=f'magento:inventory:{self.id}')._job_export_inventory()
        return result
