from __future__ import annotations

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock


# Reuse the repository's existing fail-closed import harness. Importing it first
# guarantees that app.main never sees the developer environment, production DB,
# or a usable network socket even when this file is run on its own.
_TESTS_DIR = str(Path(__file__).resolve().parent)
if _TESTS_DIR not in sys.path:
    sys.path.insert(0, _TESTS_DIR)

import test_regression_foundation as foundation  # noqa: E402
from app import asset_proxy_policy as policy  # noqa: E402


httpx = foundation.httpx
main = foundation.main
HTTPException = foundation.HTTPException

# Creating a Windows event loop uses a local socketpair. Temporarily restore the
# real socket class for that local primitive, then immediately return to the
# fail-closed network guard inherited during collection.
with mock.patch.object(foundation.socket, "socket", foundation._REAL_SOCKET):
    _TEST_EVENT_LOOP = asyncio.new_event_loop()
_DYNAMIC_ENV_OVERRIDES: dict[str, str] = {}


def _safe_getenv(name: str, default=None):
    return _DYNAMIC_ENV_OVERRIDES.get(name, default)


# These guards are started by the route-test class, after any earlier test
# module has completed, so their lifetime never depends on test file order.
_ASSET_ENV_PATCHER = mock.patch.object(foundation.os, "getenv", side_effect=_safe_getenv)
_ASSET_SQLITE_PATCHER = mock.patch.object(
    foundation.db.sqlite3,
    "connect",
    side_effect=foundation._guarded_sqlite_connect,
)
_ASSET_NETWORK_PATCHERS = (
    mock.patch.object(foundation.socket, "socket", foundation._NoNetworkSocket),
    mock.patch.object(foundation.socket, "create_connection", foundation._deny_network),
)
def tearDownModule() -> None:
    _TEST_EVENT_LOOP.close()


class _FakeAsyncClient:
    responses: list[object] = []
    requests: list[tuple[str, dict[str, str]]] = []
    init_kwargs: list[dict[str, object]] = []

    def __init__(self, *_args, **kwargs):
        self.init_kwargs.append(dict(kwargs))

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    async def get(self, url: str, *, headers: dict[str, str]):
        self.requests.append((url, dict(headers)))
        if not self.responses:
            raise AssertionError("Unexpected asset proxy request")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            raise item
        status_code, response_headers, content = item
        return httpx.Response(
            status_code,
            headers=response_headers,
            content=content,
            request=httpx.Request("GET", url),
        )


class AssetProxyPolicyTests(unittest.TestCase):
    def test_default_hosts_use_exact_or_dot_subdomain_matching(self) -> None:
        allowed = policy.parse_allowed_asset_hosts(None)
        self.assertEqual(policy.DEFAULT_ASSET_PROXY_ALLOWED_HOSTS, allowed)
        for host in allowed:
            with self.subTest(host=host):
                self.assertTrue(policy.asset_url_allowed(f"https://{host}/image.jpg", allowed))
                self.assertTrue(policy.asset_url_allowed(f"https://media.{host}/image.jpg", allowed))

        self.assertTrue(policy.asset_url_allowed("https://MEDIA.OZON.RU.:443/image.jpg", allowed))

    def test_lookalikes_and_invalid_origins_are_rejected(self) -> None:
        allowed = policy.parse_allowed_asset_hosts(None)
        rejected = (
            "https://evil-ozon.ru/image.jpg",
            "https://ozon.ru.attacker.example/image.jpg",
            "https://api-seller.ozon.ru.attacker.example/image.jpg",
            "https://attacker-ozon.ru/image.jpg",
            "https://prefixozon.rusuffix.example/image.jpg",
            "https:///missing-host.jpg",
            "https://user:password@ozon.ru/image.jpg",
            "ftp://ozon.ru/image.jpg",
            "http://ozon.ru/image.jpg",
            "https://localhost/image.jpg",
            "https://127.0.0.1/image.jpg",
            "https://10.1.2.3/image.jpg",
            "https://169.254.1.2/image.jpg",
            "https://[::1]/image.jpg",
            "https://ozon.ru:8443/image.jpg",
        )
        for url in rejected:
            with self.subTest(url=url):
                self.assertFalse(policy.asset_url_allowed(url, allowed))

    def test_configured_hosts_are_normalized_without_substring_matching(self) -> None:
        allowed = policy.parse_allowed_asset_hosts(" Example.COM. ,cdn.example.com,example.com")
        self.assertEqual(("example.com", "cdn.example.com"), allowed)
        self.assertTrue(policy.asset_url_allowed("https://IMG.EXAMPLE.COM.:443/a", allowed))
        self.assertFalse(policy.asset_url_allowed("https://notexample.com/a", allowed))
        self.assertFalse(policy.asset_url_allowed("https://example.com.attacker.test/a", allowed))
        self.assertEqual((), policy.parse_allowed_asset_hosts(""))

    def test_credential_origin_is_exact(self) -> None:
        self.assertTrue(policy.asset_url_requires_ozon_credentials("https://api-seller.ozon.ru/file"))
        self.assertTrue(policy.asset_url_requires_ozon_credentials("https://API-SELLER.OZON.RU.:443/file"))
        for url in (
            "https://ozon.ru/file",
            "https://cdn.ngenix.net/file",
            "https://foo.api-seller.ozon.ru/file",
            "https://api-seller.ozon.ru.attacker.example/file",
            "https://api-seller.ozon.ru:8443/file",
            "http://api-seller.ozon.ru/file",
        ):
            with self.subTest(url=url):
                self.assertFalse(policy.asset_url_requires_ozon_credentials(url))


class AssetProxyRouteTests(unittest.TestCase):
    fake_connector = SimpleNamespace(client_id="test-client-id", api_key="test-api-key")

    @classmethod
    def setUpClass(cls) -> None:
        _ASSET_ENV_PATCHER.start()
        _ASSET_SQLITE_PATCHER.start()
        for patcher in _ASSET_NETWORK_PATCHERS:
            patcher.start()

    @classmethod
    def tearDownClass(cls) -> None:
        _ASSET_ENV_PATCHER.stop()
        _ASSET_SQLITE_PATCHER.stop()
        for patcher in reversed(_ASSET_NETWORK_PATCHERS):
            patcher.stop()

    def setUp(self) -> None:
        foundation._NETWORK_ATTEMPTS.clear()
        _FakeAsyncClient.responses = []
        _FakeAsyncClient.requests = []
        _FakeAsyncClient.init_kwargs = []

    def tearDown(self) -> None:
        self.assertEqual([], foundation._NETWORK_ATTEMPTS, "A test attempted real network access")

    def _run_proxy(self, url: str, responses: list[object]):
        _FakeAsyncClient.responses = list(responses)
        _FakeAsyncClient.requests = []
        _FakeAsyncClient.init_kwargs = []
        with (
            mock.patch.object(main.httpx, "AsyncClient", _FakeAsyncClient),
            mock.patch.dict(main.connectors, {"ozon": self.fake_connector}),
        ):
            return _TEST_EVENT_LOOP.run_until_complete(main.proxy_image(url))

    def test_headers_are_only_added_for_exact_credential_origin(self) -> None:
        with mock.patch.dict(main.connectors, {"ozon": self.fake_connector}):
            approved = main._asset_proxy_headers("https://api-seller.ozon.ru/image")
            cdn = main._asset_proxy_headers("https://cdn.ngenix.net/image")
            lookalike = main._asset_proxy_headers("https://api-seller.ozon.ru.attacker.example/image")
        self.assertEqual("test-client-id", approved.get("Client-Id"))
        self.assertEqual("test-api-key", approved.get("Api-Key"))
        self.assertNotIn("Client-Id", cdn)
        self.assertNotIn("Api-Key", cdn)
        self.assertNotIn("Client-Id", lookalike)
        self.assertNotIn("Api-Key", lookalike)

        incomplete_connectors = (
            SimpleNamespace(client_id="test-client-id", api_key=""),
            SimpleNamespace(client_id="", api_key="test-api-key"),
        )
        for connector in incomplete_connectors:
            with self.subTest(connector=connector):
                with mock.patch.dict(main.connectors, {"ozon": connector}):
                    incomplete = main._asset_proxy_headers("https://api-seller.ozon.ru/image")
                self.assertNotIn("Client-Id", incomplete)
                self.assertNotIn("Api-Key", incomplete)

    def test_direct_image_preserves_response_contract(self) -> None:
        response = self._run_proxy(
            "https://cdn.ngenix.net/image.jpg",
            [(200, {"content-type": "image/jpeg"}, b"image-bytes")],
        )
        self.assertEqual(b"image-bytes", response.body)
        self.assertEqual("image/jpeg", response.media_type)
        self.assertEqual("private, max-age=3600", response.headers["cache-control"])
        self.assertEqual(1, len(_FakeAsyncClient.requests))
        self.assertEqual(False, _FakeAsyncClient.init_kwargs[0]["follow_redirects"])

    def test_forbidden_redirect_is_blocked_before_second_request(self) -> None:
        forbidden_destinations = (
            "https://ozon.ru.attacker.example/secret",
            "https://attacker.example/secret",
            "http://cdn.ngenix.net/insecure",
            "https://127.0.0.1/private",
        )
        for destination in forbidden_destinations:
            with self.subTest(destination=destination):
                with self.assertRaises(HTTPException) as error:
                    self._run_proxy(
                        "https://cdn.ngenix.net/start",
                        [(302, {"location": destination}, b"")],
                    )
                self.assertEqual(502, error.exception.status_code)
                self.assertEqual(1, len(_FakeAsyncClient.requests))

    def test_redirect_recomputes_credentials_for_each_host(self) -> None:
        self._run_proxy(
            "https://api-seller.ozon.ru/start",
            [
                (302, {"location": "https://cdn.ngenix.net/image"}, b""),
                (200, {"content-type": "image/png"}, b"png"),
            ],
        )
        self.assertEqual("test-client-id", _FakeAsyncClient.requests[0][1].get("Client-Id"))
        self.assertNotIn("Client-Id", _FakeAsyncClient.requests[1][1])
        self.assertNotIn("Api-Key", _FakeAsyncClient.requests[1][1])

        self._run_proxy(
            "https://cdn.ngenix.net/start",
            [
                (302, {"location": "https://api-seller.ozon.ru/image"}, b""),
                (200, {"content-type": "image/png"}, b"png"),
            ],
        )
        self.assertNotIn("Client-Id", _FakeAsyncClient.requests[0][1])
        self.assertEqual("test-client-id", _FakeAsyncClient.requests[1][1].get("Client-Id"))

    def test_relative_redirect_and_three_hop_limit(self) -> None:
        response = self._run_proxy(
            "https://cdn.ngenix.net/start",
            [
                (302, {"location": "/one"}, b""),
                (301, {"location": "two"}, b""),
                (307, {"location": "/three"}, b""),
                (200, {"content-type": "image/webp"}, b"webp"),
            ],
        )
        self.assertEqual(b"webp", response.body)
        self.assertEqual(
            [
                "https://cdn.ngenix.net/start",
                "https://cdn.ngenix.net/one",
                "https://cdn.ngenix.net/two",
                "https://cdn.ngenix.net/three",
            ],
            [url for url, _headers in _FakeAsyncClient.requests],
        )

    def test_fourth_redirect_and_missing_location_are_rejected(self) -> None:
        with self.assertRaises(HTTPException) as limit_error:
            self._run_proxy(
                "https://cdn.ngenix.net/start",
                [
                    (302, {"location": "/one"}, b""),
                    (302, {"location": "/two"}, b""),
                    (302, {"location": "/three"}, b""),
                    (302, {"location": "/four"}, b""),
                ],
            )
        self.assertEqual(502, limit_error.exception.status_code)
        self.assertEqual(4, len(_FakeAsyncClient.requests))

        with self.assertRaises(HTTPException) as location_error:
            self._run_proxy("https://cdn.ngenix.net/start", [(302, {}, b"")])
        self.assertEqual(502, location_error.exception.status_code)
        self.assertEqual(1, len(_FakeAsyncClient.requests))

    def test_content_checks_and_upstream_errors_remain_sanitized(self) -> None:
        with self.assertRaises(HTTPException) as media_error:
            self._run_proxy(
                "https://cdn.ngenix.net/file",
                [(200, {"content-type": "text/html"}, b"not-an-image")],
            )
        self.assertEqual(415, media_error.exception.status_code)

        with mock.patch.dict(_DYNAMIC_ENV_OVERRIDES, {"IMAGE_PROXY_MAX_BYTES": "100000"}):
            with self.assertRaises(HTTPException) as size_error:
                self._run_proxy(
                    "https://cdn.ngenix.net/large",
                    [(200, {"content-type": "image/jpeg"}, b"x" * 100_001)],
                )
        self.assertEqual(413, size_error.exception.status_code)

        secret_text = "https://attacker.test/?token=do-not-expose"
        with self.assertRaises(HTTPException) as upstream_error:
            self._run_proxy("https://cdn.ngenix.net/fail", [RuntimeError(secret_text)])
        self.assertEqual(502, upstream_error.exception.status_code)
        self.assertNotIn(secret_text, str(upstream_error.exception.detail))
        self.assertEqual("Image preview request failed", upstream_error.exception.detail)

    def test_asset_route_remains_authentication_protected(self) -> None:
        async def request_without_session():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as client:
                return await client.get(
                    "/api/assets/image",
                    params={"url": "https://cdn.ngenix.net/image.jpg"},
                )

        response = _TEST_EVENT_LOOP.run_until_complete(request_without_session())
        self.assertEqual(401, response.status_code)


if __name__ == "__main__":
    unittest.main()
