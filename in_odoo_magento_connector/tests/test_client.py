from unittest.mock import Mock
from urllib.parse import parse_qs

from odoo.tests.common import BaseCase, tagged

import requests

from ..services.client import MagentoClient, MagentoError, MagentoRetryableError, search_params, validate_url


@tagged("post_install", "-at_install")
class TestMagentoClient(BaseCase):
    def setUp(self):
        super().setUp()
        self.client = MagentoClient("https://shop.example.test", auth_type="bearer", token="private-token")
        self.client.session.close()
        self.client.session = Mock()

    def test_https_required_including_loopback(self):
        for url in ("http://shop.example.test", "http://127.0.0.1:9024", "http://localhost",
                    "http://[::1]", "https://user:secret@shop.example.test",
                    "https://shop.example.test?token=x"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_url(url)
        validate_url("https://localhost:8443")

    def test_redirect_not_followed(self):
        self.client.session.request.return_value = Mock(status_code=302)
        with self.assertRaises(MagentoError):
            self.client.request("GET", "orders")
        self.assertFalse(self.client.session.request.call_args.kwargs["allow_redirects"])

    def test_rate_limit_honors_retry_after(self):
        self.client.session.request.return_value = Mock(status_code=429, headers={"Retry-After": "125"})
        with self.assertRaises(MagentoRetryableError) as error:
            self.client.request("GET", "orders")
        self.assertEqual(error.exception.seconds, 125)

    def test_lost_write_response_does_not_blindly_retry(self):
        self.client.session.request.side_effect = requests.Timeout("private-token")
        with self.assertRaises(MagentoError) as error:
            self.client.request("POST", "categories", payload={})
        self.assertNotIsInstance(error.exception, MagentoRetryableError)
        self.assertNotIn("private-token", str(error.exception))
        with self.assertRaises(MagentoRetryableError):
            self.client.request("PUT", "products/SKU", payload={})

    def test_authentication_failure_does_not_expose_response(self):
        response = Mock(status_code=401, text="private-token customer@example.test")
        self.client.session.request.return_value = response
        with self.assertRaises(MagentoError) as error:
            self.client.request("GET", "orders")
        self.assertEqual(error.exception.status, 401)
        self.assertNotIn("private-token", str(error.exception))

    def test_invalid_json_is_actionable(self):
        self.client.session.request.return_value = Mock(status_code=200, json=Mock(side_effect=ValueError()))
        with self.assertRaisesRegex(MagentoError, "invalid JSON"):
            self.client.request("GET", "orders")

    def test_filters_are_anded(self):
        params = search_params([("store_id", "in", "1,2"), ("entity_id", "gt", 100)])
        self.assertEqual(params["searchCriteria[filter_groups][0][filters][0][value]"], "1,2")
        self.assertEqual(params["searchCriteria[filter_groups][1][filters][0][value]"], 100)

    def test_order_date_query_uses_oauth_compatible_space_encoding(self):
        self.client.session.request.return_value = Mock(status_code=200, json=Mock(return_value={"items": []}))
        params = search_params([("created_at", "gteq", "2026-10-14 00:00:00")])
        self.client.request("GET", "orders", params=params)
        query = self.client.session.request.call_args.kwargs["params"]
        self.assertIn("2026-10-14%2000%3A00%3A00", query)
        self.assertNotIn("+", query)
        self.assertEqual(parse_qs(query)["searchCriteria[filter_groups][0][filters][0][value]"], ["2026-10-14 00:00:00"])

    def test_put_adding_a_customer_address_is_not_replay_safe(self):
        self.client.session.request.side_effect = requests.Timeout('private details')
        with self.assertRaises(MagentoError) as error:
            self.client.request('PUT', 'customers/10', payload={}, replay_safe=False)
        self.assertNotIsInstance(error.exception, MagentoRetryableError)
        self.assertNotIn('private details', str(error.exception))
