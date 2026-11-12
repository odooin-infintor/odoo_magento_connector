from markupsafe import Markup

from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError


class MagentoPublishCategories(models.TransientModel):
    _name = 'in.magento.publish.categories'
    _description = 'Publish Categories to Magento'

    instance_id = fields.Many2one('in.magento.instance', string='Magento connection', required=True,
                                 domain="[('enabled', '=', True)]")
    category_ids = fields.Many2many('product.category', string='Selected categories', required=True, readonly=True)
    parent_binding_id = fields.Many2one('in.magento.category.binding', string='Magento parent',
        domain="[('instance_id', '=', instance_id), ('external_id', '!=', False)]",
        help='For new top-level mappings. Leave empty to use the connection root category. Existing mappings keep their parents.')
    root_category_id = fields.Integer(related='instance_id.root_category_id')
    include_parents = fields.Boolean(string='Include unselected Odoo parents', default=False,
        help='Include ancestors up to the first existing mapping. Review them below before publishing. Selected parent-child relationships are always preserved.')
    preview_html = fields.Html(compute='_compute_preview', string='Publication preview', sanitize=True)
    publish_count = fields.Integer(compute='_compute_preview', string='Categories to publish')
    extra_count = fields.Integer(compute='_compute_preview', string='Additional parents')

    @api.onchange('instance_id')
    def _onchange_instance(self):
        self.parent_binding_id = False

    @api.model
    def _open_for_categories(self, categories):
        if not self.env.user.has_group('in_odoo_magento_connector.group_magento_manager'):
            raise AccessError(_('Only Magento connector administrators may publish categories.'))
        categories.check_access('read')
        if not categories:
            raise UserError(_('Select at least one category.'))
        connections = self.env['in.magento.instance'].search([('enabled', '=', True)])
        if not connections:
            raise UserError(_('Configure and enable a Magento connection first.'))
        return {'type': 'ir.actions.act_window', 'name': _('Publish Categories to Magento'),
                'res_model': self._name, 'view_mode': 'form', 'target': 'new',
                'context': dict(self.env.context, default_category_ids=[Command.set(categories.ids)],
                    default_instance_id=connections.id if len(connections) == 1 else False)}

    def _publication_plan(self):
        """Return the reviewed hierarchy in parent-first order without mutations."""
        self.ensure_one()
        if not self.instance_id:
            return []
        if self.parent_binding_id and (self.parent_binding_id.instance_id != self.instance_id or not self.parent_binding_id.external_id):
            raise UserError(_('Choose a published parent from the selected Magento connection.'))
        selected = set(self.category_ids.ids)
        existing = self.env['in.magento.category.binding'].search([('instance_id', '=', self.instance_id.id)])
        by_category = {binding.category_id.id: binding for binding in existing}
        plan, visited, visiting = [], set(), set()

        def visit(category):
            if category.id in visited:
                return
            if category.id in visiting:
                raise UserError(_('The category hierarchy contains a cycle.'))
            category.check_access('read')
            visiting.add(category.id)
            binding = by_category.get(category.id)
            parent_category = False
            parent_binding = False
            if binding:
                parent_binding = binding.parent_id
                if parent_binding and not parent_binding.external_id:
                    visit(parent_binding.category_id)
            elif category.parent_id and (self.include_parents or category.parent_id.id in selected):
                visit(category.parent_id)
                parent_category = category.parent_id
            else:
                parent_binding = self.parent_binding_id
            plan.append({'category': category, 'binding': binding, 'parent_category': parent_category,
                         'parent_binding': parent_binding, 'additional': category.id not in selected})
            visiting.remove(category.id)
            visited.add(category.id)

        for category in self.category_ids.sorted('complete_name'):
            visit(category)
        return plan

    @api.depends('instance_id', 'category_ids', 'parent_binding_id', 'include_parents')
    def _compute_preview(self):
        for wizard in self:
            if wizard.parent_binding_id and (wizard.parent_binding_id.instance_id != wizard.instance_id or not wizard.parent_binding_id.external_id):
                wizard.preview_html = _("Choose a published parent from the selected Magento connection.")
                wizard.publish_count = wizard.extra_count = 0
                continue
            plan = wizard._publication_plan()
            wizard.publish_count = len(plan)
            wizard.extra_count = sum(item['additional'] for item in plan)
            rows = []
            for item in plan:
                parent = item['parent_category'] or (item['parent_binding'].category_id if item['parent_binding'] else False)
                parent_label = parent.display_name if parent else _('Connection root (ID %s)', wizard.root_category_id)
                binding = item['binding']
                action = _('Update') if binding and binding.external_id else _('Create')
                rows.append(Markup('<tr><td>{}</td><td>{}</td><td>{}</td><td>{}</td></tr>').format(
                    item['category'].display_name, action, parent_label,
                    _('Additional parent') if item['additional'] else _('Selected')))
            wizard.preview_html = Markup('<table class="table table-sm"><thead><tr><th>{}</th><th>{}</th><th>{}</th><th>{}</th></tr></thead><tbody>{}</tbody></table>').format(
                _('Category'), _('Action'), _('Magento parent'), _('Selection'), Markup('').join(rows)) if plan else False

    def action_publish(self):
        self.ensure_one()
        self.check_access('write')
        instance = self.instance_id
        instance._require_manager()
        instance._check_enabled()
        if instance.company_id not in self.env.companies:
            raise AccessError(_('The Magento company must be one of your selected companies.'))
        if not self.category_ids:
            raise UserError(_('Select at least one category.'))
        plan = self._publication_plan()
        Binding = self.env['in.magento.category.binding'].with_company(instance.company_id)
        by_category = {}
        bindings = Binding.browse()
        with self.env.cr.savepoint():
            for item in plan:
                binding = item['binding']
                if not binding:
                    parent = by_category[item['parent_category'].id] if item['parent_category'] else item['parent_binding']
                    binding = Binding.create({'instance_id': instance.id, 'category_id': item['category'].id,
                                              'parent_id': parent.id if parent else False})
                by_category[item['category'].id] = binding
                bindings |= binding
            # Existing export logic queues missing parents first; a child waits
            # for its parent's committed Magento ID before making an API write.
            bindings.with_context(magento_sync_trigger='manual').action_export()
        action = self.env['ir.actions.actions']._for_xml_id('in_odoo_magento_connector.action_categories')
        action['domain'] = [('id', 'in', bindings.ids)]
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Magento category publication queued'),
            'message': _('%s categories queued. Check Sync Logs or Background Jobs for the result.', len(bindings)),
            'type': 'success', 'sticky': False, 'next': action,
        }}
