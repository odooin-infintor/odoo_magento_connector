from odoo import api, fields, models, Command, _
from odoo.exceptions import AccessError, UserError, ValidationError


class MagentoPublishProducts(models.TransientModel):
    _name = 'in.magento.publish.products'
    _description = 'Publish Products to Magento'
    _check_company_auto = True

    instance_id = fields.Many2one('in.magento.instance', string='Magento connection', required=True,
                                 domain="[('enabled', '=', True)]")
    company_id = fields.Many2one(related='instance_id.company_id')
    product_ids = fields.Many2many('product.product', string='Selected products', required=True, readonly=True)
    product_count = fields.Integer(compute='_compute_counts')
    new_count = fields.Integer(compute='_compute_counts', string='New mappings')
    existing_count = fields.Integer(compute='_compute_counts', string='Existing mappings')
    pricelist_id = fields.Many2one(related='instance_id.pricelist_id')
    publish_enabled = fields.Boolean(related='instance_id.publish_enabled')
    auto_export_products = fields.Boolean(related='instance_id.auto_export_products')
    category_ids = fields.Many2many('in.magento.category.binding', string='Magento categories',
        domain="[('instance_id', '=', instance_id), ('external_id', '!=', False)]")
    tax_class_id = fields.Integer(string='Magento tax class ID', default=0, required=True,
        help='Applies to new mappings only. Enter the Magento product tax class; 0 means no tax class.')
    parent_line_ids = fields.One2many('in.magento.publish.parent', 'wizard_id', string='Configurable parents')
    sync_inventory = fields.Boolean(string='Synchronize inventory', help='For new mappings only. Requires tracked physical products and warehouse/source mappings.')
    sync_image = fields.Boolean(string='Export product image', help='Applies to new mappings only.')
    required_attribute_ids = fields.Many2many('product.attribute', compute='_compute_attribute_readiness')
    attribute_setup_needed = fields.Boolean(compute='_compute_attribute_readiness')
    attribute_status = fields.Text(string='Attribute readiness', compute='_compute_attribute_readiness')
    setup_attribute_id = fields.Many2one('product.attribute', string='Attribute to configure')
    setup_attribute_code = fields.Char(string='Magento attribute code', compute='_compute_setup_attribute',
        store=True, readonly=False)
    setup_mapping_id = fields.Many2one('in.magento.attribute.mapping', compute='_compute_setup_mapping')
    setup_attribute_set_id = fields.Integer(related='instance_id.attribute_set_id', string='Magento attribute set ID')

    def _attribute_readiness(self):
        """Local prerequisites only; export jobs still verify remote identities."""
        self.ensure_one()
        families = self.product_ids.product_tmpl_id.filtered(lambda template: template.product_variant_count > 1)
        attributes = families.attribute_line_ids.attribute_id.sorted('id')
        if not self.instance_id:
            return attributes, [], [_('Choose a Magento connection to check its attribute mappings.')]
        values = families.product_variant_ids.product_template_attribute_value_ids.product_attribute_value_id
        mappings = self.instance_id.attribute_mapping_ids
        issues, status = [], []
        for attribute in attributes:
            needed = values.filtered(lambda value: value.attribute_id == attribute)
            mapping = mappings.filtered(lambda row: row.attribute_id == attribute)
            missing = needed - mapping.value_ids.value_id
            problems = []
            if not mapping:
                problems.append(_('not mapped to this connection'))
            elif not mapping.external_id:
                problems.append(_('Magento attribute ID is missing; publish the attribute or verify an existing mapping'))
            if missing:
                problems.append(_('unmapped values: %s', ', '.join(missing.mapped('name'))))
            if problems:
                detail = _('%(attribute)s: %(problems)s', attribute=attribute.name, problems='; '.join(problems))
                issues.append(detail)
                status.append(detail)
            else:
                status.append(_('%(attribute)s: mappings complete (%(code)s). Magento is checked again during product export.',
                    attribute=attribute.name, code=mapping.attribute_code))
        return attributes, issues, status

    @api.depends('instance_id', 'product_ids',
        'product_ids.product_tmpl_id.attribute_line_ids.value_ids',
        'instance_id.attribute_mapping_ids', 'instance_id.attribute_mapping_ids.attribute_id',
        'instance_id.attribute_mapping_ids.attribute_code', 'instance_id.attribute_mapping_ids.external_id',
        'instance_id.attribute_mapping_ids.value_ids.value_id')
    def _compute_attribute_readiness(self):
        for wizard in self:
            attributes, issues, status = wizard._attribute_readiness()
            wizard.required_attribute_ids = attributes
            wizard.attribute_setup_needed = bool(issues)
            wizard.attribute_status = '\n\n'.join(status)

    @api.depends('instance_id', 'setup_attribute_id', 'instance_id.attribute_mapping_ids.attribute_code')
    def _compute_setup_attribute(self):
        for wizard in self:
            mapping = wizard.instance_id.attribute_mapping_ids.filtered(lambda row: row.attribute_id == wizard.setup_attribute_id)
            wizard.setup_attribute_code = mapping.attribute_code or (
                f'odoo_attr_{wizard.setup_attribute_id.id}' if wizard.setup_attribute_id else False)

    @api.depends('instance_id', 'setup_attribute_id', 'instance_id.attribute_mapping_ids')
    def _compute_setup_mapping(self):
        for wizard in self:
            wizard.setup_mapping_id = wizard.instance_id.attribute_mapping_ids.filtered(
                lambda row: row.attribute_id == wizard.setup_attribute_id)

    def _reopen(self):
        self.ensure_one()
        return {'type': 'ir.actions.act_window', 'name': _('Publish to Magento'),
            'res_model': self._name, 'res_id': self.id, 'view_mode': 'form',
            'views': [(False, 'form')], 'target': 'new'}

    def action_refresh_attributes(self):
        self.ensure_one()
        self.check_access('read')
        self.instance_id._require_manager()
        self.invalidate_recordset(['required_attribute_ids', 'attribute_setup_needed', 'attribute_status', 'setup_mapping_id'])
        return self._reopen()

    def _check_attribute_setup(self):
        self.ensure_one()
        self.check_access('write')
        if not self.instance_id:
            raise UserError(_('Choose a Magento connection first.'))
        self.instance_id._require_manager()
        self.instance_id._check_enabled()
        if self.instance_id.company_id not in self.env.companies:
            raise AccessError(_('The Magento company must be one of your selected companies.'))
        attributes, issues, status = self._attribute_readiness()
        if not self.setup_attribute_id or self.setup_attribute_id not in attributes:
            raise UserError(_('Choose an attribute used by the selected product variants.'))
        self.setup_attribute_id.check_access('read')

    def action_publish_attribute(self):
        self._check_attribute_setup()
        # Reuse the existing attribute publication workflow and its identity
        # safeguards. This queues attributes only, never the selected products.
        with self.env.cr.savepoint():
            attribute_wizard = self.env['in.magento.publish.attributes'].create({
                'instance_id': self.instance_id.id, 'attribute_id': self.setup_attribute_id.id,
                'attribute_code': self.setup_attribute_code})
            attribute_wizard.action_publish()
        self.invalidate_recordset(['required_attribute_ids', 'attribute_setup_needed', 'attribute_status', 'setup_mapping_id'])
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Attribute publication queued'), 'type': 'success', 'sticky': False,
            'message': _('Wait for the attribute and values to finish, then use Refresh attribute status. Products have not been published.'),
            'next': self._reopen()}}

    def action_open_attribute_mapping(self):
        self._check_attribute_setup()
        mapping = self.instance_id.attribute_mapping_ids.filtered(lambda row: row.attribute_id == self.setup_attribute_id)
        if not mapping:
            if not self.setup_attribute_code:
                raise UserError(_('Enter the existing Magento attribute code first.'))
            mapping = self.env['in.magento.attribute.mapping'].create({
                'instance_id': self.instance_id.id, 'attribute_id': self.setup_attribute_id.id,
                'attribute_code': self.setup_attribute_code})
        self.invalidate_recordset(['required_attribute_ids', 'attribute_setup_needed', 'attribute_status', 'setup_mapping_id'])
        return {'type': 'ir.actions.act_window', 'name': _('Edit / verify attribute mapping'),
            'res_model': mapping._name, 'res_id': mapping.id, 'view_mode': 'form',
            'views': [(False, 'form')], 'target': 'new'}

    @api.depends('instance_id', 'product_ids')
    def _compute_counts(self):
        for wizard in self:
            existing = self.env['in.magento.product.binding'].search_count([
                ('instance_id', '=', wizard.instance_id.id), ('product_id', 'in', wizard.product_ids.ids)
            ]) if wizard.instance_id else 0
            wizard.product_count = len(wizard.product_ids)
            wizard.existing_count = existing
            wizard.new_count = wizard.product_count - existing

    @api.onchange('instance_id', 'product_ids')
    def _onchange_instance(self):
        self.category_ids = False
        attributes, issues, status = self._attribute_readiness()
        if self.setup_attribute_id not in attributes:
            self.setup_attribute_id = attributes[:1]
        for line in self.parent_line_ids:
            existing = self.env['in.magento.template.binding'].search([
                ('instance_id', '=', self.instance_id.id), ('template_id', '=', line.template_id.id)], limit=1)
            line.parent_sku = existing.sku or f'ODOO-TPL-{line.template_id.id}' 

    @api.model
    def _open_for_products(self, products):
        if not self.env.user.has_group('in_odoo_magento_connector.group_magento_manager'):
            raise AccessError(_('Only Magento connector administrators may publish products.'))
        products.check_access('read')
        if not products:
            raise UserError(_('Select at least one product.'))
        products = products.product_tmpl_id.product_variant_ids
        connections = self.env['in.magento.instance'].search([('enabled', '=', True)])
        if not connections:
            raise UserError(_('Configure and enable a Magento connection first.'))
        return {
            'type': 'ir.actions.act_window', 'name': _('Publish to Magento'),
            'res_model': self._name, 'view_mode': 'form', 'target': 'new',
            'context': dict(self.env.context, default_product_ids=[Command.set(products.ids)],
                            default_instance_id=connections.id if len(connections) == 1 else False,
                            default_parent_line_ids=[Command.create({'template_id': template.id,
                                'parent_sku': (self.env['in.magento.template.binding'].search([
                                    ('instance_id', '=', connections.id), ('template_id', '=', template.id)], limit=1).sku
                                    if len(connections) == 1 else False) or f'ODOO-TPL-{template.id}'})
                                for template in products.product_tmpl_id if template.product_variant_count > 1]),
        }

    def action_publish(self):
        self.ensure_one()
        self.check_access('write')
        instance = self.instance_id
        instance._require_manager()
        instance._check_enabled()
        products = self.product_ids.exists()
        products.check_access('read')
        if not products:
            raise UserError(_('Select at least one product.'))
        if instance.company_id not in self.env.companies:
            raise AccessError(_('The Magento company must be one of your selected companies.'))
        if self.tax_class_id < 0:
            raise ValidationError(_('Magento tax class ID cannot be negative.'))
        if any(category.instance_id != instance or not category.external_id for category in self.category_ids):
            raise UserError(_('Choose published categories from the selected Magento connection.'))
        Binding = self.env['in.magento.product.binding'].with_company(instance.company_id)
        existing = Binding.search([('instance_id', '=', instance.id), ('product_id', 'in', products.ids)])
        if any(not row._publication_allowed() for row in existing):
            raise UserError(_('These products are unpublished or pending a status change. Use Republish to Magento first.'))
        by_product = {binding.product_id.id: binding for binding in existing}
        families = products.product_tmpl_id.filtered(lambda template: template.product_variant_count > 1)
        if set(self.parent_line_ids.template_id.ids) != set(families.ids) or len(self.parent_line_ids) != len(families):
            raise UserError(_('Review a configurable parent SKU for each variant family. Reopen Publish to Magento from the product.'))
        if set(families.product_variant_ids.ids) - set(products.ids):
            raise UserError(_('Publish all active variants of a configurable product together.'))
        if self.sync_inventory and not instance.inventory_source_ids:
            raise UserError(_('Configure warehouse/source mappings before enabling inventory synchronization.'))
        parents = self.env['in.magento.template.binding'].search([
            ('instance_id', '=', instance.id), ('template_id', 'in', families.ids)])
        parents_by_template = {parent.template_id.id: parent for parent in parents}
        for template in families:
            if template.type != 'consu' or any(line.attribute_id.create_variant != 'always' or line.value_ids.filtered('is_custom') for line in template.attribute_line_ids):
                raise UserError(_('Configurable products require physical products with instant, non-custom variant attributes.'))
        attributes, issues, status = self._attribute_readiness()
        if issues:
            raise UserError(_('Complete the attribute and value mappings for %(connection)s before publishing:\n\n%(details)s\n\n'
                'Open the Attributes tab in this wizard. Choose each attribute and use Publish / update attribute, '
                'or Edit / verify mapping for an existing Magento attribute. When the background jobs finish, '
                'click Refresh attribute status and publish the products again.',
                connection=instance.name, details='\n'.join('- ' + issue for issue in issues)))
        planned = []
        selected_skus = set()
        for line in self.parent_line_ids:
            parent = parents_by_template.get(line.template_id.id)
            if parent and line.parent_sku != parent.sku:
                raise UserError(_('An existing configurable parent keeps its original SKU.'))
            sku = parent.sku if parent else line.parent_sku
            if not sku or sku != sku.strip() or sku in selected_skus:
                raise UserError(_('Configurable parent SKUs must be nonempty and unique.'))
            if self.env['in.magento.template.binding'].search_count([('instance_id', '=', instance.id), ('sku', '=', sku), ('template_id', '!=', line.template_id.id)]):
                raise UserError(_('Another configurable product already uses this parent SKU.'))
            selected_skus.add(sku)
        # Validate the complete selection before creating mappings or queue jobs.
        for product in products:
            if product.company_id and product.company_id != instance.company_id:
                raise UserError(_('%s belongs to a different company.', product.display_name))
            if not product.active or not product.sale_ok:
                raise UserError(_('%s must be active and available for sale.', product.display_name))
            if product.type not in ('consu', 'service'):
                raise UserError(_('%s is unsupported. Only physical products and services can be published.', product.display_name))
            binding = by_product.get(product.id)
            sku = binding.sku if binding else product.default_code
            if not sku or sku != sku.strip():
                raise UserError(_('Set a nonempty Internal Reference without surrounding spaces for %s. It becomes the Magento SKU.', product.display_name))
            if sku in selected_skus:
                raise UserError(_('The selected products use duplicate SKU %s. Each SKU must be unique.', sku))
            selected_skus.add(sku)
            if binding and product.product_tmpl_id not in families:
                binding._payload()  # Check prerequisites such as published categories.
            if not binding:
                planned.append({'instance_id': instance.id, 'product_id': product.id, 'sku': sku,
                    'category_ids': [Command.set(self.category_ids.ids)],
                    'tax_class_id': self.tax_class_id, 'sync_image': self.sync_image,
                    'sync_inventory': self.sync_inventory and product.is_storable})
        collisions = Binding.search([('instance_id', '=', instance.id), ('sku', 'in', list(selected_skus)),
                                     ('product_id', 'not in', products.ids)])
        if collisions:
            raise UserError(_('Another product is already mapped with SKU %s.', collisions[0].sku))
        if Binding.search_count([('instance_id', '=', instance.id), ('sku', 'in', self.parent_line_ids.mapped('parent_sku'))]):
            raise UserError(_('A configurable parent SKU is already used by a simple product or variant.'))
        if self.env['in.magento.template.binding'].search_count([('instance_id', '=', instance.id), ('sku', 'in', [row.sku for row in existing] + [row['sku'] for row in planned])]):
            raise UserError(_('A variant SKU is already used by a configurable parent.'))
        # Atomic even when invoked from a shell or another addon.
        with self.env.cr.savepoint():
            for line in self.parent_line_ids:
                if line.template_id.id not in parents_by_template:
                    parent = self.env['in.magento.template.binding'].with_company(instance.company_id).create({
                        'instance_id': instance.id, 'template_id': line.template_id.id, 'sku': line.parent_sku,
                        'category_ids': [Command.set(self.category_ids.ids)], 'tax_class_id': self.tax_class_id, 'sync_image': self.sync_image})
                    parents_by_template[line.template_id.id] = parent
                    parents |= parent
            bindings = existing | Binding.create(planned)
            for binding in bindings:
                parent = parents_by_template.get(binding.product_id.product_tmpl_id.id)
                if parent and binding.template_binding_id != parent:
                    binding.template_binding_id = parent
            parents.with_context(magento_sync_trigger='manual').action_export()
            bindings.filtered(lambda row: not row.template_binding_id).with_context(magento_sync_trigger='manual').action_export()
        action = self.env['ir.actions.actions']._for_xml_id('in_odoo_magento_connector.action_products')
        action['domain'] = [('id', 'in', bindings.ids)]
        return {'type': 'ir.actions.client', 'tag': 'display_notification', 'params': {
            'title': _('Magento publication queued'),
            'message': _('%s product(s) queued. Check Sync Logs or Background Jobs for the result.', len(bindings)),
            'type': 'success', 'sticky': False, 'next': action,
        }}


class MagentoPublishParent(models.TransientModel):
    _name = 'in.magento.publish.parent'
    _description = 'Configurable Parent Publication'

    wizard_id = fields.Many2one('in.magento.publish.products', required=True, ondelete='cascade')
    template_id = fields.Many2one('product.template', required=True, readonly=True)
    parent_sku = fields.Char(required=True)
