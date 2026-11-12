from odoo import models


class ProductCategory(models.Model):
    _inherit = 'product.category'

    def action_publish_to_magento(self):
        return self.env['in.magento.publish.categories']._open_for_categories(self)
