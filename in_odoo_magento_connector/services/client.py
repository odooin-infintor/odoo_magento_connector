"""Magento REST transport. Never log credentials or remote response bodies.

Retries belong to the persistent job queue, not a hidden HTTP retry loop.
Uncertain non-idempotent writes require reconciliation before another POST.
"""
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import quote, urlencode, urlsplit

import requests
from requests_oauthlib import OAuth1


class MagentoError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


class MagentoRetryableError(MagentoError):
    def __init__(self, message, seconds=60, status=None):
        super().__init__(message, status)
        self.seconds = seconds


def validate_url(url):
    parts = urlsplit(url)
    if not parts.hostname or parts.username or parts.password or parts.query or parts.fragment:
        raise ValueError("Use a store base URL without credentials, query or fragment.")
    if parts.scheme != "https":
        raise ValueError("HTTPS is required for Magento connections.")
    if "/rest/" in parts.path or parts.path.rstrip("/").endswith("/rest"):
        raise ValueError("Enter the Magento base URL, without /rest or an API endpoint.")


def search_params(filters=(), page=1, size=100, sort="entity_id"):
    result = {
        "searchCriteria[currentPage]": page,
        "searchCriteria[pageSize]": size,
        "searchCriteria[sortOrders][0][field]": sort,
        "searchCriteria[sortOrders][0][direction]": "ASC",
    }
    # Separate groups are ANDed. Multiple filters in one group would be ORed.
    for index, (field, condition, value) in enumerate(filters):
        prefix = f"searchCriteria[filter_groups][{index}][filters][0]"
        result.update({f"{prefix}[field]": field, f"{prefix}[condition_type]": condition,
                       f"{prefix}[value]": value})
    return result


class MagentoClient:
    def __init__(self, base_url, store_code="all", auth_type="oauth1", token="",
                 consumer_key="", consumer_secret="", token_secret=""):
        validate_url(base_url)
        self.base_url = f"{base_url.rstrip('/')}/rest/{quote(store_code, safe='')}/V1/"
        self.session = requests.Session()
        self.session.headers.update({"Accept": "application/json", "Content-Type": "application/json"})
        if auth_type == "oauth1":
            self.session.auth = OAuth1(consumer_key, consumer_secret, token, token_secret,
                                      signature_method="HMAC-SHA256")
        else:
            self.session.headers["Authorization"] = f"Bearer {token}"

    def close(self):
        self.session.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    def request(self, method, path, *, params=None, payload=None, replay_safe=None):
        if path.startswith(("/", "http:" , "https:")) or ".." in path.split("/"):
            raise ValueError("API paths must be relative to V1.")
        safe = method == "GET" or (method == "PUT" and replay_safe is not False) or bool(replay_safe)
        # Magento's OAuth verifier treats a '+' in query values differently
        # from a space. Use RFC 3986 encoding before signing (not form-style
        # encoding), notably for created_at filters containing a date/time.
        query = urlencode(params, doseq=True, quote_via=quote) if params and not isinstance(params, str) else params
        try:
            response = self.session.request(
                method, self.base_url + path, params=query, json=payload,
                timeout=(10, 60), allow_redirects=False,
            )
        except requests.exceptions.SSLError:
            raise MagentoError("Magento TLS certificate verification failed.") from None
        except (requests.Timeout, requests.ConnectionError):
            if safe:
                raise MagentoRetryableError("Magento connection failed; the job will retry.") from None
            raise MagentoError("Magento write outcome is unknown. Reconcile the remote record before retrying.") from None
        status = response.status_code
        if status in (401, 403):
            raise MagentoError("Magento authentication or API permission failed. Check connection credentials and ACLs.", status)
        if status == 429 or status >= 500:
            if safe:
                raise MagentoRetryableError(f"Magento returned HTTP {status}; retry scheduled.",
                                            self._retry_after(response.headers.get("Retry-After")), status)
            raise MagentoError(f"Magento returned HTTP {status} during a write. Reconcile before retrying.", status)
        if not 200 <= status < 300:
            raise MagentoError(f"Magento returned HTTP {status}. Check the record mapping and Magento logs.", status)
        if status == 204:
            return None
        try:
            return response.json()
        except ValueError:
            raise MagentoError("Magento returned invalid JSON. Check its proxy and API configuration.", status) from None

    @staticmethod
    def _retry_after(value):
        try:
            seconds = int(value)
        except (ValueError, TypeError):
            try:
                seconds = int((parsedate_to_datetime(value) - datetime.now(timezone.utc)).total_seconds())
            except (ValueError, TypeError, OverflowError):
                seconds = 60
        return max(1, min(seconds, 3600))
