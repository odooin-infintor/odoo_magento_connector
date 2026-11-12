from odoo import api, fields, models, _
from odoo.exceptions import AccessError, UserError


class ProductAttribute(models.Model):
    _inherit = 'product.attribute'

    def action_publish_to_magento(self):
        self.ensure_one()
        self.check_access('read')
        if not self.env.user.has_group('in_odoo_magento_connector.group_magento_manager'):
            raise AccessError(_('Only Magento connector administrators may publish attributes.'))
        connections = self.env['in.magento.instance'].search([('enabled', '=', True)])
        if not connections:
            raise UserError(_('Configure and enable a Magento connection first.'))
        return {'type': 'ir.actions.act_window', 'name': _('Publish Attribute to Magento'),
                'res_model': 'in.magento.publish.attributes', 'view_mode': 'form', 'target': 'new',
                'context': dict(self.env.context, default_attribute_id=self.id,
                    default_instance_id=connections.id if len(connections) == 1 else False)}


class MagentoPublishAttributes(models.TransientModel):
    _name = 'in.magento.publish.attributes'
    _description = 'Publish Attribute to Magento'

    instance_id = fields.Many2one('in.magento.instance', string='Magento connection', required=True,
        domain="[('enabled', '=', True)]")
    attribute_id = fields.Many2one('product.attribute', required=True, readonly=True)
    attribute_set_id = fields.Integer(related='instance_id.attribute_set_id')
    binding_id = fields.Many2one('in.magento.attribute.mapping', compute='_compute_binding')
    attribute_code = fields.Char(compute='_compute_code', store=True, precompute=True, readonly=False, required=True)
    value_ids = fields.One2many(related='attribute_id.value_ids', readonly=True)

    @api.depends('instance_id', 'attribute_id')
    def _compute_binding(self):
        for wizard in self:
            mapping = self.env['in.magento.attribute.mapping'].search([
                ('instance_id', '=', wizard.instance_id.id), ('attribute_id', '=', wizard.attribute_id.id)], limit=1)
            wizard.binding_id = mapping

    @api.depends('instance_id', 'attribute_id')
    def _compute_code(self):
        for wizard in self:
            wizard.attribute_code = wizard.binding_id.attribute_code or (f'odoo_attr_{wizard.attribute_id.id}' if wizard.attribute_id else False)

    def action_publish(self):
        self.ensure_one()
        self.check_access('write')
        self.instance_id._require_manager()
        self.instance_id._check_enabled()
        self.attribute_id.check_access('read')
        if self.instance_id.company_id not in self.env.companies:
            raise AccessError(_('The Magento company must be one of your selected companies.'))
        Mapping = self.env['in.magento.attribute.mapping'].with_company(self.instance_id.company_id)
        binding = Mapping.search([('instance_id', '=', self.instance_id.id), ('attribute_id', '=', self.attribute_id.id)], limit=1)
        with self.env.cr.savepoint():
            if binding and binding.attribute_code != self.attribute_code:
                raise UserError(_('An existing attribute mapping keeps its Magento code.'))
            if not binding:
                binding = Mapping.create({'instance_id': self.instance_id.id, 'attribute_id': self.attribute_id.id,
                    'attribute_code': self.attribute_code})
            binding.with_context(magento_sync_trigger='manual').action_export()
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Magento attribute publication queued'), 'type': 'success', 'sticky': False,
            'message': _('Check Sync Logs or Background Jobs for completion before publishing products.'),
            # Nested notification actions bypass the controller's action
            # normalization. Odoo's action service requires explicit views.
            'next': {'type': 'ir.actions.act_window', 'res_model': binding._name, 'res_id': binding.id,
                'view_mode': 'form', 'views': [(False, 'form')]},
        }}
