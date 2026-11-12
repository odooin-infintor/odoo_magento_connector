from contextlib import contextmanager
from copy import deepcopy
from datetime import timedelta
import json
from pathlib import Path
from unittest.mock import Mock, patch


from odoo import Command, fields
from odoo.exceptions import AccessError, UserError, ValidationError
from odoo.tests import Form
from odoo.tests.common import TransactionCase, tagged, new_test_user
from odoo.addons.queue_job.tests.common import trap_jobs
from odoo.addons.queue_job.job import Job

from ..services.client import MagentoError
from ..models.customers import fingerprint
from ..services.media import image_fingerprint


@tagged("post_install", "-at_install")
class TestMagentoConnector(TransactionCase):
    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.company = cls.env.company
        cls.currency = cls.env.ref("base.EUR")
        cls.currency.active = True
        cls.pricelist = cls.env["product.pricelist"].create({"name": "Magento EUR", "currency_id": cls.currency.id, "company_id": cls.company.id})
        cls.warehouse = cls.env["stock.warehouse"].search([("company_id", "=", cls.company.id)], limit=1)
        cls.shipping = cls.env["product.product"].create({"name": "Magento Freight", "type": "service"})
        cls.product = cls.env["product.product"].create({"name": "Test Product", "default_code": "SIM-001", "list_price": 50})
        cls.instance = cls.env["in.magento.instance"].create({
            "name": "Test Magento", "base_url": "https://shop.example.test",
            "auth_type": "bearer", "access_token": "local-demo-token", "enabled": True,
            "warehouse_id": cls.warehouse.id, "pricelist_id": cls.pricelist.id,
            "shipping_product_id": cls.shipping.id, "store_ids": "1", "import_from": "2026-01-01 00:00:00",
        })
        cls.product_binding = cls.env["in.magento.product.binding"].create({
            "instance_id": cls.instance.id, "product_id": cls.product.id, "sku": "SIM-001",
        })
        cls.tax = cls.env["account.tax"].create({"name": "Test 20%", "amount": 20, "amount_type": "percent",
                                                "type_tax_use": "sale", "company_id": cls.company.id, "price_include_override": "tax_excluded"})
        cls.env["in.magento.tax.mapping"].create({"name": "20%", "instance_id": cls.instance.id, "rate": 20, "tax_ids": [Command.set(cls.tax.ids)]})
        cls.env["in.magento.tax.mapping"].create({"name": "Zero", "instance_id": cls.instance.id, "rate": 0})
        cls.fixture = json.loads((Path(__file__).parent / "fixtures/order.json").read_text())

    def _inbox(self, payload=None):
        payload = deepcopy(payload or self.fixture)
        return self.env["in.magento.order.binding"].create({"instance_id": self.instance.id,
            "external_id": str(payload["entity_id"]), "name": payload["increment_id"], "payload": payload})

    def _setup_wizard(self, instance=None):
        return self.env['in.magento.setup'].with_context(default_instance_id=(instance or self.instance).id).create({})

    def _diagnostic_client(self):
        client = Mock()
        def request(method, path, **kwargs):
            self.assertEqual(method, 'GET')
            if path == 'store/storeViews':
                return [{'id': 1, 'code': 'default', 'website_id': 1, 'name': 'Default', 'is_active': True}]
            if path == 'store/storeGroups':
                return [{'id': 1, 'name': 'Main', 'root_category_id': 2}]
            if path == 'products/attribute-sets/sets/list':
                return {'items': [{'attribute_set_id': 4, 'attribute_set_name': 'Default'}], 'total_count': 1}
            if path == 'products/attribute-sets/4':
                return {'attribute_set_id': 4}
            if path == 'categories/2':
                return {'id': 2}
            if path == 'inventory/sources':
                return {'items': [{'source_code': 'default', 'name': 'Default source', 'enabled': True}]}
            if path == 'inventory/sources/default':
                return {'source_code': 'default', 'enabled': True}
            return {'items': [], 'total_count': 0}
        client.request.side_effect = request
        return client

    def test_operations_job_scope_includes_batch_and_excludes_other_connection(self):
        other = self.env['in.magento.instance'].create({'name': 'Other same company',
            'base_url': 'https://other.example.test', 'warehouse_id': self.warehouse.id,
            'pricelist_id': self.pricelist.id})
        product = self.env['product.product'].create({'name': 'Second batch product'})
        binding = self.env['in.magento.product.binding'].create({'instance_id': self.instance.id,
            'product_id': product.id, 'sku': 'OPS-SECOND'})
        batch = Job((self.product_binding | binding)._job_export_prices)
        batch.store()
        failure = Job(self.instance._job_poll_orders)
        failure.set_failed(exc_name='UserError', exc_message='example')
        failure.store()
        foreign = Job(other._job_probe_worker)
        foreign.store()
        self.assertEqual(batch.db_record().magento_instance_id, self.instance)
        self.assertEqual(self.instance.waiting_job_count, 1)
        self.assertEqual(self.instance.failed_job_count, 1)
        self.assertEqual(other.waiting_job_count, 1)
        selected = self.env['queue.job'].search(self.instance.action_jobs()['domain'])
        self.assertIn(batch.db_record(), selected)
        self.assertNotIn(foreign.db_record(), selected)
        self.assertTrue(self.instance.oldest_waiting_at)
        self.assertFalse(self.instance.last_success_at)
        self._inbox()
        self.instance.invalidate_recordset()
        self.assertEqual(self.instance.pending_order_count, 1)

    def test_operations_probe_is_local_and_allowed_while_paused(self):
        self.instance.enabled = False
        with self._mock_api(Mock()) as client, trap_jobs() as trap:
            self.instance.action_probe_worker()
            trap.assert_jobs_count(1, only=self.instance._job_probe_worker)
            self.instance._job_probe_worker()
            client.request.assert_not_called()
        self.assertTrue(self.instance.worker_probe_completed_at)
        self.assertFalse(self.instance.last_success_at)

    def _activity_log(self, occurred_at, **values):
        return self.env['in.magento.sync.log'].sudo().create(dict({
            'instance_id': self.instance.id, 'occurred_at': occurred_at,
            'direction': 'export', 'operation': 'update', 'status': 'success',
            'trigger': 'manual', 'summary': 'Activity test', 'reference': 'ACTIVITY-TEST',
        }, **values))

    def test_operations_today_count_timezone_boundaries_and_connection_scope(self):
        other = self.env['in.magento.instance'].create({'name': 'Activity other store',
            'base_url': 'https://activity.example.test', 'warehouse_id': self.warehouse.id,
            'pricelist_id': self.pricelist.id})
        self._activity_log('2026-10-14 21:59:59')
        included = self._activity_log('2026-10-14 22:00:00')
        utc_included = self._activity_log('2026-10-15 22:00:00')
        self._activity_log('2026-10-15 10:00:00', status='failed')
        self._activity_log('2026-10-15 10:00:00', status='retry')
        self._activity_log('2026-10-15 10:00:00', status='skipped')
        self._activity_log('2026-10-15 10:00:00', instance_id=other.id)
        now = fields.Datetime.to_datetime('2026-10-15 12:00:00')
        with patch.object(fields.Datetime, 'now', return_value=now):
            berlin = self.instance.with_context(tz='Europe/Berlin')
            self.assertEqual(berlin.successful_syncs_today, 1)
            self.assertEqual(self.env['in.magento.sync.log'].search(
                berlin.action_successful_syncs_today()['domain']), included)
            utc = self.instance.with_context(tz='UTC')
            self.assertEqual(utc.successful_syncs_today, 1)
            self.assertEqual(self.env['in.magento.sync.log'].search(
                utc.action_successful_syncs_today()['domain']), utc_included)

    def test_operations_today_respects_daylight_saving_days(self):
        for day, hours in [('2026-03-29 12:00:00', 23), ('2026-10-25 12:00:00', 25)]:
            with patch.object(fields.Datetime, 'now', return_value=fields.Datetime.to_datetime(day)):
                domain = self.instance.with_context(tz='Europe/Berlin')._today_success_domain()
                self.assertEqual((domain[2][2] - domain[1][2]).total_seconds(), hours * 3600)

    def test_operations_recent_activity_bounded_ordered_and_escaped(self):
        now = fields.Datetime.now()
        logs = self.env['in.magento.sync.log']
        for index in range(12):
            logs |= self._activity_log(now - timedelta(minutes=12-index), reference=f'ACTIVITY-{index}')
        latest = self._activity_log(now, reference='<script>alert(1)</script>', change_ids=[Command.create({
            'field_name': 'price', 'previous_value': '40', 'new_value': '50'})])
        older_inserted_last = self._activity_log(now - timedelta(days=1), reference='OLD-INSERTED-LAST')
        self.assertEqual(self.instance.recent_activity_ids, latest | logs.sorted('occurred_at', reverse=True)[:9])
        self.assertNotIn(older_inserted_last, self.instance.recent_activity_ids)
        self.assertEqual(latest.activity_details, 'price: 40 → 50')
        html = str(self.instance.recent_activity_html)
        self.assertNotIn('<script>', html)
        self.assertIn('price: 40 → 50', html)
        self.assertEqual(html.count('class="border-top py-2"'), 3)
        latest.status = 'failed'
        self.assertFalse(latest.activity_details)
        self.instance.invalidate_recordset(['recent_activity_html', 'recent_activity_ids'])
        self.assertIn('Failed', str(self.instance.recent_activity_html))
        stock = self._activity_log(now, operation='inventory', change_ids=[Command.create({
            'field_name': 'default', 'previous_value': '{"quantity": 0, "status": 0}',
            'new_value': '{"quantity": 15.0, "status": 1}'})])
        self.assertEqual(stock.activity_details, 'Stock (default): 0 → 15')

    def test_operations_recent_activity_excludes_other_connection(self):
        other = self.env['in.magento.instance'].create({'name': 'Recent other store',
            'base_url': 'https://recent.example.test', 'warehouse_id': self.warehouse.id,
            'pricelist_id': self.pricelist.id})
        local = self._activity_log(fields.Datetime.now())
        foreign = self._activity_log(fields.Datetime.now(), instance_id=other.id, reference='FOREIGN-STORE')
        self.assertEqual(self.instance.recent_activity_ids, local)
        self.assertEqual(other.recent_activity_ids, foreign)
        self.assertNotIn('FOREIGN-STORE', str(self.instance.recent_activity_html))

    def test_diagnostics_reads_only_and_detects_configuration_changes(self):
        self.instance.enabled = False
        with self._mock_api(self._diagnostic_client()):
            self.instance._job_run_diagnostics()
        self.assertEqual(self.instance.diagnostics_state, 'warning')
        self.assertTrue(self.instance.diagnostics_current)
        self.assertNotIn(self.instance.access_token, json.dumps(self.instance.diagnostics_results))
        self.instance.attribute_set_id = 5
        self.assertFalse(self.instance.diagnostics_current)

    def test_diagnostics_accepts_empty_magento_projected_collection(self):
        client = self._diagnostic_client()
        original = client.request.side_effect
        client.request.side_effect = lambda method, path, **kwargs: (
            {'items': None, 'total_count': 0} if path == 'products'
            else original(method, path, **kwargs))
        with self._mock_api(client):
            self.instance._job_run_diagnostics()
        checks = {row['code']: row for row in self.instance.diagnostics_results}
        self.assertEqual(checks['products']['status'], 'pass')

    def test_diagnostics_rejects_null_nonempty_or_unknown_collection(self):
        for response in ({'items': None, 'total_count': 3}, {'items': None}, {'total_count': 0}):
            with self.subTest(response=response):
                client = self._diagnostic_client()
                original = client.request.side_effect
                client.request.side_effect = lambda method, path, **kwargs: (
                    response if path == 'products' else original(method, path, **kwargs))
                with self._mock_api(client):
                    self.instance._job_run_diagnostics()
                checks = {row['code']: row for row in self.instance.diagnostics_results}
                self.assertEqual(checks['products']['status'], 'error')

    def test_diagnostics_invalid_scope_and_failed_permission_are_visible_without_secrets(self):
        self.instance.store_ids = '999'
        client = self._diagnostic_client()
        original = client.request.side_effect
        def request(method, path, **kwargs):
            if path == 'products':
                raise MagentoError('private secret customer@example.test', 403)
            return original(method, path, **kwargs)
        client.request.side_effect = request
        with self._mock_api(client):
            self.instance._job_run_diagnostics()
        checks = {row['code']: row for row in self.instance.diagnostics_results}
        self.assertEqual(checks['scope']['status'], 'error')
        self.assertEqual(checks['products']['status'], 'error')
        self.assertEqual(checks['attribute_set']['status'], 'pass')
        self.assertNotIn('private secret', json.dumps(checks))
        self.assertNotIn('customer@example.test', json.dumps(checks))

    def test_diagnostics_neutralized_and_missing_credentials_make_no_remote_calls(self):
        self.env['ir.config_parameter'].sudo().set_param('database.is_neutralized', 'true')
        with self._mock_api(Mock()) as client:
            self.instance._job_run_diagnostics()
            client.request.assert_not_called()
        self.assertEqual(self.instance.diagnostics_state, 'error')
        self.env['ir.config_parameter'].sudo().set_param('database.is_neutralized', 'false')
        self.instance.access_token = False
        with self._mock_api(Mock()) as client:
            self.instance._job_run_diagnostics()
            client.request.assert_not_called()
        self.assertEqual(self.instance.diagnostics_state, 'error')

    def test_diagnostics_optional_flows_require_mappings_and_check_permissions(self):
        self.instance.write({'auto_import_orders': True, 'shipping_product_id': False,
            'auto_export_inventory': True, 'auto_export_shipments': True,
            'import_invoices': True, 'auto_import_customers': True, 'customer_store_id': 99})
        with self._mock_api(self._diagnostic_client()):
            self.instance._job_run_diagnostics()
        checks = {row['code']: row['status'] for row in self.instance.diagnostics_results}
        for key in ('order_setup', 'sources', 'journal', 'customer_scope'):
            self.assertEqual(checks[key], 'error')
        for key in ('orders', 'invoices', 'customers/search', 'shipments'):
            self.assertEqual(checks[key], 'pass')

    def test_setup_creates_paused_connection_and_clears_transient_credentials(self):
        wizard = self.env['in.magento.setup'].create({'name': 'Guided new store',
            'base_url': 'https://new.example.test', 'warehouse_id': self.warehouse.id,
            'pricelist_id': self.pricelist.id, 'auth_type': 'bearer', 'access_token': 'temporary-test-token'})
        with trap_jobs() as trap:
            wizard.action_next()
            trap.assert_jobs_count(0)
        self.assertEqual(wizard.stage, 'scope')
        self.assertFalse(wizard.instance_id.enabled)
        self.assertFalse(wizard.instance_id.auto_import_orders)
        self.assertEqual(wizard.instance_id.access_token, 'temporary-test-token')
        self.assertFalse(wizard.access_token)

    def test_setup_existing_connection_preserves_tokens_and_automation(self):
        self.instance.auto_export_products = True
        wizard = self._setup_wizard()
        self.assertFalse(wizard.access_token)
        wizard.action_next()
        self.assertEqual(self.instance.access_token, 'local-demo-token')
        self.assertTrue(self.instance.auto_export_products)
        self.assertTrue(self.instance.enabled)

    def test_setup_discovery_selection_and_inventory_mapping(self):
        wizard = self._setup_wizard()
        wizard.action_next()
        with self._mock_api(self._diagnostic_client()):
            wizard.action_discover()
        self.assertEqual(wizard.export_store_id.code, 'all')
        self.assertEqual(wizard.import_store_ids.remote_id, 1)
        self.assertEqual(wizard.attribute_option_id.remote_id, 4)
        wizard.action_next()
        self.assertEqual(wizard.stage, 'mappings')
        wizard.source_option_id = wizard.option_ids.filtered(lambda o: o.kind == 'source')
        wizard.action_add_source()
        self.assertEqual(self.instance.inventory_source_ids.source_code, 'default')
        with self.assertRaisesRegex(UserError, 'already has a mapping'):
            wizard.action_add_source()

    def test_setup_discovery_optional_inventory_failure_and_invalid_response(self):
        wizard = self._setup_wizard()
        client = self._diagnostic_client()
        original = client.request.side_effect
        def request(method, path, **kwargs):
            if path == 'inventory/sources':
                raise MagentoError('private-token', 403)
            return original(method, path, **kwargs)
        client.request.side_effect = request
        with self._mock_api(client):
            wizard.action_discover()
        self.assertTrue(wizard.attribute_option_id)
        self.assertIn('failed', wizard.discovery_note)
        self.assertNotIn('private-token', wizard.discovery_note)
        client.request.side_effect = lambda *a, **kw: {}
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'Invalid'):
            wizard.action_discover()

    def test_setup_rejects_choices_from_another_wizard(self):
        first, second = self._setup_wizard(), self._setup_wizard()
        with self._mock_api(self._diagnostic_client()):
            first.action_discover()
            second.action_discover()
        first.stage = 'scope'
        first.attribute_option_id = second.attribute_option_id
        with self.assertRaisesRegex(UserError, 'this setup wizard'):
            first.action_next()

    def test_setup_enable_requires_current_diagnostics_and_warning_review(self):
        self.instance.enabled = False
        wizard = self._setup_wizard()
        wizard.write({'stage': 'review', 'enable_after_setup': True})
        with self.assertRaisesRegex(UserError, 'Run diagnostics'):
            wizard.action_finish()
        with self._mock_api(self._diagnostic_client()):
            self.instance._job_run_diagnostics()
        with self.assertRaisesRegex(UserError, 'acknowledge'):
            wizard.action_finish()
        wizard.accept_warnings = True
        self.instance.order_interval = 7
        with self.assertRaisesRegex(UserError, 'Run diagnostics'):
            wizard.action_finish()
        with self._mock_api(self._diagnostic_client()):
            self.instance._job_run_diagnostics()
        with trap_jobs() as trap:
            wizard.action_finish()
            trap.assert_jobs_count(0)
        self.assertTrue(self.instance.enabled)
        self.assertTrue(self.instance.setup_completed_at)

    def test_setup_finish_without_enabling_preserves_pause(self):
        self.instance.enabled = False
        wizard = self._setup_wizard()
        wizard.stage = 'review'
        wizard.action_finish()
        self.assertFalse(self.instance.enabled)
        self.assertTrue(self.instance.setup_completed_at)

    def test_operations_and_setup_require_manager_access(self):
        viewer = new_test_user(self.env, login='operations_viewer',
            groups='base.group_user,in_odoo_magento_connector.group_magento_user')
        for method in ('action_open_setup', 'action_run_diagnostics', 'action_probe_worker', 'action_operations', 'action_jobs'):
            with self.assertRaises(AccessError):
                getattr(self.instance.with_user(viewer), method)()
        with self.assertRaises(AccessError):
            self.instance.with_user(viewer).read(['diagnostics_results'])
        with self.assertRaises(AccessError):
            self.env['in.magento.setup'].with_user(viewer).create({'name': 'Forbidden'})

    def test_operations_diagnostics_html_escapes_remote_labels(self):
        self.instance.diagnostics_results = [{'label': '<script>alert(1)</script>', 'status': 'warning', 'detail': '<img src=x onerror=alert(1)>'}]
        html = str(self.instance.diagnostics_html)
        self.assertNotIn('<script>', html)
        self.assertNotIn('<img ', html)

    def test_operations_manager_cannot_access_another_company_connection(self):
        other = self.env['res.company'].create({'name': 'Operations other company'})
        manager = new_test_user(self.env, login='operations_other_manager', company_id=other.id,
            company_ids=[Command.set(other.ids)],
            groups='base.group_user,in_odoo_magento_connector.group_magento_manager')
        connection = self.instance.with_user(manager).with_context(allowed_company_ids=other.ids)
        for method in ('action_open_setup', 'action_run_diagnostics', 'action_probe_worker',
                       'action_operations', 'action_jobs'):
            with self.assertRaises(AccessError):
                getattr(connection, method)()
        with self.assertRaises(AccessError):
            connection.read(['failed_job_count', 'diagnostics_results'])

    def test_setup_does_not_reset_import_cursor(self):
        self.instance.order_cursor = fields.Datetime.now()
        previous = self.instance.order_cursor
        wizard = self._setup_wizard()
        wizard.import_from = fields.Datetime.now() - timedelta(days=20)
        self.assertEqual(self.instance.order_cursor, previous)

    @contextmanager
    def _mock_api(self, client):
        @contextmanager
        def fake(record, **kwargs):
            yield client
        with patch.object(type(self.instance), "_client", fake):
            yield client

    def test_order_total_addresses_and_idempotency(self):
        inbox = self._inbox()
        order_id = inbox._job_import()
        order = self.env["sale.order"].browse(order_id)
        self.assertEqual(order.state, "draft")
        self.assertAlmostEqual(order.amount_total, 114)
        self.assertAlmostEqual(order.amount_tax, 19)
        self.assertEqual(order.partner_shipping_id.street, "Example Street 2")
        self.assertEqual(order.partner_invoice_id.street, "Example Street 1")
        self.assertEqual(order.in_magento_payment_method, "checkmo")
        self.assertEqual(inbox._job_import(), order_id)

    def test_mismatched_totals_roll_back_order_and_customer(self):
        payload = deepcopy(self.fixture)
        payload["grand_total"] += 1
        inbox = self._inbox(payload)
        before = self.env["res.partner"].search_count([])
        with self.assertRaisesRegex(UserError, "totals differ"):
            inbox._job_import()
        self.assertFalse(inbox.sale_order_id)
        self.assertFalse(self.env["sale.order"].search([("in_magento_external_id", "=", inbox.external_id)]))
        self.assertEqual(self.env["res.partner"].search_count([]), before)

    def test_missing_product_is_quarantined(self):
        payload = deepcopy(self.fixture)
        payload["items"][0]["sku"] = "UNMAPPED"
        with self.assertRaisesRegex(UserError, "UNMAPPED"):
            self._inbox(payload)._job_import()

    def test_foreign_currency_is_not_silently_converted(self):
        payload = deepcopy(self.fixture)
        payload["order_currency_code"] = "USD"
        with self.assertRaisesRegex(UserError, "currency"):
            self._inbox(payload)._job_import()

    def test_existing_customer_reused_with_fresh_address_snapshot(self):
        first = self._inbox()
        first._job_import()
        second_payload = deepcopy(self.fixture)
        second_payload.update(entity_id=1002, increment_id="SIM-000002")
        second_payload["billing_address"]["street"] = ["Changed address"]
        second = self._inbox(second_payload)
        second._job_import()
        self.assertEqual(first.sale_order_id.partner_id, second.sale_order_id.partner_id)
        self.assertEqual(first.sale_order_id.partner_invoice_id.street, "Example Street 1")
        self.assertEqual(second.sale_order_id.partner_invoice_id.street, "Changed address")

    def test_wrong_store_is_rejected(self):
        payload = deepcopy(self.fixture)
        payload["store_id"] = 999
        with self.assertRaisesRegex(UserError, "unconfigured"):
            self._inbox(payload)._job_import()

    def test_pending_payment_cannot_be_auto_confirmed(self):
        self.instance.auto_confirm_orders = True
        payload = deepcopy(self.fixture)
        payload["state"] = "pending_payment"
        inbox = self._inbox(payload)
        with self.assertRaisesRegex(UserError, "Automatic confirmation"):
            inbox._job_import()
        self.assertFalse(inbox.sale_order_id)

    def test_poll_persists_inbox_and_queues_processing(self):
        client = Mock()
        client.request.return_value = {"items": [self.fixture]}
        with self._mock_api(client), trap_jobs() as jobs:
            self.instance._job_poll_orders()
            jobs.assert_jobs_count(1)
            self.assertEqual(jobs.enqueued_jobs[0].priority, 5)
        inbox = self.env["in.magento.order.binding"].search([("instance_id", "=", self.instance.id)])
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox.state, "pending")
        self.assertTrue(self.instance.order_cursor)
        with self._mock_api(client), trap_jobs():
            self.instance._job_poll_orders()
        self.assertEqual(self.env["in.magento.order.binding"].search_count([("instance_id", "=", self.instance.id)]), 1)

    def test_page_does_not_advance_checkpoint_until_last_page(self):
        self.instance.order_batch_size = 1
        client = Mock()
        client.request.return_value = {"items": [self.fixture]}
        with self._mock_api(client), trap_jobs() as jobs:
            self.instance._job_poll_orders()
            jobs.assert_jobs_count(2)
            self.assertEqual([job.priority for job in jobs.enqueued_jobs], [5, 5])
        self.assertFalse(self.instance.order_cursor)

    def test_existing_remote_sku_requires_explicit_link(self):
        client = Mock()
        client.request.return_value = {"id": 101, "sku": "SIM-001", "type_id": "simple"}
        with self._mock_api(client):
            with self.assertRaisesRegex(UserError, "Link Existing"):
                self.product_binding._job_export()
            self.assertEqual(client.request.call_count, 1)
            self.product_binding.action_link()
        self.assertEqual(self.product_binding.external_id, "101")
        self.assertFalse(self.product_binding.last_sync)

    def test_new_product_is_disabled_by_default(self):
        client = Mock()
        client.request.side_effect = [MagentoError("not found", 404), {"id": 102}]
        with self._mock_api(client):
            self.product_binding._job_export()
        payload = client.request.call_args.kwargs["payload"]
        self.assertEqual(payload["product"]["status"], 2)
        self.assertEqual(self.product_binding.external_id, "102")

    def test_queued_actions_do_not_make_inline_http_calls(self):
        with patch.object(type(self.instance), "_client") as api, trap_jobs() as jobs:
            self.instance.action_import_orders()
            self.product_binding.action_export()
            jobs.assert_jobs_count(2)
            self.assertEqual([job.priority for job in jobs.enqueued_jobs], [5, 10])
            api.assert_not_called()

    def test_catalog_scheduler_skips_unchanged_and_worker_rechecks(self):
        self.instance.auto_export_products = True
        self.product_binding.external_id = '101'
        self.product_binding.last_fingerprint = self.product_binding._fingerprint()
        with patch.object(type(self.instance), '_client') as api, trap_jobs() as jobs:
            self.instance._cron_schedule()
            jobs.assert_jobs_count(0)
            self.product.list_price = 55
            self.instance._cron_schedule()
            jobs.assert_jobs_count(1, only=self.product_binding._job_export_if_changed)
            # Reverting while the job waits must not produce a remote write.
            self.product.list_price = 50
            jobs.perform_enqueued_jobs()
            api.assert_not_called()

    def test_catalog_scheduler_detects_pricelist_dependency(self):
        self.instance.auto_export_products = True
        self.product_binding.external_id = '101'
        self.product_binding.last_fingerprint = self.product_binding._fingerprint()
        before = self.product.write_date
        self.env['product.pricelist.item'].create({'pricelist_id': self.pricelist.id,
            'applied_on': '0_product_variant', 'product_id': self.product.id,
            'compute_price': 'fixed', 'fixed_price': 42})
        with trap_jobs() as jobs:
            self.instance._cron_schedule()
            jobs.assert_jobs_count(1, only=self.product_binding._job_export_if_changed)
        self.assertEqual(self.product.write_date, before)

    def test_catalog_scheduler_isolates_invalid_mapping(self):
        self.instance.auto_export_products = True
        self.product_binding.external_id = '101'
        category = self.env['in.magento.category.binding'].create({
            'instance_id': self.instance.id,
            'category_id': self.env['product.category'].create({'name': 'Not published'}).id})
        self.product_binding.category_ids = category
        other = self.env['in.magento.product.binding'].create({'instance_id': self.instance.id,
            'product_id': self.shipping.id, 'sku': 'FREIGHT', 'external_id': '102'})
        with patch.object(type(self.instance), '_client') as api, trap_jobs() as jobs:
            self.instance._cron_schedule()
            jobs.assert_jobs_count(2)
            jobs.assert_jobs_count(1, only=other._job_export_if_changed)
            jobs.assert_jobs_count(1, only=self.product_binding._job_export_if_changed)
            api.assert_not_called()

    def test_order_manual_retry_keeps_priority(self):
        inbox = self._inbox()
        with trap_jobs() as jobs:
            inbox.action_retry()
            jobs.assert_enqueued_job(inbox._job_import, properties={'priority': 5})

    def test_catalog_stock_followup_only_publication_or_disabling(self):
        self.product.is_storable = True
        self.product_binding.sync_inventory = True
        client = Mock()
        client.request.side_effect = [MagentoError('not found', 404), {'id': 101}]
        with self._mock_api(client), trap_jobs() as jobs:
            self.product_binding._job_export()
            jobs.assert_jobs_count(1, only=self.product_binding._job_export_inventory)
        client.request.side_effect = None
        client.request.return_value = {'id': 101, 'sku': 'SIM-001', 'type_id': 'simple'}
        self.product.list_price = 55
        with self._mock_api(client), trap_jobs() as jobs:
            self.product_binding._job_export_if_changed()
            jobs.assert_jobs_count(0)
        self.product.sale_ok = False
        with self._mock_api(client), trap_jobs() as jobs:
            self.product_binding._job_export_if_changed()
            jobs.assert_jobs_count(1, only=self.product_binding._job_export_inventory)

    def test_linked_product_first_export_initializes_stock(self):
        self.product.is_storable = True
        self.product_binding.write({'external_id': '101', 'sync_inventory': True})
        client = Mock()
        client.request.return_value = {'id': 101, 'sku': 'SIM-001', 'type_id': 'simple'}
        with self._mock_api(client), trap_jobs() as jobs:
            self.product_binding._job_export()
            jobs.assert_jobs_count(1, only=self.product_binding._job_export_inventory)

    def test_disabled_connection_blocks_jobs(self):
        self.instance.enabled = False
        with self.assertRaisesRegex(UserError, "disabled"):
            self.product_binding._job_export()
        with self.assertRaisesRegex(UserError, "disabled"):
            self._inbox()._job_import()

    def test_viewer_cannot_read_credentials_or_run_exports(self):
        viewer = new_test_user(self.env, login="magento_viewer", groups="in_odoo_magento_connector.group_magento_user")
        instance = self.instance.with_user(viewer)
        with self.assertRaises(AccessError):
            instance.read(["access_token"])
        with self.assertRaises(AccessError):
            instance.action_import_orders()

    def test_other_company_not_visible(self):
        other = self.env["res.company"].create({"name": "Other Magento Company"})
        viewer = new_test_user(self.env, login="other_magento_viewer", company_id=other.id,
                               company_ids=[Command.set(other.ids)], groups="in_odoo_magento_connector.group_magento_user")
        self.assertFalse(self.env["in.magento.instance"].with_user(viewer).search([("id", "=", self.instance.id)]))

    def test_queued_catalog_and_order_round_trip(self):
        client = Mock()
        remote_product = {"id": 101, "sku": "SIM-001", "type_id": "simple"}

        def response(method, path, **kwargs):
            if (method, path) == ("GET", "store/storeViews"):
                return [{"id": 1, "code": "default"}]
            if (method, path) == ("POST", "categories"):
                return {"id": 3, **kwargs["payload"]["category"]}
            if (method, path) == ("GET", "products/SIM-001"):
                return remote_product
            if (method, path) == ("PUT", "products/SIM-001"):
                remote_product.update(kwargs["payload"]["product"])
                return remote_product
            if (method, path) == ("GET", "orders"):
                return {"items": [self.fixture]}
            self.fail(f"Unexpected API call: {method} {path}")

        client.request.side_effect = response
        category = self.env["in.magento.category.binding"].create({
            "instance_id": self.instance.id,
            "category_id": self.env["product.category"].create({"name": "Test category"}).id,
        })
        with self._mock_api(client):
            self.instance.action_test_connection()
            self.product_binding.action_link()
            with trap_jobs() as jobs:
                category.action_export()
                jobs.perform_enqueued_jobs()
            self.product_binding.category_ids = category
            with trap_jobs() as jobs:
                self.product_binding.action_export()
                self.instance.action_import_orders()
                jobs.perform_enqueued_jobs()
                # Polling persists the inbox, then schedules import separately.
                jobs.assert_jobs_count(1)
                jobs.perform_enqueued_jobs()
        inbox = self.instance.order_ids
        self.assertEqual(len(inbox), 1)
        self.assertEqual(inbox.state, "done")
        self.assertEqual(inbox.sale_order_id.amount_total, 114)
        self.assertEqual(remote_product["custom_attributes"][-1]["value"], [category.external_id])
        self.assertTrue(self.product_binding.last_sync)

    def test_mapped_connection_cannot_be_repointed(self):
        with self.assertRaisesRegex(UserError, "mapped connection"):
            self.instance.base_url = "https://other.example.test"

    def test_neutralized_database_blocks_sync(self):
        self.env["ir.config_parameter"].sudo().set_param("database.is_neutralized", True)
        with self.assertRaisesRegex(UserError, "neutralized"):
            self.product_binding._job_export()

    def test_sync_log_price_difference_and_noop(self):
        client = Mock()
        remote = {"id": 101, **self.product_binding._payload()['product']}
        self.product_binding.external_id = '101'
        self.product.list_price = 55
        client.request.side_effect = [remote, {"id": 101}]
        with self._mock_api(client):
            self.product_binding.with_context(magento_sync_trigger='automatic')._job_export()
        log = self.instance.sync_log_ids
        self.assertEqual(len(log), 1)
        self.assertEqual(log.trigger, 'automatic')
        self.assertEqual(log.previous_source, 'magento')
        self.assertEqual(log.currency_code, 'EUR')
        price = log.change_ids.filtered(lambda change: change.field_name == 'price')
        self.assertEqual(json.loads(price.previous_value), 50)
        self.assertEqual(json.loads(price.new_value), 55)
        self.assertFalse(log.change_ids.filtered(lambda change: change.field_name == 'name'))
        client.reset_mock()
        with self._mock_api(client):
            self.product_binding._job_export_if_changed()
        client.request.assert_not_called()
        self.assertEqual(len(self.instance.sync_log_ids), 1)
        self.assertEqual(self.instance.sync_log_count, 1)
        self.assertEqual(self.instance.action_sync_logs()['domain'], [('instance_id', '=', self.instance.id)])

    def test_sync_log_import_private_data_and_duplicate(self):
        inbox = self._inbox()
        inbox._job_import()
        inbox._job_import()
        log = self.instance.sync_log_ids
        self.assertEqual(len(log), 1)
        self.assertEqual(log.sale_order_id, inbox.sale_order_id)
        self.assertEqual(log.direction, 'import')
        values = ' '.join(log.change_ids.mapped('new_value'))
        self.assertNotIn(self.fixture['customer_email'], values)
        self.assertNotIn('Example Street', values)
        self.assertNotIn('billing_address', values)
        self.assertEqual(json.loads(log.change_ids.filtered(lambda c: c.field_name == 'total').new_value), 114)

    def test_sync_log_failure_after_business_rollback(self):
        payload = deepcopy(self.fixture)
        payload['grand_total'] += 1
        inbox = self._inbox(payload)
        job = Job(inbox._job_import)
        job.store()
        job.set_started()
        job.store()
        with self.assertRaises(UserError):
            with self.env.cr.savepoint():
                job.perform()
        self.assertFalse(self.instance.sync_log_ids)
        self.assertFalse(inbox.sale_order_id)
        job.set_failed(exc_name='odoo.exceptions.UserError', exc_message='secret-token customer@example.test')
        job.store()
        log = self.instance.sync_log_ids
        self.assertEqual(log.status, 'failed')
        self.assertEqual(log.job_id, job.db_record())
        self.assertFalse(log.change_ids)
        self.assertNotIn('secret-token', log.error)
        self.assertNotIn('customer@example.test', log.error)
        job.store()
        self.assertEqual(len(self.instance.sync_log_ids), 1)

    def test_sync_log_retry_and_trigger_survive_queue_serialization(self):
        job = Job(self.product_binding.with_context(magento_sync_trigger='automatic')._job_export_if_changed)
        job.store()
        job = Job.load(self.env, job.uuid)
        self.assertEqual(job.recordset.env.context['magento_sync_trigger'], 'automatic')
        job.set_started()
        job.store()
        job.retry = 1
        job.postpone(result='private error', seconds=60)
        job.set_pending(reset_retry=False)
        job.store()
        log = self.instance.sync_log_ids
        self.assertEqual(log.status, 'retry')
        self.assertEqual(log.trigger, 'automatic')
        self.assertEqual(log.attempt, 1)
        self.assertNotIn('private error', log.error)

    def test_sync_log_readonly_and_company_isolation(self):
        log = self.env['in.magento.sync.log']._record(self.product_binding, direction='export',
            operation='update', summary='Test', before={'price': 50}, after={'price': 55})
        viewer = new_test_user(self.env, login='log_viewer', groups='in_odoo_magento_connector.group_magento_user')
        self.assertEqual(log.with_user(viewer).summary, 'Test')
        with self.assertRaises(AccessError):
            log.with_user(viewer).write({'summary': 'Tampered'})
        with self.assertRaises(AccessError):
            log.change_ids.with_user(viewer).write({'new_value': '999'})
        manager = new_test_user(self.env, login='log_manager', groups='in_odoo_magento_connector.group_magento_manager')
        with self.assertRaises(AccessError):
            log.with_user(manager).unlink()
        other = self.env['res.company'].create({'name': 'Log other company'})
        viewer.write({'company_id': other.id, 'company_ids': [Command.set(other.ids)]})
        self.assertFalse(self.env['in.magento.sync.log'].with_user(viewer).search([('id', '=', log.id)]))
        self.assertFalse(self.env['in.magento.sync.log.change'].with_user(viewer).search([('log_id', '=', log.id)]))

    def test_sync_log_retention(self):
        logs = self.env['in.magento.sync.log']
        old = logs._record(self.instance, direction='import', operation='poll', summary='Old')
        old.write({'occurred_at': fields.Datetime.now() - timedelta(days=91)})
        fresh = logs._record(self.instance, direction='import', operation='poll', summary='Fresh')
        self.instance.sync_log_retention_days = 0
        logs._gc_sync_logs()
        self.assertTrue(old.exists())
        self.instance.sync_log_retention_days = 90
        logs._gc_sync_logs()
        self.assertFalse(old.exists())
        self.assertTrue(fresh.exists())

    def test_sync_log_category_remote_previous_values(self):
        category = self.env['in.magento.category.binding'].create({
            'instance_id': self.instance.id, 'category_id': self.env['product.category'].create({'name': 'New title'}).id, 'external_id': '3'})
        client = Mock()
        client.request.side_effect = [{'id': 3, 'name': 'Old title', 'parent_id': 2}, {'id': 3}]
        with self._mock_api(client):
            category._job_export()
        log = self.instance.sync_log_ids
        self.assertEqual(log.category_binding_id, category)
        name = log.change_ids.filtered(lambda change: change.field_name == 'name')
        self.assertEqual(json.loads(name.previous_value), 'Old title')
        self.assertEqual(json.loads(name.new_value), category.category_id.name)

    def _publish_wizard(self, products, **values):
        return self.env['in.magento.publish.products'].create({
            'instance_id': self.instance.id, 'product_ids': [Command.set(products.ids)], **values})

    def test_publish_product_and_bulk_entry_points(self):
        action = self.product.product_tmpl_id.action_publish_to_magento()
        self.assertEqual(action['res_model'], 'in.magento.publish.products')
        wizard = self.env[action['res_model']].with_context(action['context']).create({})
        self.assertEqual(wizard.product_ids, self.product)
        self.assertEqual(wizard.instance_id, self.instance)
        action = self.product.action_publish_to_magento()
        self.assertEqual(action['res_model'], 'in.magento.publish.products')
        action = self.env.ref('in_odoo_magento_connector.action_publish_templates_magento').with_context(
            active_model='product.template', active_ids=self.product.product_tmpl_id.ids).run()
        self.assertEqual(action['res_model'], 'in.magento.publish.products')
        for model, view_id in [('product.template', 'product.product_template_only_form_view'),
                               ('product.product', 'product.product_normal_form_view')]:
            arch = self.env[model].get_view(self.env.ref(view_id).id, 'form')['arch']
            self.assertIn('action_publish_to_magento', arch)

    def test_publish_bulk_reuses_mapping_and_queues_without_http(self):
        fresh = self.env['product.product'].create({'name': 'New publication', 'default_code': 'NEW-001'})
        # Existing mapped SKU remains stable, even after an Odoo reference change.
        self.product.default_code = 'CHANGED-REFERENCE'
        wizard = self._publish_wizard(self.product | fresh, tax_class_id=4, sync_image=True)
        self.assertEqual(wizard.new_count, 1)
        self.assertEqual(wizard.existing_count, 1)
        with patch.object(type(self.instance), '_client') as api, trap_jobs() as jobs:
            result = wizard.action_publish()
            jobs.assert_jobs_count(2)
            api.assert_not_called()
        bindings = self.env['in.magento.product.binding'].search([('instance_id', '=', self.instance.id)])
        self.assertEqual(len(bindings), 2)
        new = bindings.filtered(lambda binding: binding.product_id == fresh)
        self.assertEqual(new.sku, 'NEW-001')
        self.assertEqual(new.tax_class_id, 4)
        self.assertTrue(new.sync_image)
        self.assertEqual(self.product_binding.sku, 'SIM-001')
        self.assertEqual(self.product_binding.tax_class_id, 0)
        self.assertFalse(self.product_binding.sync_image)
        self.assertEqual(result['params']['next']['domain'], [('id', 'in', bindings.ids)])
        with trap_jobs() as jobs:
            wizard.action_publish()
            jobs.assert_jobs_count(2)
        self.assertEqual(self.env['in.magento.product.binding'].search_count([('instance_id', '=', self.instance.id)]), 2)

    def test_publish_missing_sku_rejects_whole_batch(self):
        fresh = self.env['product.product'].create({'name': 'Good', 'default_code': 'GOOD-001'})
        invalid = self.env['product.product'].create({'name': 'Missing reference'})
        wizard = self._publish_wizard(fresh | invalid)
        with trap_jobs() as jobs, self.assertRaisesRegex(UserError, 'Internal Reference'):
            wizard.action_publish()
        jobs.assert_jobs_count(0)
        self.assertFalse(self.env['in.magento.product.binding'].search([('product_id', 'in', (fresh | invalid).ids)]))

    def test_publish_duplicate_skus_and_existing_collision(self):
        fresh = self.env['product.product'].create({'name': 'Collision', 'default_code': 'SIM-001'})
        with self.assertRaisesRegex(UserError, 'duplicate SKU'):
            self._publish_wizard(self.product | fresh).action_publish()
        with self.assertRaisesRegex(UserError, 'already mapped'):
            self._publish_wizard(fresh).action_publish()

    def test_publish_rejects_wrong_company_and_disabled_connection(self):
        other = self.env['res.company'].create({'name': 'Publication other company'})
        foreign = self.env['product.product'].create({'name': 'Foreign', 'default_code': 'FOREIGN', 'company_id': other.id})
        with self.assertRaisesRegex(UserError, 'different company'):
            self._publish_wizard(foreign).action_publish()
        self.instance.enabled = False
        with self.assertRaisesRegex(UserError, 'disabled'):
            self._publish_wizard(self.product).action_publish()

    def test_publish_rejects_unsupported_and_inactive_products(self):
        attribute = self.env['product.attribute'].create({'name': 'Size', 'value_ids': [
            Command.create({'name': 'Small'}), Command.create({'name': 'Large'})]})
        template = self.env['product.template'].create({'name': 'Multiple variants',
            'attribute_line_ids': [Command.create({'attribute_id': attribute.id,
                'value_ids': [Command.set(attribute.value_ids.ids)]})]})
        self.assertEqual(template.product_variant_count, 2)
        with self.assertRaisesRegex(UserError, 'parent SKU'):
            self._publish_wizard(template.product_variant_ids[:1]).action_publish()
        self.assertEqual(template.action_publish_to_magento()['res_model'], 'in.magento.publish.products')
        self.product.sale_ok = False
        with self.assertRaisesRegex(UserError, 'available for sale'):
            self._publish_wizard(self.product).action_publish()

    def test_publish_explicit_category_assignment_and_new_product_export(self):
        category = self.env['in.magento.category.binding'].create({
            'instance_id': self.instance.id, 'external_id': '3',
            'category_id': self.env['product.category'].create({'name': 'Published category'}).id})
        fresh = self.env['product.product'].create({'name': 'Fresh', 'default_code': 'FRESH'})
        wizard = self._publish_wizard(fresh, category_ids=[Command.set(category.ids)])
        client = Mock()
        client.request.side_effect = [MagentoError('missing', 404), {'id': 200}]
        with self._mock_api(client), trap_jobs() as jobs:
            wizard.action_publish()
            jobs.perform_enqueued_jobs()
        binding = self.env['in.magento.product.binding'].search([('product_id', '=', fresh.id)])
        self.assertEqual(binding.category_ids, category)
        self.assertEqual(binding.external_id, '200')
        self.assertEqual(client.request.call_args.kwargs['payload']['product']['status'], 2)
        log = self.instance.sync_log_ids
        self.assertEqual(log.operation, 'create')
        self.assertEqual(log.trigger, 'manual')
        category.external_id = False
        with self.assertRaisesRegex(UserError, 'published categories'):
            wizard.action_publish()

    def test_publish_viewer_cannot_bypass_button_security(self):
        viewer = new_test_user(self.env, login='publish_viewer', groups='in_odoo_magento_connector.group_magento_user')
        with self.assertRaises(AccessError):
            self.product.with_user(viewer).action_publish_to_magento()
        with self.assertRaises(AccessError):
            self.env['in.magento.publish.products'].with_user(viewer).create({
                'instance_id': self.instance.id, 'product_ids': [Command.set(self.product.ids)]})

    def _category_wizard(self, categories, **values):
        return self.env['in.magento.publish.categories'].create({
            'instance_id': self.instance.id, 'category_ids': [Command.set(categories.ids)], **values})

    def test_publish_categories_entry_points(self):
        category = self.env['product.category'].create({'name': 'Category entry'})
        action = category.action_publish_to_magento()
        wizard = self.env[action['res_model']].with_context(action['context']).create({})
        self.assertEqual(wizard.category_ids, category)
        self.assertEqual(wizard.instance_id, self.instance)
        result = self.env.ref('in_odoo_magento_connector.action_publish_categories_magento').with_context(
            active_model='product.category', active_ids=category.ids).run()
        self.assertEqual(result['res_model'], 'in.magento.publish.categories')
        arch = self.env['product.category'].get_view(self.env.ref('product.product_category_form_view').id, 'form')['arch']
        self.assertIn('action_publish_to_magento', arch)

    def test_publish_categories_selected_only_and_no_products(self):
        parent = self.env['product.category'].create({'name': 'Internal parent'})
        child = self.env['product.category'].create({'name': 'Public child', 'parent_id': parent.id})
        wizard = self._category_wizard(child)
        self.assertEqual(wizard.publish_count, 1)
        self.assertEqual(wizard.extra_count, 0)
        product_count = self.env['in.magento.product.binding'].search_count([])
        with patch.object(type(self.instance), '_client') as api, trap_jobs() as jobs:
            wizard.action_publish()
            jobs.assert_jobs_count(1)
            api.assert_not_called()
        bindings = self.env['in.magento.category.binding'].search([('instance_id', '=', self.instance.id)])
        self.assertEqual(bindings.category_id, child)
        self.assertFalse(bindings.parent_id)
        self.assertEqual(self.env['in.magento.product.binding'].search_count([]), product_count)

    def test_publish_categories_parent_first_round_trip(self):
        parent = self.env['product.category'].create({'name': 'Parent <safe>'})
        child = self.env['product.category'].create({'name': 'Child', 'parent_id': parent.id})
        wizard = self._category_wizard(child, include_parents=True)
        self.assertEqual(wizard.extra_count, 1)
        self.assertEqual(wizard.publish_count, 2)
        self.assertIn('&lt;safe&gt;', wizard.preview_html)
        self.assertNotIn('<safe>', wizard.preview_html)
        client = Mock()
        client.request.side_effect = [{'id': 301}, {'id': 302}]
        with self._mock_api(client), trap_jobs() as jobs:
            wizard.action_publish()
            jobs.assert_jobs_count(2)
            jobs.perform_enqueued_jobs()
        bindings = self.env['in.magento.category.binding'].search([('instance_id', '=', self.instance.id)])
        parent_binding = bindings.filtered(lambda binding: binding.category_id == parent)
        child_binding = bindings.filtered(lambda binding: binding.category_id == child)
        self.assertEqual(child_binding.parent_id, parent_binding)
        self.assertEqual(parent_binding.external_id, '301')
        self.assertEqual(child_binding.external_id, '302')
        self.assertEqual(client.request.call_args_list[1].kwargs['payload']['category']['parent_id'], 301)
        self.assertEqual(len(self.instance.sync_log_ids), 2)
        self.assertTrue(all(log.trigger == 'manual' for log in self.instance.sync_log_ids))

    def test_publish_categories_bulk_preserves_selected_hierarchy(self):
        parent = self.env['product.category'].create({'name': 'Bulk parent'})
        child = self.env['product.category'].create({'name': 'Bulk child', 'parent_id': parent.id})
        wizard = self._category_wizard(child | parent)
        self.assertFalse(wizard.include_parents)
        self.assertEqual(wizard.extra_count, 0)
        with trap_jobs() as jobs:
            wizard.action_publish()
            jobs.assert_jobs_count(2)
        binding = self.env['in.magento.category.binding'].search([('category_id', '=', child.id)])
        self.assertEqual(binding.parent_id.category_id, parent)

    def test_publish_categories_reuses_existing_parent_and_mapping(self):
        internal_parent = self.env['product.category'].create({'name': 'Internal'})
        category = self.env['product.category'].create({'name': 'Public', 'parent_id': internal_parent.id})
        storefront = self.env['product.category'].create({'name': 'Storefront'})
        parent_binding = self.env['in.magento.category.binding'].create({
            'instance_id': self.instance.id, 'category_id': storefront.id, 'external_id': '300'})
        wizard = self._category_wizard(category, parent_binding_id=parent_binding.id)
        with trap_jobs():
            wizard.action_publish()
        binding = self.env['in.magento.category.binding'].search([('category_id', '=', category.id)])
        self.assertEqual(binding.parent_id, parent_binding)
        binding.external_id = '301'
        wizard.write({'parent_binding_id': False, 'include_parents': True})
        self.assertEqual(wizard.publish_count, 1)
        with trap_jobs() as jobs:
            wizard.action_publish()
            jobs.assert_jobs_count(1)
        self.assertEqual(binding.parent_id, parent_binding)
        self.assertEqual(self.env['in.magento.category.binding'].search_count([('category_id', '=', category.id)]), 1)
        self.assertFalse(self.env['in.magento.category.binding'].search([('category_id', '=', internal_parent.id)]))

    def test_publish_categories_existing_unpublished_parents_are_reviewed(self):
        parent = self.env['product.category'].create({'name': 'Pending parent'})
        child = self.env['product.category'].create({'name': 'Pending child'})
        parent_binding = self.env['in.magento.category.binding'].create({'instance_id': self.instance.id, 'category_id': parent.id})
        self.env['in.magento.category.binding'].create({'instance_id': self.instance.id, 'category_id': child.id, 'parent_id': parent_binding.id})
        wizard = self._category_wizard(child)
        self.assertEqual(wizard.extra_count, 1)
        self.assertEqual([item['category'] for item in wizard._publication_plan()], [parent, child])
        with trap_jobs() as jobs:
            wizard.action_publish()
            jobs.assert_jobs_count(2)

    def test_publish_categories_invalid_parent_and_access(self):
        category = self.env['product.category'].create({'name': 'Category'})
        other = self.instance.copy({'name': 'Other connection'})
        parent = self.env['in.magento.category.binding'].create({'instance_id': other.id, 'category_id': category.id, 'external_id': '3'})
        wizard = self._category_wizard(category, parent_binding_id=parent.id)
        with trap_jobs() as jobs, self.assertRaisesRegex(UserError, 'published parent'):
            wizard.action_publish()
        jobs.assert_jobs_count(0)
        wizard.parent_binding_id = False
        self.instance.enabled = False
        with self.assertRaisesRegex(UserError, 'disabled'):
            wizard.action_publish()
        viewer = new_test_user(self.env, login='category_publish_viewer', groups='in_odoo_magento_connector.group_magento_user')
        with self.assertRaises(AccessError):
            category.with_user(viewer).action_publish_to_magento()
        with self.assertRaises(AccessError):
            self.env['in.magento.publish.categories'].with_user(viewer).create({
                'instance_id': self.instance.id, 'category_ids': [Command.set(category.ids)]})


    def _variant_fixture(self):
        attribute = self.env['product.attribute'].create({'name': 'Size', 'value_ids': [
            Command.create({'name': 'Small'}), Command.create({'name': 'Large'})]})
        template = self.env['product.template'].create({'name': 'Variant Test', 'is_storable': True, 'list_price': 50,
            'attribute_line_ids': [Command.create({'attribute_id': attribute.id, 'value_ids': [Command.set(attribute.value_ids.ids)]})]})
        for index, variant in enumerate(template.product_variant_ids):
            variant.default_code = f'VAR-{index}'
        mapping = self.env['in.magento.attribute.mapping'].create({'instance_id': self.instance.id,
            'attribute_id': attribute.id, 'attribute_code': 'test_size', 'external_id': 141,
            'value_ids': [Command.create({'value_id': value.id, 'external_id': index + 11}) for index, value in enumerate(attribute.value_ids)]})
        action = template.action_publish_to_magento()
        wizard = self.env[action['res_model']].with_context(action['context']).create({})
        return template, mapping, wizard

    def test_configurable_publish_form_preserves_readonly_parent_template(self):
        template, mapping, unused_wizard = self._variant_fixture()
        action = template.action_publish_to_magento()
        # Selecting a connection runs the onchange and creates editable parent
        # rows. A direct ORM create does not exercise the client's save payload.
        context = dict(action['context'], default_instance_id=False)
        form = Form(self.env[action['res_model']].with_context(context))
        form.instance_id = self.instance
        with form.parent_line_ids.edit(0) as parent:
            parent.parent_sku = 'FORM-PARENT'
        wizard = form.save()
        self.assertEqual(wizard.parent_line_ids.template_id, template)
        self.assertEqual(wizard.parent_line_ids.parent_sku, 'FORM-PARENT')
        self.assertEqual(wizard.product_ids, template.product_variant_ids)
        self.assertEqual(wizard.setup_attribute_id, mapping.attribute_id)
        self.assertEqual(wizard.setup_attribute_code, mapping.attribute_code)
        with trap_jobs() as jobs:
            wizard.action_publish()
            jobs.assert_jobs_count(1)
        self.assertEqual(self.instance.template_ids.template_id, template)
        self.assertEqual(self.instance.template_ids.child_ids.product_id, template.product_variant_ids)

    def test_configurable_bulk_form_preserves_each_parent_template(self):
        template, mapping, unused_wizard = self._variant_fixture()
        other = self.env['product.template'].create({
            'name': 'Second variant family', 'is_storable': True,
            'attribute_line_ids': [Command.create({
                'attribute_id': mapping.attribute_id.id,
                'value_ids': [Command.set(mapping.attribute_id.value_ids.ids)],
            })],
        })
        for index, variant in enumerate(other.product_variant_ids):
            variant.default_code = f'FORM-OTHER-{index}'
        templates = template | other
        action = templates.action_publish_to_magento()
        form = Form(self.env[action['res_model']].with_context(
            dict(action['context'], default_instance_id=False)))
        form.instance_id = self.instance
        wizard = form.save()
        self.assertEqual(wizard.parent_line_ids.template_id, templates)
        self.assertEqual(len(wizard.parent_line_ids), 2)
        with trap_jobs() as jobs:
            wizard.action_publish()
            jobs.assert_jobs_count(2)
        for parent in self.instance.template_ids:
            self.assertEqual(parent.child_ids.product_id, parent.template_id.product_variant_ids)

    def test_publication_attribute_readiness_names_missing_values(self):
        template, mapping, wizard = self._variant_fixture()
        missing = mapping.value_ids[0]
        value_name = missing.value_id.name
        missing.unlink()
        self.assertTrue(wizard.attribute_setup_needed)
        self.assertIn(value_name, wizard.attribute_status)
        with trap_jobs() as jobs:
            with self.assertRaises(UserError) as error:
                wizard.action_publish()
            jobs.assert_jobs_count(0)
        self.assertIn(mapping.attribute_id.name, str(error.exception))
        self.assertIn(value_name, str(error.exception))
        self.assertIn(self.instance.name, str(error.exception))
        self.assertIn('Attributes tab', str(error.exception))

    def test_publication_attribute_setup_uses_selected_connection_only(self):
        template, mapping, wizard = self._variant_fixture()
        other = self.instance.copy({'name': 'Other destination'})
        wizard.write({'instance_id': other.id, 'setup_attribute_id': mapping.attribute_id.id,
            'setup_attribute_code': 'new_size'})
        self.assertIn('not mapped', wizard.attribute_status)
        self.assertTrue(wizard.attribute_setup_needed)
        with trap_jobs() as jobs:
            result = wizard.action_publish_attribute()
            jobs.assert_jobs_count(1)
        new_mapping = other.attribute_mapping_ids
        self.assertEqual(new_mapping.attribute_id, mapping.attribute_id)
        self.assertEqual(new_mapping.attribute_code, 'new_size')
        self.assertFalse(new_mapping.external_id)
        self.assertFalse(other.template_ids)
        self.assertFalse(other.product_ids)
        self.assertEqual(result['params']['next']['res_id'], wizard.id)
        self.assertEqual(result['params']['next']['views'], [(False, 'form')])
        self.assertEqual(wizard.product_ids, template.product_variant_ids)
        self.assertEqual(wizard.parent_line_ids.template_id, template)
        self.assertEqual(mapping.attribute_code, 'test_size')
        self.assertIn('Magento attribute ID is missing', wizard.attribute_status)

    def test_publication_attribute_mapping_can_be_edited_without_export(self):
        template, mapping, wizard = self._variant_fixture()
        wizard.setup_attribute_id = mapping.attribute_id
        client = Mock()
        with self._mock_api(client), trap_jobs() as jobs:
            result = wizard.action_open_attribute_mapping()
            jobs.assert_jobs_count(0)
        client.request.assert_not_called()
        self.assertEqual(result['res_id'], mapping.id)
        self.assertEqual(result['target'], 'new')
        self.assertEqual(result['views'], [(False, 'form')])
        self.assertFalse(wizard.attribute_setup_needed)
        self.assertIn('mappings complete', wizard.attribute_status)
        refreshed = wizard.action_refresh_attributes()
        self.assertEqual(refreshed['res_id'], wizard.id)
        self.assertEqual(refreshed['views'], [(False, 'form')])

    def test_publication_attribute_setup_rejects_unrelated_attribute(self):
        template, mapping, wizard = self._variant_fixture()
        unrelated = self.env['product.attribute'].create({'name': 'Unrelated'})
        wizard.setup_attribute_id = unrelated
        with trap_jobs() as jobs:
            with self.assertRaisesRegex(UserError, 'selected product variants'):
                wizard.action_publish_attribute()
            with self.assertRaisesRegex(UserError, 'selected product variants'):
                wizard.action_open_attribute_mapping()
            jobs.assert_jobs_count(0)

    def test_publication_existing_attribute_setup_creates_local_mapping_only(self):
        template, mapping, wizard = self._variant_fixture()
        other = self.instance.copy({'name': 'Existing remote attribute destination'})
        wizard.write({'instance_id': other.id, 'setup_attribute_id': mapping.attribute_id.id,
            'setup_attribute_code': 'existing_size'})
        client = Mock()
        with self._mock_api(client), trap_jobs() as jobs:
            result = wizard.action_open_attribute_mapping()
            jobs.assert_jobs_count(0)
        client.request.assert_not_called()
        local_mapping = other.attribute_mapping_ids
        self.assertEqual(result['res_id'], local_mapping.id)
        self.assertEqual(local_mapping.attribute_code, 'existing_size')
        self.assertFalse(local_mapping.external_id)
        self.assertFalse(local_mapping.value_ids)
        self.assertTrue(wizard.attribute_setup_needed)
        self.assertFalse(other.template_ids)
        viewer = new_test_user(self.env, login='product_attribute_setup_viewer',
            groups='in_odoo_magento_connector.group_magento_user')
        with self.assertRaises(AccessError):
            wizard.with_user(viewer).action_publish_attribute()
        with self.assertRaises(AccessError):
            wizard.with_user(viewer).action_open_attribute_mapping()

    def test_configurable_publication_links_variants_and_is_repeatable(self):
        template, mapping, wizard = self._variant_fixture()
        remote = {}
        client = Mock()
        def response(method, path, **kwargs):
            if path == 'products/attributes/test_size':
                return {'attribute_id': 141, 'frontend_input': 'select', 'scope': 'global', 'is_user_defined': True,
                        'options': [{'value': str(value.external_id)} for value in mapping.value_ids]}
            if path.endswith('/attributes'):
                return [{'attribute_code': 'test_size'}]
            sku = path.removeprefix('products/')
            if method == 'GET':
                if sku not in remote:
                    raise MagentoError('not found', 404)
                return deepcopy(remote[sku])
            data = deepcopy(kwargs['payload']['product'])
            sku = data['sku']
            if method == 'POST':
                remote[sku] = {'id': len(remote) + 200, **data}
            else:
                remote[sku].update(data)
            return deepcopy(remote[sku])
        client.request.side_effect = response
        with self._mock_api(client), trap_jobs() as jobs:
            wizard.action_publish()
            jobs.assert_jobs_count(1)
            jobs.perform_enqueued_jobs()
            jobs.perform_enqueued_jobs()
        parent = self.instance.template_ids
        self.assertEqual(len(parent.child_ids), 2)
        self.assertEqual(remote[parent.sku]['type_id'], 'configurable')
        self.assertEqual(set(remote[parent.sku]['extension_attributes']['configurable_product_links']), {201, 202})
        for child in parent.child_ids:
            self.assertEqual(remote[child.sku]['visibility'], 1)
            self.assertEqual(remote[child.sku]['type_id'], 'simple')
            self.assertEqual(remote[child.sku]['price'], 50)
        self.assertTrue(parent.linked_at)
        with self._mock_api(client), trap_jobs() as jobs:
            wizard.action_publish()
            jobs.perform_enqueued_jobs()
            jobs.perform_enqueued_jobs()
        self.assertEqual(len(remote), 3)
        self.assertEqual(len(self.instance.template_ids), 1)

    def test_configurable_order_uses_parent_price_child_identity(self):
        template, mapping, wizard = self._variant_fixture()
        with trap_jobs():
            wizard.action_publish()
        parent = self.instance.template_ids
        parent.external_id = '200'
        child = parent.child_ids[0]
        child.external_id = '201'
        payload = deepcopy(self.fixture)
        commercial = payload['items'][0]
        commercial.update(item_id=1, product_type='configurable', product_id=200, sku=parent.sku)
        variant = deepcopy(commercial)
        variant.update(item_id=2, parent_item_id=1, product_type='simple', product_id=201, sku=child.sku,
                       price=0, row_total=0, tax_amount=0, discount_amount=0)
        payload['items'].append(variant)
        inbox = self._inbox(payload)
        inbox._job_import()
        order = inbox.sale_order_id
        product_lines = order.order_line.filtered(lambda line: line.product_id == child.product_id)
        self.assertEqual(len(product_lines), 1)
        self.assertEqual(product_lines.product_uom_qty, 2)
        self.assertEqual(product_lines.price_unit, 50)
        self.assertEqual(order.amount_total, 114)
        self.assertEqual(len(order.order_line), 2)
        bad = deepcopy(payload)
        bad['entity_id'] = 5000
        bad['items'][1]['product_id'] = 999
        with self.assertRaisesRegex(UserError, 'exact child'):
            self._inbox(bad)._job_import()

    def test_catalog_scheduler_filters_variants_and_parent_dependencies(self):
        template, mapping, wizard = self._variant_fixture()
        with trap_jobs():
            wizard.action_publish()
        parent = self.instance.template_ids
        parent.external_id = '200'
        for index, child in enumerate(parent.child_ids):
            child.external_id = str(201 + index)
            child.last_fingerprint = child._fingerprint()
        parent.last_fingerprint = parent._fingerprint()
        self.instance.auto_export_products = True
        with trap_jobs() as jobs:
            self.instance._cron_schedule()
            jobs.assert_jobs_count(0)
        # Attribute option mappings change outgoing data without writing products.
        mapping.value_ids[0].external_id = 99
        with trap_jobs() as jobs:
            self.instance._cron_schedule()
            jobs.assert_jobs_count(2)
            jobs.assert_jobs_count(1, only=parent._job_export_if_changed)

    def test_inventory_physical_owned_stock_and_source_isolation(self):
        self.product.is_storable = True
        self.product_binding.write({'external_id': '101', 'sync_inventory': True})
        self.env['in.magento.inventory.source'].create({'instance_id': self.instance.id,
            'warehouse_id': self.warehouse.id, 'source_code': 'default'})
        location = self.warehouse.lot_stock_id
        self.env['stock.quant']._update_available_quantity(self.product, location, 10)
        self.env['stock.quant']._update_reserved_quantity(self.product, location, 3)
        owner = self.env['res.partner'].create({'name': 'Consignment owner'})
        self.env['stock.quant']._update_available_quantity(self.product, location, 20, owner_id=owner)
        outside = self.env['stock.location'].create({'name': 'Outside mapped stock', 'usage': 'internal', 'company_id': self.company.id})
        self.env['stock.quant']._update_available_quantity(self.product, outside, 30)
        self.assertEqual(self.product_binding._inventory_items(), [{'sku': 'SIM-001', 'source_code': 'default', 'quantity': 10, 'status': 1}])
        client = Mock()
        client.request.side_effect = [{'id': 101, 'type_id': 'simple'}, {'source_code': 'default', 'enabled': True},
            {'items': [{'sku': 'SIM-001', 'source_code': 'default', 'quantity': 1, 'status': 1}]}, []]
        with self._mock_api(client):
            self.product_binding._job_export_inventory()
        write = client.request.call_args
        self.assertEqual(write.args, ('POST', 'inventory/source-items'))
        self.assertTrue(write.kwargs['replay_safe'])
        self.assertEqual(write.kwargs['payload']['sourceItems'][0]['quantity'], 10)
        self.assertEqual(self.instance.sync_log_ids.operation, 'inventory')
        client.reset_mock()
        client.request.side_effect = [{'id': 101, 'type_id': 'simple'}, {'source_code': 'default', 'enabled': True},
            {'items': [{'sku': 'SIM-001', 'source_code': 'default', 'quantity': 10, 'status': 1}]}]
        with self._mock_api(client):
            self.product_binding._job_export_inventory()
        self.assertEqual(client.request.call_count, 3)
        self.assertEqual(len(self.instance.sync_log_ids), 1)
        self.env['stock.quant']._update_available_quantity(self.product, location, -12)
        self.assertEqual(self.product_binding._inventory_items()[0]['quantity'], 0)
        self.assertEqual(self.product_binding._inventory_items()[0]['status'], 0)

    def _bulk_stock_fixture(self, count=2):
        self.product.is_storable = True
        self.product_binding.write({'external_id': '101', 'sync_inventory': True})
        if not self.instance.inventory_source_ids:
            self.env['in.magento.inventory.source'].create({'instance_id': self.instance.id,
                'warehouse_id': self.warehouse.id, 'source_code': 'default'})
        records = self.product_binding
        for index in range(1, count):
            product = self.env['product.product'].create({'name': f'Bulk {index}', 'is_storable': True})
            records |= self.env['in.magento.product.binding'].create({'instance_id': self.instance.id,
                'product_id': product.id, 'sku': f'BULK-{index}', 'external_id': str(101 + index), 'sync_inventory': True})
        for record in records:
            self.env['stock.quant']._update_available_quantity(record.product_id, self.warehouse.lot_stock_id, 10)
        remote = {row.sku: {'id': int(row.external_id), 'sku': row.sku, 'type_id': 'simple'} for row in records}
        stock = {(row.sku, 'default'): {'sku': row.sku, 'source_code': 'default', 'quantity': 1, 'status': 1} for row in records}
        def response(method, path, **kwargs):
            if method == 'POST':
                for item in kwargs['payload']['sourceItems']:
                    stock[(item['sku'], item['source_code'])] = deepcopy(item)
                return []
            if path == 'products':
                ids = kwargs['params']['searchCriteria[filter_groups][0][filters][0][value]'].split(',')
                rows = [row for row in remote.values() if str(row['id']) in ids]
                return {'items': deepcopy(rows), 'total_count': len(rows)}
            if path.startswith('products/'):
                return deepcopy(remote[path.removeprefix('products/')])
            if path.startswith('inventory/sources/'):
                return {'source_code': path.removeprefix('inventory/sources/'), 'enabled': True}
            if path == 'inventory/source-items':
                params = kwargs['params']
                skus = params['searchCriteria[filter_groups][0][filters][0][value]'].split(',')
                source = params['searchCriteria[filter_groups][1][filters][0][value]']
                rows = [row for (sku, code), row in stock.items() if sku in skus and code == source]
                return {'items': deepcopy(rows), 'total_count': len(rows)}
            raise AssertionError(path)
        client = Mock()
        client.request.side_effect = response
        return records, client, remote, stock

    def test_inventory_bulk_changed_only_and_per_product_logs(self):
        records, client, remote, stock = self._bulk_stock_fixture()
        stock[(records[0].sku, 'default')]['quantity'] = 10
        self.env['stock.quant']._update_reserved_quantity(records[1].product_id, self.warehouse.lot_stock_id, 3)
        owner = self.env['res.partner'].create({'name': 'Bulk consignment'})
        self.env['stock.quant']._update_available_quantity(records[1].product_id, self.warehouse.lot_stock_id, 20, owner_id=owner)
        with self._mock_api(client):
            records._job_export_inventory_batch()
        writes = [call for call in client.request.call_args_list if call.args[0] == 'POST']
        self.assertEqual(len(writes), 1)
        self.assertTrue(writes[0].kwargs['replay_safe'])
        self.assertEqual(writes[0].kwargs['payload']['sourceItems'], [
            {'sku': records[1].sku, 'source_code': 'default', 'quantity': 10, 'status': 1}])
        self.assertEqual(self.instance.sync_log_ids.product_binding_id, records[1])
        self.assertTrue(all(records.mapped('last_inventory_sync')))
        client.reset_mock()
        with self._mock_api(client):
            records._job_export_inventory_batch()
        self.assertEqual(client.request.call_count, 3)
        self.assertEqual(len(self.instance.sync_log_ids), 1)

    def test_inventory_bulk_scheduling_bounded_and_no_inline_http(self):
        records, client, remote, stock = self._bulk_stock_fixture(101)
        with self._mock_api(client), trap_jobs() as jobs:
            records.action_export_inventory()
            jobs.assert_jobs_count(2)
            self.assertEqual([len(job.recordset) for job in jobs.enqueued_jobs], [100, 1])
            self.assertEqual(jobs.enqueued_jobs[0].method_name, '_job_export_inventory_batch')
            client.request.assert_not_called()

    def test_inventory_bulk_multi_source_scope(self):
        records, client, remote, stock = self._bulk_stock_fixture()
        warehouse = self.env['stock.warehouse'].create({'name': 'Bulk second', 'code': 'BULK2', 'company_id': self.company.id})
        self.env['in.magento.inventory.source'].create({'instance_id': self.instance.id,
            'warehouse_id': warehouse.id, 'source_code': 'second'})
        for row in records:
            self.env['stock.quant']._update_available_quantity(row.product_id, warehouse.lot_stock_id, 4)
            stock[(row.sku, 'unmapped')] = {'sku': row.sku, 'source_code': 'unmapped', 'quantity': 99, 'status': 1}
        with self._mock_api(client):
            records._job_export_inventory_batch()
        writes = [call for call in client.request.call_args_list if call.args[0] == 'POST']
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(writes[0].kwargs['payload']['sourceItems']), 4)
        for row in records:
            self.assertEqual(stock[(row.sku, 'default')]['quantity'], 10)
            self.assertEqual(stock[(row.sku, 'second')]['quantity'], 4)
            self.assertEqual(stock[(row.sku, 'unmapped')]['quantity'], 99)

    def test_inventory_bulk_bad_mapping_isolated(self):
        records, client, remote, stock = self._bulk_stock_fixture()
        remote[records[0].sku]['id'] = 999
        with self._mock_api(client), trap_jobs() as jobs:
            records._job_export_inventory_batch()
            jobs.assert_jobs_count(1, only=records[0]._job_export_inventory)
        self.assertEqual(stock[(records[0].sku, 'default')]['quantity'], 1)
        self.assertEqual(stock[(records[1].sku, 'default')]['quantity'], 10)
        self.assertFalse(records[0].last_inventory_sync)

    def test_inventory_bulk_unacknowledged_delivery_isolated(self):
        inbox, picking, shipment = self._fulfillment_fixture()
        records, client, remote, stock = self._bulk_stock_fixture()
        with self._mock_api(client), trap_jobs() as jobs:
            records._job_export_inventory_batch()
            jobs.assert_jobs_count(1, only=self.product_binding._job_export_inventory)
        self.assertEqual(stock[(self.product_binding.sku, 'default')]['quantity'], 1)
        self.assertEqual(stock[(records[1].sku, 'default')]['quantity'], 10)

    def test_inventory_bulk_rejection_falls_back_without_success_logs(self):
        records, client, remote, stock = self._bulk_stock_fixture()
        response = client.request.side_effect
        rejected = False
        def reject_once(method, path, **kwargs):
            nonlocal rejected
            if method == 'POST' and not rejected:
                rejected = True
                raise MagentoError('Invalid batch', 400)
            return response(method, path, **kwargs)
        client.request.side_effect = reject_once
        with self._mock_api(client), trap_jobs() as jobs:
            records._job_export_inventory_batch()
            jobs.assert_jobs_count(2)
            self.assertEqual(set(self.instance.sync_log_ids.mapped('status')), {'retry'})
            self.assertFalse(any(records.mapped('last_inventory_sync')))
            jobs.perform_enqueued_jobs()
        self.assertTrue(all(records.mapped('last_inventory_sync')))
        self.assertEqual(len(self.instance.sync_log_ids.filtered(lambda log: log.status == 'success')), 2)

    def test_inventory_bulk_uncertain_partial_write_reconciles_on_retry(self):
        from odoo.addons.queue_job.exception import RetryableJobError
        records, client, remote, stock = self._bulk_stock_fixture()
        response = client.request.side_effect
        def timeout(method, path, **kwargs):
            if method == 'POST':
                item = kwargs['payload']['sourceItems'][0]
                stock[(item['sku'], item['source_code'])] = deepcopy(item)
                raise RetryableJobError('Unknown write outcome')
            return response(method, path, **kwargs)
        client.request.side_effect = timeout
        with self._mock_api(client), self.assertRaises(RetryableJobError), self.env.cr.savepoint():
            records._job_export_inventory_batch()
        self.assertFalse(any(records.mapped('last_inventory_sync')))
        self.assertFalse(self.instance.sync_log_ids)
        client.reset_mock()
        client.request.side_effect = response
        with self._mock_api(client):
            records._job_export_inventory_batch()
        writes = [call for call in client.request.call_args_list if call.args[0] == 'POST']
        self.assertEqual(len(writes), 1)
        self.assertEqual(len(writes[0].kwargs['payload']['sourceItems']), 1)
        self.assertTrue(all(row['quantity'] == 10 for row in stock.values()))

    def test_inventory_bulk_invalid_response_and_disabled_source_block_writes(self):
        records, client, remote, stock = self._bulk_stock_fixture()
        response = client.request.side_effect
        for failure in ('duplicate', 'scope', 'truncated', 'disabled'):
            def invalid(method, path, **kwargs):
                result = response(method, path, **kwargs)
                if path.startswith('inventory/sources/') and failure == 'disabled':
                    result['enabled'] = False
                if path == 'inventory/source-items':
                    if failure == 'duplicate':
                        result['items'][1] = result['items'][0]
                    elif failure == 'scope':
                        result['items'][0]['source_code'] = 'other'
                    elif failure == 'truncated':
                        result['items'] = []
                return result
            client.reset_mock()
            client.request.side_effect = invalid
            with self.subTest(failure=failure), self._mock_api(client), self.assertRaises(UserError):
                records._job_export_inventory_batch()
            self.assertFalse(any(call.args[0] == 'POST' for call in client.request.call_args_list))

    def test_inventory_bulk_respects_opt_out_after_queueing(self):
        records, client, remote, stock = self._bulk_stock_fixture()
        records[0].sync_inventory = False
        with self._mock_api(client):
            records._job_export_inventory_batch()
        self.assertEqual(stock[(records[0].sku, 'default')]['quantity'], 1)
        self.assertEqual(stock[(records[1].sku, 'default')]['quantity'], 10)
        self.instance.enabled = False
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'disabled'):
            records._job_export_inventory_batch()

    def test_inventory_bulk_failure_logged_for_each_product(self):
        records, client, remote, stock = self._bulk_stock_fixture()
        job = Job(records._job_export_inventory_batch)
        job.store()
        job.set_failed(exc_info='private', exc_message='private')
        job.store()
        self.assertEqual(self.instance.sync_log_ids.product_binding_id, records)
        self.assertEqual(set(self.instance.sync_log_ids.mapped('operation')), {'inventory'})
        self.assertEqual(set(self.instance.sync_log_ids.mapped('status')), {'failed'})

    def test_inventory_bulk_collection_reads_all_pages(self):
        client = Mock()
        client.request.side_effect = [{'items': [{'id': 1}], 'total_count': 2},
                                      {'items': [{'id': 2}], 'total_count': 2}]
        rows = self.product_binding._inventory_collection(client, 'products', [], 2, 'entity_id')
        self.assertEqual(rows, [{'id': 1}, {'id': 2}])
        self.assertEqual(client.request.call_args.kwargs['params']['searchCriteria[currentPage]'], 2)

    def _bulk_price_fixture(self, count=2):
        records, client, remote, stock = self._bulk_stock_fixture(count)
        original = client.request.side_effect
        for record in records:
            record.product_id.list_price = 50
            record.write({'last_sync': fields.Datetime.now(), 'last_fingerprint': record._fingerprint(),
                          'last_nonprice_fingerprint': record._price_snapshot()['catalog']})
            remote[record.sku]['price'] = 50
            record.product_id.list_price = 55
        def response(method, path, **kwargs):
            if path == 'products/base-prices':
                for item in kwargs['payload']['prices']:
                    remote[item['sku']]['price'] = item['price']
                return []
            if path == 'store/storeViews':
                return [{'code': 'default', 'id': 1}, {'code': 'other', 'id': 2}]
            return original(method, path, **kwargs)
        client.request.side_effect = response
        return records, client, remote

    def test_bulk_prices_verified_and_no_inventory_or_image_followup(self):
        records, client, remote = self._bulk_price_fixture()
        with self._mock_api(client), trap_jobs() as jobs:
            records._job_export_prices()
            jobs.assert_jobs_count(0)
        self.assertEqual(client.request.call_count, 3)
        write = client.request.call_args_list[1]
        self.assertEqual(write.args, ('POST', 'products/base-prices'))
        self.assertTrue(write.kwargs['replay_safe'])
        self.assertEqual(len(write.kwargs['payload']['prices']), 2)
        self.assertTrue(all(item['store_id'] == 0 for item in write.kwargs['payload']['prices']))
        self.assertTrue(all(row.last_fingerprint == row._fingerprint() for row in records))
        self.assertEqual(len(self.instance.sync_log_ids), 2)
        self.assertEqual(set(self.instance.sync_log_ids.change_ids.mapped('field_name')), {'price'})
        client.reset_mock()
        with self._mock_api(client):
            records._job_export_prices()
        client.request.assert_not_called()

    def test_bulk_prices_scheduler_batches_and_mixed_changes(self):
        records, client, remote = self._bulk_price_fixture(3)
        records[0].product_id.name = 'Changed name as well'
        self.instance.auto_export_products = True
        with self._mock_api(client), trap_jobs() as jobs:
            self.instance._cron_schedule()
            jobs.assert_jobs_count(2)
            jobs.assert_jobs_count(1, only=records[0]._job_export_if_changed)
            jobs.assert_jobs_count(1, only=records[1:]._job_export_prices)
            client.request.assert_not_called()

    def test_bulk_prices_upgrade_baseline_requires_full_match(self):
        records, client, remote = self._bulk_price_fixture()
        records.last_nonprice_fingerprint = False
        self.assertEqual(records[0]._catalog_change_kind(), 'full')
        records[0].product_id.list_price = 50
        self.assertFalse(records[0]._catalog_change_kind())
        self.assertTrue(records[0].last_nonprice_fingerprint)
        records[0].product_id.list_price = 55
        self.assertEqual(records[0]._catalog_change_kind(), 'price')

    def test_bulk_prices_recheck_changed_catalog_and_reverted_price(self):
        records, client, remote = self._bulk_price_fixture()
        records[0].product_id.name = 'Changed while queued'
        records[1].product_id.list_price = 50
        with self._mock_api(client), trap_jobs() as jobs:
            records._job_export_prices()
            jobs.assert_jobs_count(1, only=records[0]._job_export_if_changed)
            client.request.assert_not_called()

    def test_bulk_prices_partial_rejection_acknowledges_only_verified_rows(self):
        records, client, remote = self._bulk_price_fixture()
        response = client.request.side_effect
        def partial(method, path, **kwargs):
            if path == 'products/base-prices':
                remote[records[0].sku]['price'] = 55
                return [{'message': 'Rejected item', 'parameters': ['private']}]
            return response(method, path, **kwargs)
        client.request.side_effect = partial
        with self._mock_api(client), trap_jobs() as jobs:
            records._job_export_prices()
            jobs.assert_jobs_count(1, only=records[1]._job_export_if_changed)
        self.assertEqual(records[0].last_fingerprint, records[0]._fingerprint())
        self.assertNotEqual(records[1].last_fingerprint, records[1]._fingerprint())
        self.assertEqual(self.instance.sync_log_ids.filtered(lambda log: log.status == 'success').product_binding_id, records[0])

    def test_bulk_prices_missing_acl_falls_back(self):
        records, client, remote = self._bulk_price_fixture()
        response = client.request.side_effect
        def forbidden(method, path, **kwargs):
            if path == 'products/base-prices':
                raise MagentoError('Forbidden', 403)
            return response(method, path, **kwargs)
        client.request.side_effect = forbidden
        with self._mock_api(client), trap_jobs() as jobs:
            records._job_export_prices()
            jobs.assert_jobs_count(2)
        self.assertFalse(self.instance.sync_log_ids.filtered(lambda log: log.status == 'success'))
        self.assertTrue(all(row.last_fingerprint != row._fingerprint() for row in records))

    def test_bulk_prices_uncertain_write_recovers_without_duplicate_write(self):
        from odoo.addons.queue_job.exception import RetryableJobError
        records, client, remote = self._bulk_price_fixture()
        response = client.request.side_effect
        def timeout(method, path, **kwargs):
            result = response(method, path, **kwargs)
            if path == 'products/base-prices':
                raise RetryableJobError('Unknown outcome')
            return result
        client.request.side_effect = timeout
        with self._mock_api(client), self.assertRaises(RetryableJobError), self.env.cr.savepoint():
            records._job_export_prices()
        self.assertFalse(self.instance.sync_log_ids)
        client.reset_mock()
        client.request.side_effect = response
        with self._mock_api(client):
            records._job_export_prices()
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(client.request.call_args.args, ('GET', 'products'))
        self.assertTrue(all(row.last_fingerprint == row._fingerprint() for row in records))

    def test_bulk_prices_scope_identity_and_opt_out(self):
        records, client, remote = self._bulk_price_fixture()
        # Connection scope is immutable once published; patch only the resolver
        # input in a separate unpublished instance when testing a store code.
        other = self.instance.copy({'name': 'Store-specific', 'store_code': 'other'})
        self.assertEqual(other._price_store_id(client), 2)
        remote[records[0].sku]['type_id'] = 'configurable'
        with self._mock_api(client), trap_jobs() as jobs:
            records._job_export_prices()
            jobs.assert_jobs_count(1, only=records[0]._job_export_if_changed)
        self.assertEqual(remote[records[0].sku]['price'], 50)
        self.assertEqual(remote[records[1].sku]['price'], 55)
        self.instance.batch_price_updates = False
        self.assertEqual(records[0]._catalog_change_kind(), 'full')

    def test_bulk_prices_configurable_children(self):
        template, mapping, wizard = self._variant_fixture()
        with trap_jobs():
            wizard.action_publish()
        parent = self.instance.template_ids
        parent.external_id = '200'
        for index, child in enumerate(parent.child_ids):
            child.write({'external_id': str(201 + index), 'last_sync': fields.Datetime.now()})
            child.last_fingerprint = child._fingerprint()
            child.last_nonprice_fingerprint = child._price_snapshot()['catalog']
        template.list_price = 60
        self.assertTrue(all(child._catalog_change_kind() == 'price' for child in parent.child_ids))

    def test_bulk_prices_virtual_product(self):
        records, client, remote = self._bulk_price_fixture()
        row = records[0]
        row.product_id.write({'is_storable': False, 'type': 'service', 'list_price': 50})
        row.write({'last_fingerprint': row._fingerprint(), 'last_nonprice_fingerprint': row._price_snapshot()['catalog']})
        remote[row.sku]['type_id'] = 'virtual'
        row.product_id.list_price = 55
        with self._mock_api(client):
            records._job_export_prices()
        self.assertEqual(remote[row.sku]['price'], 55)
        self.assertEqual(row.last_fingerprint, row._fingerprint())

    def test_bulk_prices_malformed_acknowledgement_never_marks_synced(self):
        records, client, remote = self._bulk_price_fixture()
        response = client.request.side_effect
        def malformed(method, path, **kwargs):
            result = response(method, path, **kwargs)
            return {'unexpected': True} if path == 'products/base-prices' else result
        client.request.side_effect = malformed
        with self._mock_api(client), self.assertRaises(UserError), self.env.cr.savepoint():
            records._job_export_prices()
        self.assertTrue(all(row.last_fingerprint != row._fingerprint() for row in records))
        self.assertFalse(self.instance.sync_log_ids)

    def _financial_poll_fixture(self, rows):
        self.instance.import_payment_status = True
        now = fields.Datetime.now()
        records = self.env['in.magento.order.binding']
        for index, (state, age, checked_hours) in enumerate(rows):
            payload = deepcopy(self.fixture)
            payload.update(entity_id=9000 + index, increment_id=f'POLL-{index}')
            record = self._inbox(payload)
            record._job_import()
            record.sale_order_id.date_order = now - timedelta(days=age)
            record.write({'remote_state': state, 'financial_checked_at':
                          now - timedelta(hours=checked_hours) if checked_hours is not None else False})
            records |= record
        return now, records

    def test_financial_due_selection_includes_late_refunds_and_active_orders(self):
        now, records = self._financial_poll_fixture([
            ('processing', 60, 1), ('complete', 2, 1), ('complete', 60, 1),
            ('closed', 60, 25), ('canceled', 60, 25), ('processing', 1, .01), (False, 60, None)])
        with trap_jobs() as jobs:
            self.instance._schedule_due_financials(now)
            selected = {job.recordset.id for job in jobs.enqueued_jobs}
        self.assertEqual(selected, set((records[0] | records[1] | records[3] | records[4] | records[6]).ids))

    def test_financial_due_selection_bounded_fair_and_manual_override(self):
        now, records = self._financial_poll_fixture([('processing', 1, 1)] * 5 + [('complete', 60, 25)] * 3)
        self.instance.financial_poll_limit = 4
        with trap_jobs() as jobs:
            self.instance._schedule_due_financials(now)
            jobs.assert_jobs_count(4)
            selected = {job.recordset.id for job in jobs.enqueued_jobs}
        self.assertEqual(len(selected & set(records[:5].ids)), 3)
        self.assertEqual(len(selected & set(records[5:].ids)), 1)
        with trap_jobs() as jobs:
            self.instance.action_sync_financials()
            jobs.assert_jobs_count(8)

    def test_financial_check_timestamp_after_success_without_payment_import(self):
        inbox = self._accounting_setup()
        self.instance.import_payment_status = False
        client = Mock(); client.request.return_value = dict(self.fixture, state='complete')
        with self._mock_api(client), trap_jobs() as jobs:
            inbox._job_sync_financials()
            jobs.assert_jobs_count(2)
        self.assertTrue(inbox.financial_checked_at)
        self.assertFalse(inbox.payment_checked_at)
        self.assertEqual(inbox.remote_state, 'complete')
        before = inbox.financial_checked_at
        client.request.return_value['order_currency_code'] = 'USD'
        with self._mock_api(client), self.assertRaises(UserError):
            inbox._job_sync_financials()
        self.assertEqual(inbox.financial_checked_at, before)

    def test_financial_history_checks_still_queue_refund_discovery(self):
        inbox = self._accounting_setup()
        now = fields.Datetime.now()
        inbox.sale_order_id.date_order = now - timedelta(days=90)
        inbox.write({'remote_state': 'complete', 'financial_checked_at': now - timedelta(hours=25)})
        client = Mock(); client.request.return_value = dict(self.fixture, state='closed')
        with self._mock_api(client), trap_jobs() as jobs:
            self.instance._schedule_due_financials(now)
            jobs.assert_jobs_count(1, only=inbox._job_sync_financials)
            inbox._job_sync_financials()
            jobs.assert_enqueued_job(inbox._job_financial_page, args=('creditmemo', 0))

    def test_financial_schedule_settings_validated(self):
        for values in ({'financial_recent_days': 0}, {'financial_history_hours': 0}, {'financial_poll_limit': 3}):
            with self.assertRaises(ValidationError), self.env.cr.savepoint():
                self.instance.write(values)

    def test_inventory_bulk_mixed_connections_refused_before_http(self):
        records, client, remote, stock = self._bulk_stock_fixture()
        other_instance = self.instance.copy({'name': 'Another connection'})
        other = self.env['in.magento.product.binding'].create({'instance_id': other_instance.id,
            'product_id': self.product.id, 'sku': 'OTHER', 'external_id': '999', 'sync_inventory': True})
        with self._mock_api(client), self.assertRaises(ValueError):
            (records | other)._job_export_inventory_batch()
        client.request.assert_not_called()

    def test_inventory_bulk_comma_sku_uses_individual_job(self):
        records, client, remote, stock = self._bulk_stock_fixture()
        special = self.env['in.magento.product.binding'].create({'instance_id': self.instance.id,
            'product_id': self.shipping.id, 'sku': 'WITH,COMMA', 'external_id': '999', 'sync_inventory': True})
        with trap_jobs() as jobs:
            (records | special)._queue_inventory_batches()
            jobs.assert_jobs_count(2)
            jobs.assert_jobs_count(1, only=special._job_export_inventory)
            jobs.assert_jobs_count(1, only=records._job_export_inventory_batch)

    def test_inventory_opt_in_and_disabled_connection(self):
        with self.assertRaisesRegex(UserError, 'Enable Synchronize'):
            self.product_binding.action_export_inventory()
        self.product_binding.sync_inventory = True
        with self.assertRaisesRegex(UserError, 'Publish the product'):
            self.product_binding.action_export_inventory()
        self.product_binding.external_id = '101'
        with self.assertRaisesRegex(UserError, 'inventory tracking'):
            self.product_binding.action_export_inventory()
        self.product.is_storable = True
        with self.assertRaisesRegex(UserError, 'warehouse/source'):
            self.product_binding.action_export_inventory()
        self.instance.enabled = False
        with self.assertRaisesRegex(UserError, 'disabled'):
            self.product_binding._job_export_inventory()

    def test_inventory_auto_schedule_respects_interval(self):
        self.product.is_storable = True
        self.product_binding.write({'external_id': '101', 'sync_inventory': True})
        self.env['in.magento.inventory.source'].create({'instance_id': self.instance.id,
            'warehouse_id': self.warehouse.id, 'source_code': 'default'})
        self.instance.auto_export_inventory = True
        with trap_jobs() as jobs:
            self.instance._cron_schedule()
            jobs.assert_jobs_count(1, only=self.product_binding._job_export_inventory)
        with trap_jobs() as jobs:
            self.instance._cron_schedule()
            jobs.assert_jobs_count(0)


    def test_configurable_unmapped_child_or_parent_sku_collision_rejected(self):
        template, mapping, wizard = self._variant_fixture()
        wizard.parent_line_ids.parent_sku = self.product_binding.sku
        with self.assertRaisesRegex(UserError, 'already mapped|already used'):
            wizard.action_publish()
        wizard.parent_line_ids.parent_sku = 'VALID-PARENT'
        mapping.value_ids[0].unlink()
        with self.assertRaisesRegex(UserError, 'attribute and value'):
            wizard.action_publish()
        self.assertFalse(self.instance.template_ids)

    def test_configurable_images_and_archived_family(self):
        import base64
        from io import BytesIO
        from PIL import Image
        template, mapping, wizard = self._variant_fixture()
        wizard.sync_image = True
        with trap_jobs():
            wizard.action_publish()
        parent = self.instance.template_ids
        parent.external_id = '200'
        child = parent.child_ids[0]
        child.external_id = '201'
        buffer = BytesIO()
        Image.new('RGB', (8, 8), 'blue').save(buffer, format='PNG')
        template.image_1920 = base64.b64encode(buffer.getvalue())
        client = Mock()
        client.request.side_effect = [[], 1, [], 2]
        with self._mock_api(client):
            parent._job_export_image()
            child._job_export_image()
        self.assertEqual(len(self.instance.sync_log_ids), 2)
        self.assertEqual(set(self.instance.sync_log_ids.mapped('operation')), {'image'})
        self.assertFalse(any('base64' in value for value in self.instance.sync_log_ids.change_ids.mapped('new_value')))
        client.reset_mock(side_effect=True)
        with self._mock_api(client):
            parent._job_export_image()
            child._job_export_image()
        client.request.assert_not_called()
        template.active = False
        self.assertEqual(parent._payload()['product']['status'], 2)
        self.assertEqual(parent._variant_configuration(), [])
        self.assertEqual(child._payload()['product']['status'], 2)

    def test_inventory_mapping_company(self):
        self.env['in.magento.inventory.source'].create({'instance_id': self.instance.id,
            'warehouse_id': self.warehouse.id, 'source_code': 'default'})
        other = self.env['res.company'].create({'name': 'Stock other company'})
        warehouse = self.env['stock.warehouse'].search([('company_id', '=', other.id)], limit=1)
        self.assertTrue(warehouse)
        with self.assertRaises(UserError):
            with self.env.cr.savepoint():
                self.env['in.magento.inventory.source'].create({'instance_id': self.instance.id,
                    'warehouse_id': warehouse.id, 'source_code': 'foreign'})

    def test_inventory_stale_remote_mapping_blocks_write(self):
        self.product.is_storable = True
        self.product_binding.write({'external_id': '101', 'sync_inventory': True})
        self.env['in.magento.inventory.source'].create({'instance_id': self.instance.id,
            'warehouse_id': self.warehouse.id, 'source_code': 'default'})
        client = Mock()
        client.request.return_value = {'id': 999, 'type_id': 'simple'}
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'mapping changed'):
            self.product_binding._job_export_inventory()
        self.assertEqual(client.request.call_count, 1)

    def _attribute_publication_fixture(self):
        attribute = self.env['product.attribute'].create({'name': 'Material', 'create_variant': 'always',
            'value_ids': [Command.create({'name': 'Cotton'}), Command.create({'name': 'Wool'})]})
        action = attribute.action_publish_to_magento()
        wizard = self.env[action['res_model']].with_context(action['context']).create({})
        with trap_jobs() as trap:
            result = wizard.action_publish()
            trap.assert_jobs_count(1)
        # Notification follow-ups go directly to JS, without clean_action.
        followup = result['params']['next']
        self.assertEqual(followup['views'], [(False, 'form')])
        self.assertEqual(followup['res_model'], 'in.magento.attribute.mapping')
        self.assertEqual(followup['res_id'], self.instance.attribute_mapping_ids.id)
        return attribute, self.instance.attribute_mapping_ids

    def test_attribute_create_update_and_set_assignment(self):
        attribute, mapping = self._attribute_publication_fixture()
        state = {'attribute': None, 'members': []}
        def request(method, path, **kwargs):
            if method == 'GET' and path.startswith('products/attributes/'):
                if not state['attribute']:
                    raise MagentoError('Missing', 404)
                return deepcopy(state['attribute'])
            if method == 'POST' and path == 'products/attributes':
                self.assertFalse(kwargs.get('replay_safe', False))
                result = deepcopy(kwargs['payload']['attribute'])
                result['attribute_id'] = 250
                previous_options = deepcopy((state['attribute'] or {}).get('options', []))
                self.assertFalse(any(row.get('value') for row in result['options']))
                for index, option in enumerate(result['options']):
                    option.setdefault('value', str(1000 + len(previous_options) + index))
                result['options'] = previous_options + result['options']
                state['attribute'] = result
                return deepcopy(result)
            if method == 'GET' and path.endswith('/attributes'):
                return deepcopy(state['members'])
            if method == 'GET' and path.endswith('/groups/list'):
                return {'items': [{'attribute_set_id': self.instance.attribute_set_id,
                    'attribute_group_id': 7, 'attribute_group_name': 'General'}]}
            if method == 'POST' and path == 'products/attribute-sets/attributes':
                state['members'] = [{'attribute_code': mapping.attribute_code}]
                return 1
            self.fail((method, path))
        client = Mock()
        client.request.side_effect = request
        with self._mock_api(client), trap_jobs() as trap:
            mapping._job_export()
            trap.assert_jobs_count(1)
        ids = mapping.value_ids.mapped('external_id')
        self.assertEqual(len(ids), 2)
        self.assertFalse(mapping.verified_at)
        with self._mock_api(client):
            mapping._job_assign_attribute_set()
        self.assertTrue(mapping.verified_at)
        state['attribute']['options'].append({'value': '9000', 'label': 'External', 'store_labels': [{'store_id': 2, 'label': 'Extern'}]})
        state['attribute']['default_value'] = '9000'
        attribute.value_ids[0].name = 'Organic Cotton'
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'admin label'):
            mapping._job_export()
        # Operator explicitly renames the corresponding Magento admin label.
        state['attribute']['options'][0]['label'] = 'Organic Cotton'
        self.env['product.attribute.value'].create({'attribute_id': attribute.id, 'name': 'Silk'})
        with self._mock_api(client), trap_jobs():
            mapping._job_export()
            mapping._job_export()
        self.assertEqual(len(mapping.value_ids), 3)
        self.assertTrue(set(ids).issubset(mapping.value_ids.mapped('external_id')))
        self.assertEqual(len(state['attribute']['options']), 4)
        external = next(row for row in state['attribute']['options'] if row['value'] == '9000')
        self.assertEqual(external['store_labels'][0]['label'], 'Extern')
        self.assertEqual(state['attribute']['default_value'], '9000')
        self.assertEqual(set(self.instance.sync_log_ids.mapped('operation')), {'attribute'})
        self.assertEqual(set(self.instance.sync_log_ids.attribute_mapping_id.ids), {mapping.id})

    def test_attribute_existing_unlinked_and_stale_identity_block_writes(self):
        attribute, mapping = self._attribute_publication_fixture()
        client = Mock()
        client.request.return_value = {'attribute_id': 250, 'frontend_input': 'select',
            'scope': 'global', 'is_user_defined': True}
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'already exists'):
            mapping._job_export()
        mapping.external_id = 251
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'identity changed'):
            mapping._job_export()
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))
        with self.assertRaisesRegex(UserError, 'keeps its'):
            mapping.attribute_code = 'new_code'

    def test_attribute_unlinked_option_is_not_adopted_by_label(self):
        attribute, mapping = self._attribute_publication_fixture()
        mapping.external_id = 250
        client = Mock()
        client.request.return_value = {'attribute_id': 250, 'frontend_input': 'select',
            'scope': 'global', 'is_user_defined': True, 'options': [{'label': 'Cotton', 'value': '50'}]}
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'explicitly'):
            mapping._job_export()
        self.assertEqual(client.request.call_count, 1)
        self.assertFalse(mapping.value_ids)

    def test_attribute_publication_requires_supported_values_and_access(self):
        attribute, mapping = self._attribute_publication_fixture()
        attribute.create_variant = 'dynamic'
        with self.assertRaisesRegex(UserError, 'instant'):
            mapping.action_export()
        viewer = new_test_user(self.env, login='attribute_viewer', groups='base.group_user,in_odoo_magento_connector.group_magento_user')
        with self.assertRaises(AccessError):
            attribute.with_user(viewer).action_publish_to_magento()

    def test_publish_button_on_all_four_form_models(self):
        from lxml import etree
        for model, view in [('product.template', 'product.product_template_only_form_view'),
                            ('product.product', 'product.product_normal_form_view'),
                            ('product.category', 'product.product_category_form_view'),
                            ('product.attribute', 'product.product_attribute_view_form')]:
            arch = self.env[model].get_view(view_id=self.env.ref(view).id, view_type='form')['arch']
            buttons = etree.fromstring(arch).xpath('//button[@name="action_publish_to_magento"]')
            self.assertEqual(len(buttons), 1, model)

    def _fulfillment_fixture(self, quantity=2):
        self.product.is_storable = True
        self.env['stock.quant']._update_available_quantity(self.product, self.warehouse.lot_stock_id, 10)
        self.env['in.magento.inventory.source'].create({'instance_id': self.instance.id, 'warehouse_id': self.warehouse.id, 'source_code': 'default'})
        inbox = self._inbox()
        inbox._job_import()
        order = inbox.sale_order_id
        order.action_confirm()
        picking = order.picking_ids.filtered(lambda row: row.picking_type_id.code == 'outgoing')
        picking.action_assign()
        for move in picking.move_ids:
            move.quantity = quantity
            move.picked = True
        picking._action_done()
        with trap_jobs():
            inbox._prepare_shipments()
        return inbox, picking, inbox.shipment_ids.filtered(lambda row: row.picking_id == picking)

    def test_partial_shipment_quantity_tracking_and_idempotency(self):
        inbox, picking, shipment = self._fulfillment_fixture(quantity=1)
        picking.in_magento_tracking_number = 'TRACK-TEST'
        payload = shipment._shipment_payload()
        self.assertEqual(payload['items'], [{'order_item_id': 2001, 'qty': 1}])
        self.assertEqual(payload['tracks'][0]['track_number'], 'TRACK-TEST')
        self.assertFalse(payload['notify'])
        self.assertTrue(inbox.sale_order_id.picking_ids.filtered(lambda row: row.backorder_id == picking))
        client = Mock()
        client.request.side_effect = [{'items': [], 'total_count': 0}, deepcopy(self.fixture), 301]
        with self._mock_api(client), trap_jobs():
            shipment._job_export_shipment()
            shipment._job_export_shipment()
        self.assertEqual(shipment.external_id, '301')
        self.assertEqual(client.request.call_count, 3)
        self.assertEqual(client.request.call_args.args, ('POST', 'order/1001/ship'))
        self.assertEqual(len(self.instance.sync_log_ids.filtered(lambda row: row.operation == 'shipment')), 1)

    def test_shipment_recovery_matches_reference_quantity_and_source(self):
        inbox, picking, shipment = self._fulfillment_fixture()
        payload = shipment._shipment_payload()
        client = Mock()
        client.request.side_effect = [{'items': [{'entity_id': 301, 'order_id': 1001,
            'items': payload['items'], 'extension_attributes': {'source_code': 'default'}}], 'total_count': 1},
            {'items': [{'comment': payload['comment']['comment']}]}]
        with self._mock_api(client), trap_jobs():
            shipment._job_export_shipment()
        self.assertEqual(shipment.external_id, '301')
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))

    def test_inventory_waits_for_shipment_acknowledgement(self):
        from odoo.addons.queue_job.exception import RetryableJobError
        inbox, picking, shipment = self._fulfillment_fixture()
        self.product_binding.write({'sync_inventory': True, 'external_id': '101'})
        client = Mock()
        with self._mock_api(client), self.assertRaisesRegex(RetryableJobError, 'completed Odoo deliveries'):
            self.product_binding._job_export_inventory()
        client.request.assert_not_called()
        shipment.external_id = '301'
        self.assertEqual(self.product_binding._inventory_items()[0]['quantity'], 8)

    def test_cancellation_export_and_already_canceled_retry(self):
        inbox = self._inbox()
        inbox._job_import()
        inbox.sale_order_id.action_cancel()
        client = Mock()
        client.request.side_effect = [deepcopy(self.fixture), True]
        with self._mock_api(client):
            inbox._job_sync_fulfillment()
        self.assertTrue(inbox.cancellation_synced_at)
        self.assertEqual(self.instance.sync_log_ids.filtered(lambda row: row.operation == 'cancellation').direction, 'export')
        remote = deepcopy(self.fixture)
        remote['state'] = 'canceled'
        client.request.side_effect = [remote]
        before = len(self.instance.sync_log_ids)
        with self._mock_api(client):
            inbox._job_sync_fulfillment()
        self.assertEqual(len(self.instance.sync_log_ids), before)

    def test_cancellation_import_releases_odoo_reservations(self):
        self.product.is_storable = True
        self.env['stock.quant']._update_available_quantity(self.product, self.warehouse.lot_stock_id, 10)
        inbox = self._inbox()
        inbox._job_import()
        inbox.sale_order_id.action_confirm()
        inbox.sale_order_id.picking_ids.action_assign()
        remote = deepcopy(self.fixture)
        remote['state'] = 'canceled'
        remote['items'][0]['qty_canceled'] = 2
        client = Mock()
        client.request.return_value = remote
        with self._mock_api(client):
            inbox._job_sync_fulfillment()
        self.assertEqual(inbox.sale_order_id.state, 'cancel')
        self.assertEqual(set(inbox.sale_order_id.picking_ids.mapped('state')), {'cancel'})
        self.assertEqual(self.instance.sync_log_ids.filtered(lambda row: row.operation == 'cancellation').direction, 'import')
        self.assertEqual(client.request.call_count, 1)

    def test_cancellation_blocks_processed_orders(self):
        inbox, picking, shipment = self._fulfillment_fixture()
        remote = deepcopy(self.fixture)
        remote['state'] = 'canceled'
        client = Mock()
        client.request.return_value = remote
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'deliveries or invoices'):
            inbox._job_sync_fulfillment()
        self.assertEqual(inbox.sale_order_id.state, 'sale')
        remote['state'] = 'processing'
        remote['items'][0]['qty_invoiced'] = 1
        with self.assertRaisesRegex(UserError, 'processed'):
            inbox._check_remote_cancellation(remote)

    def test_shipment_overdelivery_is_blocked_before_write(self):
        inbox, picking, shipment = self._fulfillment_fixture()
        remote = deepcopy(self.fixture)
        remote['items'][0]['qty_shipped'] = 1
        client = Mock()
        client.request.side_effect = [{'items': [], 'total_count': 0}, remote]
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'exceeds'):
            shipment._job_export_shipment()
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))

    def test_fulfillment_schedule_is_opt_in_and_interval_driven(self):
        inbox = self._inbox()
        inbox._job_import()
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(0)
        self.instance.write({'auto_export_shipments': True, 'auto_import_cancellations': True})
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(1)
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(1)

    def test_shipment_recovery_rejects_changed_quantity(self):
        inbox, picking, shipment = self._fulfillment_fixture()
        payload = shipment._shipment_payload()
        client = Mock()
        client.request.side_effect = [{'items': [{'entity_id': 301, 'order_id': 1001,
            'items': [{'order_item_id': 2001, 'qty': 1}], 'extension_attributes': {'source_code': 'default'}}], 'total_count': 1},
            {'items': [{'comment': payload['comment']['comment']}]}]
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'quantities or source differ'):
            shipment._job_export_shipment()
        self.assertFalse(shipment.external_id)
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))

    def test_cancellation_disabled_direction_does_not_modify_order(self):
        inbox = self._inbox()
        inbox._job_import()
        remote = deepcopy(self.fixture)
        remote['state'] = 'canceled'
        client = Mock()
        client.request.return_value = remote
        with self._mock_api(client):
            inbox._job_sync_fulfillment(import_cancel=False)
        self.assertEqual(inbox.sale_order_id.state, 'draft')
        self.assertFalse(inbox.cancellation_synced_at)

    def test_shipment_blocks_unmanaged_remote_delivery(self):
        inbox, picking, shipment = self._fulfillment_fixture(quantity=1)
        remote = deepcopy(self.fixture)
        remote['items'][0]['qty_shipped'] = 1
        client = Mock()
        client.request.side_effect = [{'items': [], 'total_count': 0}, remote]
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'not acknowledged'):
            shipment._job_export_shipment()
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))

    def _customer_fixture(self, *, linked=False):
        partner = self.env['res.partner'].create({'name': 'Local Customer', 'email': 'customer@example.test',
            'company_id': self.company.id})
        binding = self.env['in.magento.partner.binding'].create({'instance_id': self.instance.id,
            'partner_id': partner.id, 'sync_customer': True,
            'external_id': '101' if linked else False, 'verified_at': fields.Datetime.now() if linked else False})
        remote = {'id': 101, 'website_id': 1, 'store_id': 1, 'group_id': 1, 'email': partner.email,
            'firstname': 'Local', 'lastname': 'Customer', 'addresses': [], 'taxvat': ''}
        return partner, binding, remote

    def test_customer_registration_import_preserves_identity_and_privacy(self):
        remote = {'id': 501, 'website_id': 1, 'store_id': 1, 'firstname': 'New', 'lastname': 'Registration',
            'email': 'new@example.test', 'addresses': [{'id': 800, 'firstname': 'New', 'lastname': 'Registration',
            'country_id': 'DE', 'region': {}, 'street': ['Private Road'], 'city': 'Berlin', 'postcode': '10115',
            'telephone': '123456', 'default_billing': True}]}
        client = Mock()
        client.request.return_value = remote
        with self._mock_api(client):
            self.instance._job_import_customer(501)
            self.instance._job_import_customer(501)
        binding = self.instance.customer_ids.filtered(lambda b: b.external_id == '501')
        self.assertEqual(len(binding), 1)
        self.assertEqual(len(binding.address_ids), 1)
        self.assertTrue(binding.sync_customer)
        self.assertEqual(binding.address_ids.partner_id.street, 'Private Road')
        self.assertEqual(client.request.call_count, 1)
        log = self.instance.sync_log_ids.filtered(lambda l: l.operation == 'customer')
        self.assertEqual(len(log), 1)
        self.assertFalse(log.change_ids)
        self.assertNotIn('new@example.test', log.summary)
        binding.partner_id.name = 'Odoo Owner'
        with self._mock_api(client):
            self.instance._job_import_customer(501)
        self.assertEqual(binding.partner_id.name, 'Odoo Owner')

    def test_customer_wrong_website_and_email_collision_stop(self):
        partner, binding, remote = self._customer_fixture()
        remote['website_id'] = 9
        client = Mock()
        client.request.return_value = remote
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'website'):
            self.instance._job_import_customer(101)
        binding.external_id = '101'
        remote.update(website_id=1, email='different@example.test')
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'email addresses differ'):
            binding.action_link()
        self.assertFalse(binding.verified_at)

    def test_customer_creation_and_duplicate_account_protection(self):
        partner, binding, remote = self._customer_fixture()
        client = Mock()
        client.request.side_effect = [[{'id': 1, 'website_id': 1}], {'items': [remote]}]
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'already uses this email'):
            binding._job_export_customer()
        self.assertFalse(binding.external_id)
        client.request.side_effect = [[{'id': 1, 'website_id': 1}], {'items': []}, remote]
        with self._mock_api(client), trap_jobs():
            binding._job_export_customer()
        self.assertEqual(binding.external_id, '101')
        self.assertTrue(binding.verified_at)
        with self._mock_api(client), trap_jobs() as trap:
            client.reset_mock()
            binding._job_customer_if_changed()
            client.request.assert_not_called()
            trap.assert_jobs_count(0)

    def test_customer_profile_update_omits_addresses_preserves_remote_fields(self):
        partner, binding, remote = self._customer_fixture(linked=True)
        partner.name = 'Changed Customer'
        remote.update(default_billing='800', custom_attributes=[{'attribute_code': 'loyalty', 'value': 'preserved'}])
        client = Mock()
        client.request.side_effect = [remote, dict(remote, firstname='Changed')]
        with self._mock_api(client), trap_jobs():
            binding._job_export_customer()
        payload = client.request.call_args.kwargs['payload']['customer']
        self.assertEqual(payload['firstname'], 'Changed')
        self.assertEqual(payload['custom_attributes'], remote['custom_attributes'])
        self.assertEqual(payload['default_billing'], '800')
        self.assertNotIn('addresses', payload)
        self.assertNotIn('password', payload)
        with self.assertRaisesRegex(UserError, 'keeps its identity'):
            binding.external_id = '999'

    def test_customer_address_add_preserves_unmapped_and_is_not_replay_safe(self):
        partner, binding, remote = self._customer_fixture(linked=True)
        child = self.env['res.partner'].create({'parent_id': partner.id, 'name': partner.name,
            'street': 'New Road', 'city': 'Berlin', 'zip': '10115', 'phone': '12345',
            'country_id': self.env.ref('base.de').id})
        row = self.env['in.magento.customer.address'].create({'binding_id': binding.id, 'partner_id': child.id})
        unmanaged = {'id': 800, 'firstname': 'Other', 'lastname': 'Address', 'street': ['Keep'],
            'custom_attributes': [{'attribute_code': 'tag', 'value': 'keep'}]}
        remote['addresses'] = [unmanaged]
        result = dict(remote, addresses=[unmanaged, dict(row._values(), id=801)])
        client = Mock()
        client.request.side_effect = [remote, result]
        with self._mock_api(client):
            row._job_export_address()
        self.assertEqual(row.external_id, '801')
        call = client.request.call_args
        self.assertFalse(call.kwargs['replay_safe'])
        self.assertEqual(call.kwargs['payload']['customer']['addresses'][0], unmanaged)
        client.reset_mock()
        with self._mock_api(client):
            row._job_export_address()
        client.request.assert_not_called()

    def test_customer_address_unknown_create_is_not_duplicated(self):
        partner, binding, remote = self._customer_fixture(linked=True)
        child = self.env['res.partner'].create({'parent_id': partner.id, 'name': partner.name,
            'street': 'New Road', 'city': 'Berlin', 'phone': '12345', 'country_id': self.env.ref('base.de').id})
        row = self.env['in.magento.customer.address'].create({'binding_id': binding.id, 'partner_id': child.id})
        remote['addresses'] = [dict(row._values(), id=801)]
        client = Mock()
        client.request.return_value = remote
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'matching Magento address'):
            row._job_export_address()
        self.assertEqual(client.request.call_count, 1)
        self.assertFalse(row.external_id)

    def test_customer_poll_scope_checkpoint_and_no_private_job_payload(self):
        self.instance.order_batch_size = 2
        client = Mock()
        client.request.return_value = {'items': [{'id': 101, 'website_id': 1}, {'id': 102, 'website_id': 1}]}
        with self._mock_api(client), trap_jobs() as trap:
            self.instance._job_customer_page('2026-01-01 00:00:00', '2026-10-14 00:00:00', 0)
            trap.assert_jobs_count(3)
        self.assertFalse(self.instance.customer_cursor)
        self.assertEqual(client.request.call_args.kwargs['params']['searchCriteria[filter_groups][0][filters][0][field]'], 'website_id')
        client.request.return_value = {'items': []}
        with self._mock_api(client), trap_jobs() as trap:
            self.instance._job_customer_page('2026-01-01 00:00:00', '2026-10-14 00:00:00', 102)
            trap.assert_jobs_count(0)
        self.assertEqual(self.instance.customer_cursor, fields.Datetime.to_datetime('2026-10-14 00:00:00'))

    def test_customer_sync_is_opt_in_and_neutralized_database_is_blocked(self):
        partner, binding, remote = self._customer_fixture(linked=True)
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(0)
        self.instance.write({'auto_import_customers': True, 'auto_export_customers': True})
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(2)
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(2)
        self.env['ir.config_parameter'].sudo().set_param('database.is_neutralized', 'true')
        with self.assertRaisesRegex(UserError, 'neutralized'):
            binding._job_export_customer()

    def test_customer_export_requires_mapping_verification_and_manager(self):
        partner, binding, remote = self._customer_fixture()
        binding.external_id = '101'
        with self.assertRaisesRegex(UserError, 'Verify'):
            with self._mock_api(Mock()):
                binding._job_export_customer()
        viewer = new_test_user(self.env, login='customer_viewer', groups='base.group_user,in_odoo_magento_connector.group_magento_user')
        with self.assertRaises(AccessError):
            binding.with_user(viewer).action_export()
        with self.assertRaises(AccessError):
            self.instance.with_user(viewer).action_import_customers()

    def test_barcode_custom_mapping_verification_and_fingerprint(self):
        mapping = self.env['in.magento.catalog.field'].create({'instance_id': self.instance.id,
            'field_id': self.env['ir.model.fields']._get('product.product', 'barcode').id, 'attribute_code': 'ean'})
        self.product.barcode = '1234567890123'
        with self.assertRaisesRegex(UserError, 'Verify custom'):
            self.product_binding._payload()
        client = Mock()
        client.request.side_effect = [{'attribute_id': 555, 'is_user_defined': True, 'frontend_input': 'text'}, [{'attribute_code': 'ean'}]]
        with self._mock_api(client):
            mapping.action_verify()
        self.assertIn({'attribute_code': 'ean', 'value': self.product.barcode}, self.product_binding._payload()['product']['custom_attributes'])
        before = self.product_binding._fingerprint()
        self.product.barcode = '9999999999999'
        self.assertNotEqual(before, self.product_binding._fingerprint())
        mapping.attribute_code = 'barcode'
        self.assertFalse(mapping.verified_at)

    def test_custom_field_rejects_reserved_and_relational_mapping(self):
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env['in.magento.catalog.field'].create({'instance_id': self.instance.id,
                'field_id': self.env['ir.model.fields']._get('product.product', 'barcode').id, 'attribute_code': 'price'})
        with self.assertRaises(ValidationError), self.env.cr.savepoint():
            self.env['in.magento.catalog.field'].create({'instance_id': self.instance.id,
                'field_id': self.env['ir.model.fields']._get('product.product', 'categ_id').id, 'attribute_code': 'external_category'})

    def test_category_automatic_delta_only_writes_after_name_change(self):
        category = self.env['product.category'].create({'name': 'Published category'})
        binding = self.env['in.magento.category.binding'].create({'instance_id': self.instance.id,
            'category_id': category.id, 'external_id': '10'})
        binding.last_fingerprint = binding._fingerprint()
        client = Mock()
        with self._mock_api(client):
            binding._job_export_if_changed()
        client.request.assert_not_called()
        category.name = 'Renamed category'
        client.request.side_effect = [{'id': 10, 'name': 'Published category', 'parent_id': 2}, {'id': 10}]
        with self._mock_api(client):
            binding._job_export_if_changed()
        self.assertEqual(client.request.call_args.kwargs['payload']['category']['name'], 'Renamed category')
        self.assertEqual(binding.last_fingerprint, binding._fingerprint())

    def test_attribute_delta_and_metadata_schedule_are_opt_in(self):
        attribute, mapping = self._attribute_publication_fixture()
        mapping.external_id = 250
        mapping.last_fingerprint = mapping._fingerprint()
        with self._mock_api(Mock()) as client:
            mapping._job_export_if_changed()
            client.request.assert_not_called()
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(0)
        self.instance.auto_export_attributes = True
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(0)
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(0)
        attribute.name = 'Renamed Attribute'
        self.assertNotEqual(mapping.last_fingerprint, mapping._fingerprint())
        self.instance.last_metadata_schedule = False
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(1)

    def test_customer_prequeue_checks_addresses_without_profile_jobs(self):
        partner, binding, remote = self._customer_fixture(linked=True)
        binding.last_fingerprint = fingerprint(binding._profile())
        child = self.env['res.partner'].create({'parent_id': partner.id, 'name': partner.name,
            'street': 'First Street', 'city': 'Berlin', 'phone': '12345', 'country_id': self.env.ref('base.de').id})
        address = self.env['in.magento.customer.address'].create({
            'binding_id': binding.id, 'partner_id': child.id, 'external_id': '801'})
        address.last_fingerprint = fingerprint(address._values())
        self.instance.auto_export_customers = True
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(0)
        child.street = 'Second Street'
        self.instance.last_customer_schedule = False
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(1, only=address._job_export_address)
            trap.assert_jobs_count(1)
        # A queued update that was reverted must remain a no-op in the worker.
        child.street = 'First Street'
        client = Mock()
        with self._mock_api(client):
            address._job_export_address()
        client.request.assert_not_called()
        address.external_id = False
        with trap_jobs() as trap:
            binding._schedule_customer_exports()
            trap.assert_jobs_count(1, only=address._job_export_address)

    def test_customer_prequeue_profile_change_invalid_and_opt_out(self):
        partner, binding, remote = self._customer_fixture(linked=True)
        binding.last_fingerprint = fingerprint(binding._profile())
        partner.name = 'Renamed Customer'
        with trap_jobs() as trap:
            binding._schedule_customer_exports()
            trap.assert_jobs_count(1, only=binding._job_customer_if_changed)
        partner.name = 'Local Customer'
        with self._mock_api(Mock()) as client, trap_jobs() as trap:
            binding._job_customer_if_changed()
            client.request.assert_not_called()
            trap.assert_jobs_count(0)
        partner.email = False
        with trap_jobs() as trap:
            binding._schedule_customer_exports()
            trap.assert_jobs_count(1)
        binding.sync_customer = False
        with trap_jobs() as trap:
            binding._schedule_customer_exports()
            trap.assert_jobs_count(0)

    def test_customer_prequeue_invalid_address_is_not_silently_skipped(self):
        partner, binding, remote = self._customer_fixture(linked=True)
        binding.last_fingerprint = fingerprint(binding._profile())
        child = self.env['res.partner'].create({'parent_id': partner.id, 'name': partner.name})
        address = self.env['in.magento.customer.address'].create({'binding_id': binding.id, 'partner_id': child.id})
        with trap_jobs() as trap:
            binding._schedule_customer_exports()
            trap.assert_jobs_count(1, only=address._job_export_address)
        address.sync_address = False
        with trap_jobs() as trap:
            binding._schedule_customer_exports()
            trap.assert_jobs_count(0)

    def test_metadata_prequeue_includes_parent_and_attribute_value_changes(self):
        category = self.env['product.category'].create({'name': 'Child'})
        parent = self.env['in.magento.category.binding'].create({'instance_id': self.instance.id,
            'category_id': self.env['product.category'].create({'name': 'Parent'}).id, 'external_id': '10'})
        binding = self.env['in.magento.category.binding'].create({'instance_id': self.instance.id,
            'category_id': category.id, 'external_id': '11', 'parent_id': parent.id})
        attribute, mapping = self._attribute_publication_fixture()
        mapping.external_id = '250'
        for record in parent | binding:
            record.last_fingerprint = record._fingerprint()
        mapping.last_fingerprint = mapping._fingerprint()
        self.instance.write({'auto_export_categories': True, 'auto_export_attributes': True})
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(0)
        parent.external_id = '12'
        attribute.value_ids[0].name = 'Changed value'
        self.instance.last_metadata_schedule = False
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(2)
            trap.assert_jobs_count(1, only=binding._job_export_if_changed)
            trap.assert_jobs_count(1, only=mapping._job_export_if_changed)

    def _image_fixture(self, color='blue'):
        import base64
        from io import BytesIO
        from PIL import Image
        buffer = BytesIO()
        Image.new('RGB', (8, 8), color).save(buffer, format='PNG')
        return base64.b64encode(buffer.getvalue())

    def test_image_acknowledgement_skips_jobs_and_reads_for_other_catalog_changes(self):
        binding = self.product_binding
        binding.write({'external_id': '101', 'sync_image': True})
        self.product.image_1920 = self._image_fixture()
        client = Mock()
        client.request.side_effect = [[], 42]
        with self._mock_api(client):
            binding._job_export_image()
        self.assertEqual(binding.last_image_fingerprint, image_fingerprint(binding, self.product.image_1920))
        client.reset_mock(side_effect=True)
        with self._mock_api(client):
            binding._job_export_image()
        client.request.assert_not_called()
        self.product.name = 'Changed product name'
        client.request.side_effect = [dict(binding._payload()['product'], id=101), {'id': 101}]
        with self._mock_api(client), trap_jobs() as trap:
            binding._job_export()
            trap.assert_jobs_count(0, only=binding._job_export_image)
        self.product.image_1920 = self._image_fixture('red')
        client.request.side_effect = [dict(binding._payload()['product'], id=101), {'id': 101}]
        with self._mock_api(client), trap_jobs() as trap:
            binding._job_export()
            trap.assert_jobs_count(1, only=binding._job_export_image)

    def test_image_failed_ack_and_retry_readback_do_not_duplicate_upload(self):
        import hashlib
        import base64
        binding = self.product_binding
        binding.write({'external_id': '101', 'sync_image': True})
        self.product.image_1920 = self._image_fixture()
        client = Mock()
        client.request.side_effect = [[], None]
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'acknowledge'):
            binding._job_export_image()
        self.assertFalse(binding.last_image_fingerprint)
        marker = 'odoo-' + hashlib.sha256(base64.b64decode(self.product.image_1920)).hexdigest()[:24]
        client.reset_mock(side_effect=True)
        client.request.return_value = [{'file': '/o/d/' + marker + '_1.png'}]
        with self._mock_api(client):
            binding._job_export_image()
        self.assertEqual(client.request.call_count, 1)
        self.assertTrue(binding.last_image_fingerprint)
        self.assertFalse(self.instance.sync_log_ids.filtered(lambda log: log.operation == 'image'))
        # A copied mapping must verify the image on its own remote product.
        product = self.env['product.product'].create({'name': 'Other image product',
            'image_1920': self.product.image_1920})
        self.assertNotIn('last_image_fingerprint', binding.copy_data()[0])
        copied = self.env['in.magento.product.binding'].create({'instance_id': self.instance.id,
            'product_id': product.id, 'sync_image': True, 'sku': 'COPY-IMAGE', 'external_id': '102'})
        self.assertFalse(copied.last_image_fingerprint)
        self.assertNotEqual(image_fingerprint(binding, self.product.image_1920),
            image_fingerprint(copied, copied.product_id.image_1920))
        client.reset_mock()
        with self._mock_api(client):
            copied._job_export_image()
        self.assertEqual(client.request.call_count, 1)

    def test_registration_matching_odoo_email_requires_explicit_link(self):
        partner, binding, remote = self._customer_fixture()
        client = Mock()
        client.request.return_value = remote
        before = self.env['res.partner'].search_count([])
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'explicit Customer Mapping'):
            self.instance._job_import_customer(101)
        self.assertEqual(self.env['res.partner'].search_count([]), before)

    def test_customer_mapping_rejects_cross_company_and_order_snapshot(self):
        partner, binding, remote = self._customer_fixture(linked=True)
        company = self.env['res.company'].create({'name': 'Other customer company'})
        other_partner = self.env['res.partner'].create({'name': 'Other Customer', 'company_id': company.id})
        with self.assertRaises(UserError), self.env.cr.savepoint():
            self.env['in.magento.partner.binding'].create({'instance_id': self.instance.id, 'partner_id': other_partner.id})
        inbox = self._inbox()
        inbox._job_import()
        customer_binding = self.instance.customer_ids.filtered(lambda b: b.partner_id == inbox.sale_order_id.partner_id)
        with self.assertRaisesRegex(ValidationError, 'used on an order'), self.env.cr.savepoint():
            self.env['in.magento.customer.address'].create({'binding_id': customer_binding.id,
                'partner_id': inbox.sale_order_id.partner_invoice_id.id})

    def test_custom_mapping_refuses_unmanaged_select_options(self):
        mapping = self.env['in.magento.catalog.field'].create({'instance_id': self.instance.id,
            'field_id': self.env['ir.model.fields']._get('product.product', 'barcode').id, 'attribute_code': 'ean'})
        client = Mock()
        client.request.return_value = {'attribute_id': 555, 'is_user_defined': True, 'frontend_input': 'select'}
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'text, textarea or boolean'):
            mapping.action_verify()
        self.assertFalse(mapping.verified_at)

    def test_category_delta_refuses_silent_reparenting(self):
        category = self.env['product.category'].create({'name': 'Published category'})
        binding = self.env['in.magento.category.binding'].create({'instance_id': self.instance.id,
            'category_id': category.id, 'external_id': '10'})
        client = Mock()
        client.request.return_value = {'id': 10, 'parent_id': 999}
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'Move the category'):
            binding._job_export_if_changed()
        self.assertEqual(client.request.call_count, 1)
        self.assertFalse(binding.last_fingerprint)

    def _accounting_setup(self, inbox=None):
        income = self.env['account.account'].create({'name': 'Magento test income', 'code': 'MGINCOME', 'account_type': 'income', 'company_ids': [Command.set(self.company.ids)]})
        receivable = self.env['account.account'].create({'name': 'Magento test receivable', 'code': 'MGRECV', 'account_type': 'asset_receivable', 'reconcile': True, 'company_ids': [Command.set(self.company.ids)]})
        journal = self.env['account.journal'].create({'name': 'Magento Test Sales', 'code': 'MGTS', 'type': 'sale', 'company_id': self.company.id, 'default_account_id': income.id})
        self.product.property_account_income_id = income
        self.shipping.property_account_income_id = income
        self.instance.write({'financial_journal_id': journal.id, 'import_invoices': True, 'import_refunds': True, 'import_payment_status': True})
        inbox = inbox or self._inbox()
        inbox._job_import()
        inbox.sale_order_id.partner_id.property_account_receivable_id = receivable
        return inbox

    def _accounting_document(self, inbox, *, kind='invoice', external_id=901, qty=2, shipping=5, invoice_id=None):
        data = {'entity_id': external_id, 'order_id': int(inbox.external_id), 'state': 2,
            'order_currency_code': 'EUR', 'invoice_id': invoice_id, 'created_at': '2026-10-13 10:00:00',
            'grand_total': qty * 54 + shipping * 1.2, 'tax_amount': qty * 9 + shipping * .2,
            'subtotal': qty * 50, 'discount_amount': qty * 5, 'shipping_amount': shipping, 'shipping_tax_amount': shipping * .2,
            'items': [{'order_item_id': 2001, 'qty': qty, 'price': 50, 'row_total': qty * 50, 'discount_amount': qty * 5, 'tax_amount': qty * 9}]}
        doc = self.env['in.magento.accounting'].sudo().create({'order_binding_id': inbox.id, 'kind': kind, 'external_id': str(external_id), 'name': f'TEST-{external_id}'})
        return doc, data

    def test_accounting_import_draft_and_repeat_without_payment_or_remote_write(self):
        inbox = self._accounting_setup()
        doc, data = self._accounting_document(inbox)
        payments = self.env['account.payment'].search_count([])
        client = Mock(); client.request.return_value = data
        with self._mock_api(client):
            first = doc._job_import_document()
            self.assertEqual(first, doc._job_import_document())
        self.assertEqual(doc.move_id.state, 'draft')
        self.assertEqual(doc.move_id.amount_total, 114)
        self.assertEqual(doc.move_id.invoice_line_ids.sale_line_ids, inbox.sale_order_id.order_line)
        self.assertEqual(self.env['account.payment'].search_count([]), payments)
        self.assertTrue(all(call.args[0] == 'GET' for call in client.request.call_args_list))
        self.assertEqual(len(self.instance.sync_log_ids.filtered(lambda l: l.operation == 'accounting')), 1)

    def test_accounting_partial_invoices_and_excess_quantity(self):
        inbox = self._accounting_setup()
        client = Mock()
        with self._mock_api(client):
            for external_id, shipping in [(901, 5), (902, 0)]:
                doc, data = self._accounting_document(inbox, external_id=external_id, qty=1, shipping=shipping)
                client.request.return_value = data
                doc._job_import_document()
            excess, data = self._accounting_document(inbox, external_id=903, qty=1, shipping=0)
            client.request.return_value = data
            with self.assertRaisesRegex(UserError, 'quantity exceeds'):
                excess._job_import_document()
        self.assertFalse(excess.move_id)
        self.assertEqual(sum(inbox.accounting_ids.move_id.mapped('amount_total')), 114)

    def test_accounting_mismatch_rolls_back_and_unknown_items_fail(self):
        inbox = self._accounting_setup()
        doc, data = self._accounting_document(inbox)
        client = Mock(); client.request.return_value = data
        before = self.env['account.move'].search_count([])
        with self._mock_api(client):
            data['grand_total'] -= 1
            with self.assertRaisesRegex(UserError, 'totals differ'):
                doc._job_import_document()
            self.assertEqual(self.env['account.move'].search_count([]), before)
            data['items'][0]['order_item_id'] = 99999
            with self.assertRaisesRegex(UserError, 'unknown order item'):
                doc._job_import_document()
        self.assertFalse(doc.move_id)

    def test_accounting_changed_remote_document_is_not_overwritten(self):
        inbox = self._accounting_setup()
        doc, data = self._accounting_document(inbox)
        client = Mock(); client.request.return_value = data
        with self._mock_api(client):
            doc._job_import_document()
            data['grand_total'] += 1
            with self.assertRaisesRegex(UserError, 'document changed'):
                doc._job_import_document()
        self.assertEqual(doc.move_id.amount_total, 114)

    def test_creditmemo_partial_import_links_original_invoice(self):
        inbox = self._accounting_setup()
        invoice, data = self._accounting_document(inbox)
        client = Mock(); client.request.return_value = data
        with self._mock_api(client):
            invoice._job_import_document()
            refund, credit = self._accounting_document(inbox, kind='creditmemo', external_id=801, qty=1, shipping=0, invoice_id=901)
            client.request.return_value = credit
            refund._job_import_document()
            refund._job_import_document()
        self.assertEqual(refund.move_id.move_type, 'out_refund')
        self.assertEqual(refund.move_id.state, 'draft')
        self.assertEqual(refund.move_id.amount_total, 54)
        self.assertEqual(refund.move_id.reversed_entry_id, invoice.move_id)
        self.assertFalse(refund.return_picking_ids)

    def test_creditmemo_waits_for_invoice_and_rejects_adjustments(self):
        from odoo.addons.queue_job.exception import RetryableJobError
        inbox = self._accounting_setup()
        doc, data = self._accounting_document(inbox, kind='creditmemo', invoice_id=999)
        client = Mock(); client.request.return_value = data
        with self._mock_api(client):
            with self.assertRaisesRegex(RetryableJobError, 'original Magento invoice'):
                doc._job_import_document()
            data['adjustment_positive'] = 3
            with self.assertRaisesRegex(UserError, 'adjustments'):
                doc._job_import_document()

    def test_accounting_unlinked_local_invoice_blocks_duplicate(self):
        inbox = self._accounting_setup()
        values = inbox.sale_order_id._prepare_invoice()
        values['invoice_line_ids'] = [Command.create(inbox.sale_order_id.order_line[0]._prepare_invoice_line(quantity=1))]
        self.env['account.move'].create(values)
        doc, data = self._accounting_document(inbox)
        client = Mock(); client.request.return_value = data
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'unlinked Odoo'):
            doc._job_import_document()
        self.assertFalse(doc.move_id)

    def test_prepaid_order_import_requires_invoice_opt_in(self):
        payload = deepcopy(self.fixture)
        payload['items'][0]['qty_invoiced'] = 2
        inbox = self._inbox(payload)
        with self.assertRaisesRegex(UserError, 'Previously processed'):
            inbox._job_import()
        self.instance.import_invoices = True
        inbox._job_import()
        self.assertTrue(inbox.sale_order_id)
        payload['entity_id'] = 1002
        payload['items'][0]['qty_shipped'] = 1
        with self.assertRaisesRegex(UserError, 'Previously processed'):
            self._inbox(payload)._job_import()

    def test_payment_status_is_informational_and_only_logs_changes(self):
        inbox = self._accounting_setup()
        remote = dict(self.fixture, total_paid=114, total_refunded=54, total_due=0)
        client = Mock(); client.request.return_value = remote
        payments = self.env['account.payment'].search_count([])
        with self._mock_api(client), trap_jobs():
            inbox._job_sync_financials()
            inbox._job_sync_financials()
        self.assertEqual(inbox.magento_paid, 114)
        self.assertEqual(inbox.magento_refunded, 54)
        self.assertEqual(self.env['account.payment'].search_count([]), payments)
        self.assertEqual(len(self.instance.sync_log_ids.filtered(lambda l: l.operation == 'payment')), 1)

    def test_financial_poll_scope_duplicate_and_disabled_configuration(self):
        inbox = self._accounting_setup()
        client = Mock(); client.request.return_value = {'items': [{'entity_id': 901, 'order_id': 1001, 'increment_id': 'INV901'}]}
        with self._mock_api(client), trap_jobs():
            inbox._job_financial_page('invoice')
            inbox._job_financial_page('invoice')
            self.assertEqual(len(inbox.accounting_ids), 1)
            client.request.return_value['items'][0]['order_id'] = 999
            with self.assertRaisesRegex(UserError, 'different order'):
                inbox._job_financial_page('invoice')
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(0, only=inbox._job_sync_financials)
        self.instance.auto_import_financials = True
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(1, only=inbox._job_sync_financials)

    def test_refund_return_is_explicit_idempotent_and_does_not_change_stock(self):
        inbox, picking, shipment = self._fulfillment_fixture()
        self._accounting_setup(inbox)
        invoice, invoice_data = self._accounting_document(inbox)
        doc, data = self._accounting_document(inbox, kind='creditmemo', external_id=801, qty=1, shipping=0)
        client = Mock(); client.request.side_effect = [invoice_data, data]
        with self._mock_api(client):
            invoice._job_import_document()
            doc._job_import_document()
        before = self.product.with_context(location=self.warehouse.lot_stock_id.id).qty_available
        doc.action_prepare_return()
        returns = doc.return_picking_ids
        doc.action_prepare_return()
        self.assertEqual(doc.return_picking_ids, returns)
        self.assertEqual(len(returns), 1)
        self.assertNotEqual(returns.state, 'done')
        self.assertEqual(returns.move_ids.product_uom_qty, 1)
        self.assertEqual(returns.move_ids.origin_returned_move_id, picking.move_ids)
        self.assertEqual(self.product.with_context(location=self.warehouse.lot_stock_id.id).qty_available, before)

    def test_refund_return_rejects_undelivered_goods(self):
        inbox = self._accounting_setup()
        invoice, invoice_data = self._accounting_document(inbox)
        doc, data = self._accounting_document(inbox, kind='creditmemo', external_id=801, qty=1, shipping=0)
        client = Mock(); client.request.side_effect = [invoice_data, data]
        with self._mock_api(client):
            invoice._job_import_document()
            doc._job_import_document()
        with self.assertRaisesRegex(UserError, 'not been delivered'):
            doc.action_prepare_return()
        self.assertFalse(doc.return_picking_ids)

    def test_tracking_update_preserves_other_rows_and_skips_unchanged(self):
        inbox, picking, shipment = self._fulfillment_fixture()
        previous = {'track_number': 'OLD', 'title': 'Odoo delivery', 'carrier_code': 'custom'}
        shipment.sudo().write({'external_id': '301', 'tracking_snapshot': previous})
        picking.in_magento_tracking_number = 'NEW'
        owned = dict(previous, entity_id=401, parent_id=301, order_id=1001)
        other = dict(previous, entity_id=402, track_number='MANUAL', parent_id=301, order_id=1001)
        client = Mock(); client.request.side_effect = [{'entity_id': 301, 'order_id': 1001, 'tracks': [owned, other]}, dict(owned, track_number='NEW')]
        with self._mock_api(client):
            shipment._job_export_tracking()
            shipment._job_export_tracking()
        self.assertEqual(client.request.call_count, 2)
        self.assertEqual(client.request.call_args.kwargs['payload']['entity']['entity_id'], 401)
        self.assertEqual(shipment.tracking_external_id, '401')
        self.assertEqual(len(self.instance.sync_log_ids.filtered(lambda l: l.operation == 'tracking')), 1)

    def test_tracking_create_unknown_response_recovered_without_duplicate(self):
        inbox, picking, shipment = self._fulfillment_fixture()
        shipment.sudo().external_id = '301'
        picking.in_magento_tracking_number = 'NEW'
        track = dict(shipment._tracking_values(), entity_id=401, parent_id=301, order_id=1001)
        client = Mock(); client.request.return_value = {'entity_id': 301, 'order_id': 1001, 'tracks': [track]}
        with self._mock_api(client):
            shipment._job_export_tracking()
        self.assertEqual(client.request.call_count, 1)
        self.assertEqual(shipment.tracking_external_id, '401')

    def test_tracking_remote_conflict_blocks_overwrite_and_clear_only_owned(self):
        inbox, picking, shipment = self._fulfillment_fixture()
        previous = {'track_number': 'OLD', 'title': 'Odoo delivery', 'carrier_code': 'custom'}
        shipment.sudo().write({'external_id': '301', 'tracking_external_id': '401', 'tracking_snapshot': previous})
        picking.in_magento_tracking_number = 'NEW'
        track = dict(previous, entity_id=401, parent_id=301, order_id=1001, track_number='REMOTE')
        client = Mock(); client.request.return_value = {'entity_id': 301, 'order_id': 1001, 'tracks': [track]}
        with self._mock_api(client), self.assertRaisesRegex(UserError, 'changed in Magento'):
            shipment._job_export_tracking()
        self.assertEqual(client.request.call_count, 1)
        track['track_number'] = 'OLD'
        picking.in_magento_tracking_number = False
        client.reset_mock()
        client.request.side_effect = [client.request.return_value, True]
        with self._mock_api(client):
            shipment._job_export_tracking()
        self.assertEqual(client.request.call_args.args, ('DELETE', 'shipment/track/401'))
        self.assertFalse(shipment.tracking_external_id)

    def test_accounting_actions_require_manager_and_company_scope(self):
        inbox = self._accounting_setup()
        doc, data = self._accounting_document(inbox)
        viewer = new_test_user(self.env, login='magento_accounting_viewer', groups='base.group_user,in_odoo_magento_connector.group_magento_user')
        with self.assertRaises(AccessError):
            doc.with_user(viewer).action_import()
        with self.assertRaises(AccessError):
            doc.with_user(viewer).write({'external_id': '999'})
        self.assertEqual(self.instance.action_view_accounting()['domain'], [('instance_id', '=', self.instance.id)])

    def test_complete_paid_virtual_order_import(self):
        self.instance.import_invoices = True
        self.instance.auto_confirm_orders = True
        payload = deepcopy(self.fixture)
        payload['state'] = 'complete'
        payload['items'][0].update(product_type='virtual', qty_invoiced=2)
        inbox = self._inbox(payload)
        inbox._job_import()
        self.assertEqual(inbox.sale_order_id.state, 'sale')
        payload['entity_id'] = 1002
        payload['items'][0]['product_type'] = 'simple'
        with self.assertRaisesRegex(UserError, 'new/unfulfilled'):
            self._inbox(payload)._job_import()

    def test_offline_creditmemo_cannot_exceed_partially_invoiced_quantity(self):
        inbox = self._accounting_setup()
        invoice, invoice_data = self._accounting_document(inbox, qty=1, shipping=0)
        refund, data = self._accounting_document(inbox, kind='creditmemo', external_id=801, qty=2, shipping=0)
        client = Mock(); client.request.side_effect = [invoice_data, data]
        with self._mock_api(client):
            invoice._job_import_document()
            with self.assertRaisesRegex(UserError, 'exceeds the imported invoices'):
                refund._job_import_document()
        self.assertFalse(refund.move_id)

    def test_accounting_rejects_wrong_order_currency_and_freight_overbilling(self):
        inbox = self._accounting_setup()
        first, data = self._accounting_document(inbox, qty=1)
        client = Mock(); client.request.return_value = data
        with self._mock_api(client):
            data['order_id'] = 999
            with self.assertRaisesRegex(UserError, 'identity changed'):
                first._job_import_document()
            data['order_id'] = 1001
            data['order_currency_code'] = 'USD'
            with self.assertRaisesRegex(UserError, 'currency differs'):
                first._job_import_document()
            data['order_currency_code'] = 'EUR'
            first._job_import_document()
            second, data2 = self._accounting_document(inbox, external_id=902, qty=1)
            client.request.return_value = data2
            with self.assertRaisesRegex(UserError, 'freight exceeds'):
                second._job_import_document()
        self.assertFalse(second.move_id)

    def test_financials_and_tracking_respect_neutralized_database(self):
        inbox = self._accounting_setup()
        doc, data = self._accounting_document(inbox)
        self.instance.write({'auto_import_financials': True, 'auto_export_tracking': True})
        self.env['ir.config_parameter'].sudo().set_param('database.is_neutralized', 'true')
        with trap_jobs() as trap:
            self.env['in.magento.instance']._cron_schedule()
            trap.assert_jobs_count(0)
        with self.assertRaisesRegex(UserError, 'neutralized'):
            doc._job_import_document()

    def test_return_quantity_spans_deliveries_and_never_duplicates(self):
        inbox, first, shipment = self._fulfillment_fixture(quantity=1)
        backorder = inbox.sale_order_id.picking_ids.filtered(lambda p: p.backorder_id == first)
        backorder.action_assign()
        backorder.move_ids.quantity = 1
        backorder.move_ids.picked = True
        backorder._action_done()
        self._accounting_setup(inbox)
        invoice, invoice_data = self._accounting_document(inbox)
        refund, credit = self._accounting_document(inbox, kind='creditmemo', external_id=801, qty=2, shipping=5)
        client = Mock(); client.request.side_effect = [invoice_data, credit]
        with self._mock_api(client):
            invoice._job_import_document()
            refund._job_import_document()
        refund.action_prepare_return()
        refund.action_prepare_return()
        self.assertEqual(len(refund.return_picking_ids), 2)
        self.assertEqual(sum(refund.return_picking_ids.move_ids.mapped('product_uom_qty')), 2)
        self.assertFalse(refund.return_picking_ids.filtered(lambda p: p.state == 'done'))

    def _publication_wizard(self, product=None, operation='unpublish', instance=None):
        product = product or self.product
        action = (product.action_unpublish_from_magento() if operation == 'unpublish'
                  else product.action_republish_to_magento())
        return self.env[action['res_model']].with_context(action['context']).create({
            'instance_id': (instance or self.instance).id})

    def _publication_client(self, rows):
        client = Mock()
        def request(method, path, **kwargs):
            sku = path.removeprefix('products/')
            if method == 'PUT':
                self.assertEqual(set(kwargs['payload']['product']), {'id', 'sku', 'status'})
                rows[sku].update(kwargs['payload']['product'])
            return deepcopy(rows[sku])
        client.request.side_effect = request
        return client

    def test_unpublish_disables_remote_and_preserves_mapping(self):
        self.product_binding.external_id = '42'
        client = self._publication_client({'SIM-001': {'id': 42, 'sku': 'SIM-001', 'type_id': 'simple', 'status': 1, 'price': 50}})
        with self._mock_api(client), trap_jobs() as jobs:
            self._publication_wizard().action_apply()
            self.assertEqual(self.product_binding.publication_state, 'unpublish_pending')
            self.assertEqual(self.product.magento_publication_state, 'pending')
            jobs.perform_enqueued_jobs()
        self.assertEqual(self.product_binding.publication_state, 'unpublished')
        self.assertEqual(self.product.magento_publication_state, 'paused')
        self.assertEqual(self.product_binding.external_id, '42')
        self.assertTrue(self.product.active)
        log = self.env['in.magento.sync.log'].search([('product_binding_id', '=', self.product_binding.id)], limit=1)
        self.assertIn('disabled', log.summary)
        self.assertEqual(log.change_ids.filtered(lambda c: c.field_name == 'status').new_value, '2')
        before_calls = client.request.call_count
        with self._mock_api(client):
            self.product_binding._job_set_publication()
        self.assertEqual(client.request.call_count, before_calls)

    def test_unpublish_stale_product_price_image_stock_jobs_do_no_remote_work(self):
        self.product_binding.write({'external_id': '42', 'publication_state': 'unpublish_pending', 'sync_inventory': True})
        client = Mock()
        with self._mock_api(client), trap_jobs() as jobs:
            for method in ('_job_export', '_job_export_if_changed', '_job_export_image',
                           '_job_export_inventory', '_job_export_inventory_batch', '_job_export_prices'):
                getattr(self.product_binding, method)()
            jobs.assert_jobs_count(0)
        client.request.assert_not_called()
        with self.assertRaises(UserError):
            self.product_binding.action_export()
        with self.assertRaises(UserError):
            self.product_binding.action_export_inventory()

    def test_unpublish_scheduler_filters_paused_products_before_queueing(self):
        self.product_binding.write({'external_id': '42', 'publication_state': 'unpublished', 'sync_inventory': True})
        self.instance.write({'auto_export_products': True, 'auto_export_inventory': True})
        with trap_jobs() as jobs:
            self.env['in.magento.instance']._cron_schedule()
            jobs.assert_jobs_count(0)

    def test_unpublish_bulk_price_filters_only_paused_rows(self):
        self.product_binding.write({'external_id': '42', 'publication_state': 'unpublished'})
        product = self.env['product.product'].create({'name': 'Active publication product', 'default_code': 'ACTIVE'})
        active = self.product_binding.copy({'product_id': product.id, 'sku': 'ACTIVE', 'external_id': '43'})
        seen = []
        def unchanged(record):
            seen.append(record.id)
            return False
        with patch.object(type(active), '_catalog_change_kind', unchanged), self._mock_api(Mock()):
            (active | self.product_binding)._job_export_prices()
        self.assertEqual(seen, [active.id])

    def test_unpublish_family_includes_parent_and_all_variants(self):
        template, mapping, wizard = self._variant_fixture()
        with trap_jobs():
            wizard.action_publish()
        parent = self.env['in.magento.template.binding'].search([('template_id', '=', template.id)])
        parent.external_id = '100'
        rows = {parent.sku: {'id': 100, 'sku': parent.sku, 'type_id': 'configurable', 'status': 1}}
        for index, child in enumerate(parent.child_ids, 101):
            child.external_id = str(index)
            rows[child.sku] = {'id': index, 'sku': child.sku, 'type_id': 'simple', 'status': 1}
        client = self._publication_client(rows)
        with self._mock_api(client), trap_jobs() as jobs:
            self._publication_wizard(template.product_variant_ids[:1]).action_apply()
            jobs.assert_jobs_count(3)
            jobs.perform_enqueued_jobs()
        self.assertEqual(parent.publication_state, 'unpublished')
        self.assertEqual(set(parent.child_ids.mapped('publication_state')), {'unpublished'})
        self.assertTrue(all(row['status'] == 2 for row in rows.values()))
        client.reset_mock()
        with self._mock_api(client), trap_jobs() as jobs:
            for method in ('_job_export', '_job_export_if_changed', '_job_export_image', '_job_link_variants'):
                getattr(parent, method)()
            jobs.assert_jobs_count(0)
        client.request.assert_not_called()

    def test_unpublish_remote_conflict_and_unacknowledged_status_stay_pending(self):
        self.product_binding.write({'external_id': '42', 'publication_state': 'unpublish_pending'})
        client = Mock()
        client.request.return_value = {'id': 999, 'sku': 'SIM-001', 'type_id': 'simple', 'status': 1}
        with self._mock_api(client), self.assertRaises(UserError):
            self.product_binding._job_set_publication()
        self.assertEqual(client.request.call_count, 1)
        client.request.return_value = {'id': 42, 'sku': 'SIM-001', 'type_id': 'simple', 'status': 1}
        with self._mock_api(client), self.assertRaises(UserError):
            self.product_binding._job_set_publication()
        self.assertEqual(self.product_binding.publication_state, 'unpublish_pending')
        self.assertFalse(self.env['in.magento.sync.log'].search([('product_binding_id', '=', self.product_binding.id)]))

    def test_unpublish_unknown_response_retry_reads_disabled_status(self):
        self.product_binding.write({'external_id': '42', 'publication_state': 'unpublish_pending'})
        client = self._publication_client({'SIM-001': {'id': 42, 'sku': 'SIM-001', 'type_id': 'simple', 'status': 2}})
        with self._mock_api(client):
            self.product_binding._job_set_publication()
        self.assertEqual([call.args[0] for call in client.request.call_args_list], ['GET', 'GET'])
        self.assertEqual(self.product_binding.publication_state, 'unpublished')

    def test_republish_enables_listing_and_queues_current_product(self):
        self.product_binding.write({'external_id': '42', 'publication_state': 'unpublished'})
        client = self._publication_client({'SIM-001': {'id': 42, 'sku': 'SIM-001', 'type_id': 'simple', 'status': 2}})
        with trap_jobs():
            self._publication_wizard(operation='republish').action_apply()
        with self._mock_api(client), trap_jobs() as jobs:
            self.product_binding._job_set_publication()
            jobs.assert_jobs_count(1, only=self.product_binding._job_export)
        self.assertEqual(self.product_binding.publication_state, 'managed')
        self.assertEqual(self.product.magento_publication_state, 'managed')
        self.assertFalse(self.product_binding.last_fingerprint)

    def test_unpublish_before_initial_creation_stops_job_and_can_republish(self):
        client = Mock()
        with self._mock_api(client), trap_jobs() as jobs:
            self._publication_wizard().action_apply()
            jobs.perform_enqueued_jobs()
            self.product_binding._job_export()
        client.request.assert_not_called()
        self.assertEqual(self.product_binding.publication_state, 'unpublished')
        with trap_jobs():
            self._publication_wizard(operation='republish').action_apply()
            self.product_binding._job_set_publication()
        self.assertEqual(self.product_binding._payload()['product']['status'], 1)

    def test_publication_connection_selection_and_mixed_status(self):
        self.product_binding.external_id = '42'
        other = self.instance.copy({'name': 'Other Magento'})
        other_binding = self.product_binding.copy({'instance_id': other.id, 'external_id': '77'})
        action = self.product.action_unpublish_from_magento()
        self.assertFalse(action['context']['default_instance_id'])
        with trap_jobs():
            self._publication_wizard(instance=other).action_apply()
        self.assertEqual(self.product_binding.publication_state, 'managed')
        self.assertEqual(other_binding.publication_state, 'unpublish_pending')
        other_binding.publication_state = 'unpublished'
        self.assertEqual(self.product.magento_publication_state, 'mixed')
        self.instance.enabled = False
        with self.assertRaises(UserError):
            self._publication_wizard().action_apply()

    def test_publication_rejects_viewer_and_inactive_republish(self):
        viewer = new_test_user(self.env, login='publication_viewer',
            groups='base.group_user,in_odoo_magento_connector.group_magento_user')
        with self.assertRaises(AccessError):
            self.product.with_user(viewer).action_unpublish_from_magento()
        with self.assertRaises(AccessError):
            self.env['in.magento.product.publication'].with_user(viewer).create({'operation': 'unpublish'})
        self.product.sale_ok = False
        self.product_binding.publication_state = 'unpublished'
        with self.assertRaises(UserError):
            self._publication_wizard(operation='republish').action_apply()

    def test_publish_does_not_silently_resume_unpublished_mappings(self):
        self.product_binding.publication_state = 'unpublished'
        action = self.product.action_publish_to_magento()
        wizard = self.env[action['res_model']].with_context(action['context']).create({})
        with self.assertRaises(UserError):
            wizard.action_publish()

    def test_publication_controls_on_template_and_variant_forms(self):
        for model, xmlid in [('product.template', 'product.product_template_only_form_view'),
                             ('product.product', 'product.product_normal_form_view')]:
            arch = self.env[model].get_view(self.env.ref(xmlid).id, 'form')['arch']
            self.assertIn('action_unpublish_from_magento', arch)
            self.assertIn('action_republish_to_magento', arch)
            self.assertIn('magento_publication_state', arch)

    def test_unpublish_bulk_inventory_filters_only_paused_rows(self):
        self.product_binding.write({'external_id': '42', 'publication_state': 'unpublished', 'sync_inventory': True})
        product = self.env['product.product'].create({'name': 'Active stock product', 'is_storable': True})
        active = self.env['in.magento.product.binding'].create({'instance_id': self.instance.id,
            'product_id': product.id, 'sku': 'ACTIVE-STOCK', 'external_id': '43', 'sync_inventory': True})
        seen = []
        def blocked(records):
            seen.extend(records.ids)
            return records
        client = Mock()
        with patch.object(type(active), '_inventory_blocked_bindings', blocked), self._mock_api(client), trap_jobs() as jobs:
            (active | self.product_binding)._job_export_inventory_batch()
            jobs.assert_jobs_count(1, only=active._job_export_inventory)
        self.assertEqual(seen, [active.id])
        client.request.assert_not_called()

    def test_republish_family_waits_for_all_members_before_export(self):
        template, mapping, wizard = self._variant_fixture()
        with trap_jobs():
            wizard.action_publish()
        parent = self.env['in.magento.template.binding'].search([('template_id', '=', template.id)])
        parent.write({'external_id': '100', 'publication_state': 'unpublished'})
        rows = {parent.sku: {'id': 100, 'sku': parent.sku, 'type_id': 'configurable', 'status': 2}}
        for index, child in enumerate(parent.child_ids, 101):
            child.write({'external_id': str(index), 'publication_state': 'unpublished'})
            rows[child.sku] = {'id': index, 'sku': child.sku, 'type_id': 'simple', 'status': 2}
        with trap_jobs():
            self._publication_wizard(template.product_variant_ids[:1], operation='republish').action_apply()
        with self._mock_api(self._publication_client(rows)), trap_jobs() as jobs:
            parent._job_set_publication()
            jobs.assert_jobs_count(0)
            parent.child_ids[0]._job_set_publication()
            jobs.assert_jobs_count(0)
            parent.child_ids[1]._job_set_publication()
            jobs.assert_jobs_count(1, only=parent._job_export)
        self.assertEqual(template.magento_publication_state, 'managed')

    def test_publication_job_uses_latest_intent_and_keeps_company_access(self):
        self.product_binding.external_id = '42'
        with trap_jobs():
            self._publication_wizard().action_apply()
            self._publication_wizard(operation='republish').action_apply()
        rows = {'SIM-001': {'id': 42, 'sku': 'SIM-001', 'type_id': 'simple', 'status': 1}}
        with self._mock_api(self._publication_client(rows)), trap_jobs():
            self.product_binding._job_set_publication()
        self.assertEqual(self.product_binding.publication_state, 'managed')
        self.assertEqual(rows['SIM-001']['status'], 1)
        other = self.env['res.company'].create({'name': 'Publication other company'})
        manager = new_test_user(self.env, login='publication_other_manager', company_id=other.id,
            company_ids=[Command.set(other.ids)],
            groups='base.group_user,in_odoo_magento_connector.group_magento_manager')
        wizard = self._publication_wizard()
        with self.assertRaises(AccessError):
            wizard.with_user(manager).with_context(allowed_company_ids=other.ids).action_apply()
