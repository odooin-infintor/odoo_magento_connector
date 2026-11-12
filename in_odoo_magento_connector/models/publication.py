"""Explicit listing withdrawal. Retain mappings and stop all catalog/stock jobs."""
from urllib.parse import quote

from odoo import fields, models, _
from odoo.exceptions import UserError
from odoo.addons.queue_job.exception import RetryableJobError


class MagentoPublicationMixin(models.AbstractModel):
    _name = 'in.magento.publication.mixin'
    _description = 'Magento Listing Publication State'

    publication_state = fields.Selection([
        ('managed', 'Sync active'), ('unpublish_pending', 'Unpublish pending'),
        ('unpublished', 'Unpublished / sync stopped'), ('republish_pending', 'Republish pending'),
    ], default='managed', required=True, readonly=True, copy=False, index=True,
        help='Sync active means the connector may update this mapping; it does not prove storefront visibility. Unpublish disables the Magento listing before confirming completion.')
    enable_on_creation = fields.Boolean(readonly=True, copy=False,
        help='An explicit Republish request enables a listing whose initial creation was stopped.')

    def _publication_allowed(self):
        self.ensure_one()
        return self.publication_state == 'managed'

    def _publication_lock(self):
        self.ensure_one()
        if not self.try_lock_for_update():
            raise RetryableJobError('Another publication or export is running for this product.', seconds=10)
        self.invalidate_recordset()

    def _job_set_publication(self):
        self.ensure_one()
        self.instance_id._require_manager()
        self.instance_id._check_enabled()
        self._publication_lock()
        state = self.publication_state
        if state not in ('unpublish_pending', 'republish_pending'):
            return
        enabling = state == 'republish_pending'
        product = self.template_id if self._name == 'in.magento.template.binding' else self.product_id
        if enabling and (not product.active or not product.sale_ok):
            raise UserError(_('Activate the Odoo product and enable sales before republishing.'))
        target = 1 if enabling else 2
        if self.external_id:
            expected = 'configurable' if self._name == 'in.magento.template.binding' else 'virtual' if product.type == 'service' else 'simple'
            path = 'products/' + quote(self.sku, safe='')
            with self.instance_id._client() as client:
                before = client.request('GET', path)
                def matches(row):
                    return (isinstance(row, dict) and str(row.get('id')) == self.external_id
                            and row.get('sku') == self.sku and row.get('type_id') == expected)
                if not matches(before):
                    raise UserError(_('The Magento SKU, ID or product type changed. Reconcile the mapping first.'))
                if before.get('status') != target:
                    client.request('PUT', path, payload={'product': {
                        'id': int(self.external_id), 'sku': self.sku, 'status': target}})
                after = client.request('GET', path)
                if not matches(after) or after.get('status') != target:
                    raise UserError(_('Magento has not confirmed the requested listing status. Retry after checking the remote product.'))
            self.env['in.magento.sync.log']._record(self, direction='export', operation='update',
                summary='Magento listing enabled; synchronization resumed.' if enabling else 'Magento listing disabled; synchronization stopped.',
                before={'status': before.get('status')}, after={'status': target}, previous_source='magento')
        self.publication_state = 'managed' if enabling else 'unpublished'
        if enabling:
            self.enable_on_creation = True
            self.last_fingerprint = False
            # Only export a configurable family once every mapped member is ready.
            parent = self if self._name == 'in.magento.template.binding' else self.template_binding_id
            if parent:
                if parent._publication_allowed() and all(child._publication_allowed() for child in parent.child_ids):
                    parent.action_export()
                    parent.child_ids.filtered(lambda row: row.sync_inventory and row.external_id and row.product_id.is_storable).action_export_inventory()
            else:
                self.action_export()
                if self.sync_inventory and self.external_id and self.product_id.is_storable:
                    self.action_export_inventory()


class MagentoProductPublication(models.Model):
    _name = 'in.magento.product.binding'
    _inherit = ['in.magento.product.binding', 'in.magento.publication.mixin']

    def _payload(self):
        payload = super()._payload()
        if not self.external_id and self.enable_on_creation and self.product_id.active and self.product_id.sale_ok:
            payload['product']['status'] = 1
        return payload

    def _publication_allowed(self):
        return super()._publication_allowed() and (not self.template_binding_id or self.template_binding_id.publication_state == 'managed')

    def action_export(self):
        if any(not row._publication_allowed() for row in self):
            raise UserError(_('This Magento product is unpublished or pending a status change. Use Republish to Magento first.'))
        return super().action_export()

    def _job_export(self):
        self._publication_lock()
        if self._publication_allowed():
            return super()._job_export()

    def _job_export_if_changed(self):
        if self._publication_allowed():
            return super()._job_export_if_changed()

    def _job_export_image(self):
        self._publication_lock()
        if self._publication_allowed():
            return super()._job_export_image()


class MagentoTemplatePublication(models.Model):
    _name = 'in.magento.template.binding'
    _inherit = ['in.magento.template.binding', 'in.magento.publication.mixin']

    def _payload(self):
        payload = super()._payload()
        if not self.external_id and self.enable_on_creation and self.template_id.active and self.template_id.sale_ok:
            payload['product']['status'] = 1
        return payload

    def action_export(self):
        if any(not row._publication_allowed() for row in self):
            raise UserError(_('This configurable product is unpublished or pending a status change. Use Republish to Magento first.'))
        return super().action_export()

    def _job_export(self):
        self._publication_lock()
        if self._publication_allowed():
            return super()._job_export()

    def _job_export_if_changed(self):
        if self._publication_allowed():
            return super()._job_export_if_changed()

    def _job_export_image(self):
        self._publication_lock()
        if self._publication_allowed():
            return super()._job_export_image()

    def _job_link_variants(self):
        self._publication_lock()
        if self._publication_allowed():
            return super()._job_link_variants()
