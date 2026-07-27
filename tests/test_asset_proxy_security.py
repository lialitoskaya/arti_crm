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


JPEG = b"\xff\xd8\xff\xe0jpeg"
PNG = b"\x89PNG\r\n\x1a\npng"
GIF = b"GIF89agif"
WEBP = b"RIFF\x08\x00\x00\x00WEBPwebp"
GLOBAL_V4 = "8.8.8.8"
GLOBAL_V6 = "2606:4700:4700::1111"


class _FakeChunkIterator:
    def __init__(self, response: "_FakeStreamResponse"):
        self.response = response
        self.index = 0

    def __aiter__(self):
        return self

    async def __anext__(self):
        if self.index >= len(self.response._chunks):
            raise StopAsyncIteration
        chunk = self.response._chunks[self.index]
        self.index += 1
        self.response.chunks_read += 1
        if isinstance(chunk, BaseException):
            raise chunk
        return chunk


class _FakeStreamResponse:
    def __init__(
        self,
        status_code: int,
        headers: dict[str, str],
        chunks: list[bytes | BaseException],
        url: str,
    ):
        self.status_code = status_code
        self.headers = httpx.Headers(headers)
        self._chunks = list(chunks)
        self.request = httpx.Request("GET", url)
        self.chunks_read = 0
        self.closed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        self.closed = True
        return False

    def aiter_raw(self, _chunk_size: int = 65536):
        return _FakeChunkIterator(self)

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "upstream failed",
                request=self.request,
                response=httpx.Response(self.status_code, request=self.request),
            )


class _FakeErrorStream:
    def __init__(self, error: BaseException):
        self.error = error

    async def __aenter__(self):
        raise self.error

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False


class _FakeAsyncClient:
    responses: list[object] = []
    requests: list[tuple[str, dict[str, str]]] = []
    init_kwargs: list[dict[str, object]] = []
    created_responses: list[_FakeStreamResponse] = []

    def __init__(self, *_args, **kwargs):
        self.init_kwargs.append(dict(kwargs))

    async def __aenter__(self):
        return self

    async def __aexit__(self, _exc_type, _exc, _traceback):
        return False

    def stream(self, method: str, url: str, *, headers: dict[str, str]):
        if method != "GET":
            raise AssertionError(f"Unexpected method: {method}")
        self.requests.append((url, dict(headers)))
        if not self.responses:
            raise AssertionError("Unexpected asset proxy request")
        item = self.responses.pop(0)
        if isinstance(item, BaseException):
            return _FakeErrorStream(item)
        status_code, response_headers, chunks = item
        if isinstance(chunks, bytes):
            chunks = [chunks]
        response = _FakeStreamResponse(status_code, response_headers, list(chunks), url)
        self.created_responses.append(response)
        return response


class _FakeResolver:
    def __init__(self, answers: dict[str, list[str]] | None = None):
        self.answers = answers or {}
        self.calls: list[str] = []

    def __call__(self, hostname: str):
        self.calls.append(hostname)
        return self.answers.get(hostname, [GLOBAL_V4])


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

    def test_only_global_ipv4_and_ipv6_answers_are_accepted(self) -> None:
        self.assertTrue(policy.asset_addresses_are_global([GLOBAL_V4, GLOBAL_V6]))
        rejected = (
            "127.0.0.1",
            "10.0.0.1",
            "169.254.1.1",
            "224.0.0.1",
            "192.0.2.1",
            "0.0.0.0",
            "::1",
            "fc00::1",
            "fe80::1",
            "ff02::1",
            "2001:db8::1",
            "::",
        )
        for address in rejected:
            with self.subTest(address=address):
                self.assertFalse(policy.asset_addresses_are_global([address]))
        self.assertFalse(policy.asset_addresses_are_global([]))
        self.assertFalse(policy.asset_addresses_are_global([GLOBAL_V4, "127.0.0.1"]))

    def test_dns_validation_is_injectable_and_fail_closed(self) -> None:
        resolver = _FakeResolver({"cdn.ngenix.net": [GLOBAL_V4, GLOBAL_V6]})
        self.assertTrue(policy.asset_url_resolves_globally("https://cdn.ngenix.net/a", resolver))
        self.assertEqual(["cdn.ngenix.net"], resolver.calls)
        self.assertFalse(policy.asset_url_resolves_globally("https://127.0.0.1/a", resolver))
        self.assertFalse(policy.asset_url_resolves_globally("https://cdn.ngenix.net/a", lambda _host: []))
        self.assertFalse(
            policy.asset_url_resolves_globally(
                "https://cdn.ngenix.net/a",
                mock.Mock(side_effect=OSError("resolver unavailable")),
            )
        )


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
        _FakeAsyncClient.created_responses = []

    def tearDown(self) -> None:
        self.assertEqual([], foundation._NETWORK_ATTEMPTS, "A test attempted real network access")

    def _run_proxy(
        self,
        url: str,
        responses: list[object],
        *,
        resolver: _FakeResolver | None = None,
    ):
        _FakeAsyncClient.responses = list(responses)
        _FakeAsyncClient.requests = []
        _FakeAsyncClient.init_kwargs = []
        _FakeAsyncClient.created_responses = []
        fake_resolver = resolver or _FakeResolver()
        with (
            mock.patch.object(main.httpx, "AsyncClient", _FakeAsyncClient),
            mock.patch.object(main, "resolve_asset_host_addresses", fake_resolver),
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
            [(200, {"content-type": "image/jpeg"}, JPEG)],
        )
        self.assertEqual(JPEG, response.body)
        self.assertEqual("image/jpeg", response.media_type)
        self.assertEqual("private, max-age=3600", response.headers["cache-control"])
        self.assertEqual("nosniff", response.headers["x-content-type-options"])
        self.assertEqual(1, len(_FakeAsyncClient.requests))
        self.assertEqual("identity", _FakeAsyncClient.requests[0][1]["Accept-Encoding"])
        self.assertEqual(False, _FakeAsyncClient.init_kwargs[0]["follow_redirects"])
        self.assertEqual(25, _FakeAsyncClient.init_kwargs[0]["timeout"])
        self.assertTrue(_FakeAsyncClient.created_responses[0].closed)

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
                (200, {"content-type": "image/png"}, PNG),
            ],
        )
        self.assertEqual("test-client-id", _FakeAsyncClient.requests[0][1].get("Client-Id"))
        self.assertNotIn("Client-Id", _FakeAsyncClient.requests[1][1])
        self.assertNotIn("Api-Key", _FakeAsyncClient.requests[1][1])

        self._run_proxy(
            "https://cdn.ngenix.net/start",
            [
                (302, {"location": "https://api-seller.ozon.ru/image"}, b""),
                (200, {"content-type": "image/png"}, PNG),
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
                (200, {"content-type": "image/webp"}, WEBP),
            ],
        )
        self.assertEqual(WEBP, response.body)
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
                    (302, {"location": "/one"}, b"redirect-one"),
                    (302, {"location": "/two"}, b"redirect-two"),
                    (302, {"location": "/three"}, b"redirect-three"),
                    (302, {"location": "/four"}, b"redirect-four"),
                ],
            )
        self.assertEqual(502, limit_error.exception.status_code)
        self.assertEqual(4, len(_FakeAsyncClient.requests))
        self.assertEqual([0, 0, 0, 0], [item.chunks_read for item in _FakeAsyncClient.created_responses])
        self.assertTrue(all(item.closed for item in _FakeAsyncClient.created_responses))

        with self.assertRaises(HTTPException) as location_error:
            self._run_proxy("https://cdn.ngenix.net/start", [(302, {}, b"redirect-body")])
        self.assertEqual(502, location_error.exception.status_code)
        self.assertEqual(1, len(_FakeAsyncClient.requests))
        self.assertEqual(0, _FakeAsyncClient.created_responses[0].chunks_read)
        self.assertTrue(_FakeAsyncClient.created_responses[0].closed)

    def test_content_length_oversize_is_rejected_before_read_and_closed(self) -> None:
        with mock.patch.dict(_DYNAMIC_ENV_OVERRIDES, {"IMAGE_PROXY_MAX_BYTES": "100000"}):
            with self.assertRaises(HTTPException) as error:
                self._run_proxy(
                    "https://cdn.ngenix.net/large",
                    [(200, {"content-type": "image/jpeg", "content-length": "100001"}, [JPEG])],
                )
        upstream = _FakeAsyncClient.created_responses[0]
        self.assertEqual(413, error.exception.status_code)
        self.assertEqual(0, upstream.chunks_read)
        self.assertTrue(upstream.closed)

    def test_unknown_length_stops_after_limit_without_reading_remaining_chunks(self) -> None:
        chunks = [JPEG + b"x" * (50_001 - len(JPEG)), b"y" * 50_000, b"must-not-be-read"]
        with mock.patch.dict(_DYNAMIC_ENV_OVERRIDES, {"IMAGE_PROXY_MAX_BYTES": "100000"}):
            with self.assertRaises(HTTPException) as error:
                self._run_proxy(
                    "https://cdn.ngenix.net/large",
                    [(200, {"content-type": "image/jpeg"}, chunks)],
                )
        upstream = _FakeAsyncClient.created_responses[0]
        self.assertEqual(413, error.exception.status_code)
        self.assertEqual(2, upstream.chunks_read)
        self.assertTrue(upstream.closed)

    def test_http_error_response_is_closed_without_reading_body(self) -> None:
        with self.assertRaises(HTTPException) as error:
            self._run_proxy(
                "https://cdn.ngenix.net/fail",
                [(500, {"content-type": "image/jpeg"}, b"upstream-error-body")],
            )
        upstream = _FakeAsyncClient.created_responses[0]
        self.assertEqual(502, error.exception.status_code)
        self.assertEqual("Image preview request failed", error.exception.detail)
        self.assertEqual(0, upstream.chunks_read)
        self.assertTrue(upstream.closed)

    def test_stream_iteration_error_closes_response_and_is_sanitized(self) -> None:
        secret_text = "stream failed for token=do-not-expose"
        with self.assertRaises(HTTPException) as error:
            self._run_proxy(
                "https://cdn.ngenix.net/fail",
                [(200, {"content-type": "image/jpeg"}, [JPEG, RuntimeError(secret_text)])],
            )
        upstream = _FakeAsyncClient.created_responses[0]
        self.assertEqual(502, error.exception.status_code)
        self.assertEqual("Image preview request failed", error.exception.detail)
        self.assertNotIn(secret_text, str(error.exception.detail))
        self.assertEqual(2, upstream.chunks_read)
        self.assertTrue(upstream.closed)

    def test_malformed_content_length_is_rejected_before_read_and_closed(self) -> None:
        with self.assertRaises(HTTPException) as error:
            self._run_proxy(
                "https://cdn.ngenix.net/image",
                [(200, {"content-type": "image/jpeg", "content-length": "invalid"}, JPEG)],
            )
        upstream = _FakeAsyncClient.created_responses[0]
        self.assertEqual(502, error.exception.status_code)
        self.assertEqual(0, upstream.chunks_read)
        self.assertTrue(upstream.closed)

    def test_encoded_response_is_rejected_before_read_and_closed(self) -> None:
        for content_encoding in ("gzip", "br", "identity, gzip"):
            with self.subTest(content_encoding=content_encoding):
                with self.assertRaises(HTTPException) as error:
                    self._run_proxy(
                        "https://cdn.ngenix.net/image",
                        [
                            (
                                200,
                                {
                                    "content-type": "image/jpeg",
                                    "content-encoding": content_encoding,
                                },
                                b"encoded-body-must-not-be-read",
                            )
                        ],
                    )
                upstream = _FakeAsyncClient.created_responses[0]
                self.assertEqual(502, error.exception.status_code)
                self.assertEqual("Invalid image response", error.exception.detail)
                self.assertEqual(0, upstream.chunks_read)
                self.assertTrue(upstream.closed)

    def test_redirect_body_is_not_buffered_and_all_responses_close(self) -> None:
        response = self._run_proxy(
            "https://cdn.ngenix.net/start",
            [
                (302, {"location": "/next"}, [b"redirect-body"]),
                (200, {"content-type": "image/gif"}, GIF),
            ],
        )
        self.assertEqual(GIF, response.body)
        self.assertEqual([0, 1], [item.chunks_read for item in _FakeAsyncClient.created_responses])
        self.assertTrue(all(item.closed for item in _FakeAsyncClient.created_responses))
        self.assertTrue(
            all(headers["Accept-Encoding"] == "identity" for _url, headers in _FakeAsyncClient.requests)
        )

    def test_small_jpeg_png_webp_and_gif_pass_magic_validation(self) -> None:
        for content_type, body in (
            ("image/jpeg", JPEG),
            ("image/png", PNG),
            ("image/webp", WEBP),
            ("image/gif", GIF),
        ):
            with self.subTest(content_type=content_type):
                response = self._run_proxy(
                    "https://cdn.ngenix.net/image",
                    [(200, {"content-type": content_type}, body)],
                )
                self.assertEqual(body, response.body)
                self.assertEqual(content_type, response.media_type)

    def test_active_ambiguous_and_mismatched_content_is_rejected(self) -> None:
        rejected = (
            ("image/svg+xml", b"<svg></svg>"),
            ("text/html", b"<html></html>"),
            ("application/xml", b"<?xml version='1.0'?>"),
            ("application/xhtml+xml", b"<html></html>"),
            ("application/octet-stream", JPEG),
            ("binary/octet-stream", JPEG),
            ("", JPEG),
            ("image/jpeg, image/png", JPEG),
            ("image/jpeg", PNG),
            ("image/png", JPEG),
        )
        for content_type, body in rejected:
            with self.subTest(content_type=content_type, body=body[:8]):
                headers = {"content-type": content_type} if content_type else {}
                with self.assertRaises(HTTPException) as error:
                    self._run_proxy("https://cdn.ngenix.net/file", [(200, headers, body)])
                self.assertEqual(415, error.exception.status_code)
                self.assertTrue(_FakeAsyncClient.created_responses[0].closed)

    def test_each_redirect_hop_revalidates_dns_and_credentials(self) -> None:
        resolver = _FakeResolver(
            {
                "api-seller.ozon.ru": [GLOBAL_V4],
                "cdn.ngenix.net": [GLOBAL_V6],
            }
        )
        response = self._run_proxy(
            "https://api-seller.ozon.ru/start",
            [
                (302, {"location": "https://cdn.ngenix.net/image"}, b""),
                (200, {"content-type": "image/webp"}, WEBP),
            ],
            resolver=resolver,
        )
        self.assertEqual(WEBP, response.body)
        self.assertEqual(["api-seller.ozon.ru", "cdn.ngenix.net"], resolver.calls)
        self.assertEqual("test-client-id", _FakeAsyncClient.requests[0][1].get("Client-Id"))
        self.assertNotIn("Client-Id", _FakeAsyncClient.requests[1][1])
        self.assertNotIn("Api-Key", _FakeAsyncClient.requests[1][1])

    def test_private_and_mixed_dns_redirects_stop_before_second_request(self) -> None:
        for answer in (["127.0.0.1"], [GLOBAL_V4, "10.0.0.1"], ["fe80::1"]):
            with self.subTest(answer=answer):
                resolver = _FakeResolver(
                    {"cdn.ngenix.net": [GLOBAL_V4], "o3static.com": list(answer)}
                )
                with self.assertRaises(HTTPException) as error:
                    self._run_proxy(
                        "https://cdn.ngenix.net/start",
                        [(302, {"location": "https://o3static.com/private"}, b"")],
                        resolver=resolver,
                    )
                self.assertEqual(502, error.exception.status_code)
                self.assertEqual(1, len(_FakeAsyncClient.requests))

    def test_empty_dns_answer_fails_closed_before_request(self) -> None:
        resolver = _FakeResolver({"cdn.ngenix.net": []})
        with self.assertRaises(HTTPException) as error:
            self._run_proxy("https://cdn.ngenix.net/private?token=hidden", [], resolver=resolver)
        self.assertEqual(502, error.exception.status_code)
        self.assertEqual([], _FakeAsyncClient.requests)
        self.assertNotIn("token", str(error.exception.detail))

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
