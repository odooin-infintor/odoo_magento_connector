from odoo import api, fields, models, _
from odoo.exceptions import UserError

MANAGER = "in_odoo_magento_connector.group_magento_manager"


class ProductTemplate(models.Model):
    _inherit = 'product.template'

    def action_publish_to_magento(self):
        self.check_access('read')
        return self.env['in.magento.publish.products']._open_for_products(self.product_variant_ids)


class ProductProduct(models.Model):
    _inherit = 'product.product'

    def action_publish_to_magento(self):
        return self.env['in.magento.publish.products']._open_for_products(self)


class ProductTemplatePublication(models.Model):
    _inherit = 'product.template'

    magento_template_binding_ids = fields.One2many('in.magento.template.binding', 'template_id', groups=MANAGER)
    magento_publication_state = fields.Selection([
        ('none', 'Not mapped'), ('managed', 'Published / linked'), ('paused', 'Unpublished'),
        ('pending', 'Publication pending'), ('mixed', 'Mixed by connection'),
    ], compute='_compute_magento_publication_state', groups=MANAGER)

    @api.depends_context('allowed_company_ids', 'uid')
    @api.depends('product_variant_ids.magento_binding_ids.external_id',
                 'product_variant_ids.magento_binding_ids.publication_state',
                 'magento_template_binding_ids.external_id', 'magento_template_binding_ids.publication_state')
    def _compute_magento_publication_state(self):
        products = self.env['in.magento.product.binding'].search([('product_id.product_tmpl_id', 'in', self.ids)])
        parents = self.env['in.magento.template.binding'].search([('template_id', 'in', self.ids)])
        by_template = {}
        for row in products:
            by_template.setdefault(row.product_id.product_tmpl_id.id, []).append(row)
        for row in parents:
            by_template.setdefault(row.template_id.id, []).append(row)
        for template in self:
            rows = by_template.get(template.id, [])
            states = {row.publication_state for row in rows}
            template.magento_publication_state = ('none' if not rows else
                'pending' if states & {'unpublish_pending', 'republish_pending'} or any(not row.external_id and row.publication_state == 'managed' for row in rows) else
                'managed' if states == {'managed'} else 'paused' if states == {'unpublished'} else 'mixed')

    def action_unpublish_from_magento(self):
        return self.env['in.magento.product.publication']._open(self, 'unpublish')

    def action_republish_to_magento(self):
        return self.env['in.magento.product.publication']._open(self, 'republish')


class ProductVariantPublication(models.Model):
    _inherit = 'product.product'

    magento_binding_ids = fields.One2many('in.magento.product.binding', 'product_id', groups=MANAGER)

    def action_unpublish_from_magento(self):
        return self.product_tmpl_id.action_unpublish_from_magento()

    def action_republish_to_magento(self):
        return self.product_tmpl_id.action_republish_to_magento()
