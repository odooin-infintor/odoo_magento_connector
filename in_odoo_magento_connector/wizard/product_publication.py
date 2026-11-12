from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError

MANAGER = 'in_odoo_magento_connector.group_magento_manager'


class MagentoProductPublicationWizard(models.TransientModel):
    _name = 'in.magento.product.publication'
    _description = 'Change Magento Product Publication'

    template_ids = fields.Many2many('product.template', required=True, readonly=True)
    instance_id = fields.Many2one('in.magento.instance', required=True, string='Magento connection')
    available_instance_ids = fields.Many2many('in.magento.instance', compute='_compute_bindings')
    operation = fields.Selection([('unpublish', 'Unpublish and stop sync'), ('republish', 'Republish and resume sync')], required=True, default='unpublish')
    product_binding_ids = fields.Many2many('in.magento.product.binding', compute='_compute_bindings')
    template_binding_ids = fields.Many2many('in.magento.template.binding', compute='_compute_bindings')

    def _all_bindings(self):
        products = self.env['in.magento.product.binding'].search([('product_id.product_tmpl_id', 'in', self.template_ids.ids)])
        parents = self.env['in.magento.template.binding'].search([('template_id', 'in', self.template_ids.ids)])
        return products, parents

    @api.depends('template_ids', 'instance_id')
    def _compute_bindings(self):
        for wizard in self:
            products, parents = wizard._all_bindings()
            wizard.available_instance_ids = products.instance_id | parents.instance_id
            wizard.product_binding_ids = products.filtered(lambda row: row.instance_id == wizard.instance_id)
            wizard.template_binding_ids = parents.filtered(lambda row: row.instance_id == wizard.instance_id)

    @api.model
    def _open(self, templates, operation):
        if not self.env.user.has_group(MANAGER):
            raise AccessError(_('Only Magento connector administrators may change publication.'))
        templates.check_access('read')
        if operation not in ('unpublish', 'republish'):
            raise UserError(_('Choose Unpublish or Republish.'))
        products = self.env['in.magento.product.binding'].search([('product_id.product_tmpl_id', 'in', templates.ids)])
        parents = self.env['in.magento.template.binding'].search([('template_id', 'in', templates.ids)])
        instances = products.instance_id | parents.instance_id
        if not instances:
            raise UserError(_('These products have no Magento mappings. Use Publish to Magento first.'))
        return {'type': 'ir.actions.act_window', 'name': _('Magento Publication'),
            'res_model': self._name, 'view_mode': 'form', 'target': 'new',
            'context': dict(self.env.context, default_template_ids=[Command.set(templates.ids)],
                default_instance_id=instances.id if len(instances) == 1 else False, default_operation=operation)}

    def action_apply(self):
        self.ensure_one()
        self.check_access('write')
        instance = self.instance_id
        instance._require_manager()
        instance._check_enabled()
        self.template_ids.check_access('read')
        products, parents = self._all_bindings()
        products = products.filtered(lambda row: row.instance_id == instance)
        parents = parents.filtered(lambda row: row.instance_id == instance)
        if not products and not parents:
            raise UserError(_('Choose a connection with mappings for these products.'))
        if instance.company_id not in self.env.companies:
            raise AccessError(_('Select the connection company first.'))
        enabling = self.operation == 'republish'
        if enabling and any(not row.product_id.active or not row.product_id.sale_ok for row in products):
            raise UserError(_('Activate all mapped variants and enable sales before republishing.'))
        if enabling and any(not row.template_id.active or not row.template_id.sale_ok for row in parents):
            raise UserError(_('Activate the Odoo template and enable sales before republishing.'))
        state = 'republish_pending' if enabling else 'unpublish_pending'
        with self.env.cr.savepoint():
            for records in (parents, products):
                records.check_access('write')
                records.write({'publication_state': state})
                for row in records:
                    # Queued stale exports recheck this state before doing any work.
                    row.with_context(magento_sync_trigger='manual').with_company(instance.company_id).with_delay(
                        identity_key=f'magento:publication:{row._name}:{row.id}', priority=5,
                        description=f'{instance.name}: {self.operation} {row.sku}')._job_set_publication()
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Publication change queued'),
            'message': _('Product synchronization is stopped while Magento confirms the change. Check Sync Logs or Background Jobs for the result.'),
            'type': 'success', 'sticky': False, 'next': {'type': 'ir.actions.act_window_close'}}}
