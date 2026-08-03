from __future__ import annotations

from urllib.parse import parse_qs, urlparse

import pytest

from app import yandex_oauth


def _set_config(monkeypatch: pytest.MonkeyPatch) -> yandex_oauth.YandexOAuthConfig:
    monkeypatch.setenv("YANDEX_OAUTH_CLIENT_ID", "client-id")
    monkeypatch.setenv("YANDEX_OAUTH_CLIENT_SECRET", "client-secret")
    monkeypatch.setenv(
        "YANDEX_OAUTH_REDIRECT_URI",
        "https://crm.example.test/api/auth/yandex/callback",
    )
    monkeypatch.setenv(
        "YANDEX_OAUTH_USER_MAP",
        '{"login:owner":"admin","id:42":"manager","email:user@yandex.ru":"viewer"}',
    )
    config = yandex_oauth.get_config()
    assert config is not None
    return config


def test_config_requires_complete_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "YANDEX_OAUTH_ENABLED",
        "YANDEX_OAUTH_CLIENT_ID",
        "YANDEX_OAUTH_CLIENT_SECRET",
        "YANDEX_OAUTH_REDIRECT_URI",
        "YANDEX_OAUTH_USER_MAP",
    ):
        monkeypatch.delenv(name, raising=False)
    assert yandex_oauth.get_config() is None


def test_authorization_url_uses_code_flow_pkce_and_state(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _set_config(monkeypatch)
    state, challenge, _cookie = yandex_oauth.create_flow(config)
    parsed = urlparse(
        yandex_oauth.build_authorization_url(config, state=state, code_challenge=challenge)
    )
    query = parse_qs(parsed.query)
    assert parsed.scheme == "https"
    assert parsed.netloc == "oauth.yandex.ru"
    assert query["response_type"] == ["code"]
    assert query["client_id"] == ["client-id"]
    assert query["state"] == [state]
    assert query["code_challenge_method"] == ["S256"]
    assert query["code_challenge"] == [challenge]


def test_signed_flow_cookie_round_trip_and_tamper_rejection(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _set_config(monkeypatch)
    cookie = yandex_oauth._sign_flow_cookie(
        config,
        state="state-value",
        verifier="verifier-value",
        issued_at=1_000,
    )
    assert (
        yandex_oauth.verify_flow_cookie(
            config,
            cookie,
            expected_state="state-value",
            now=1_100,
        )
        == "verifier-value"
    )
    with pytest.raises(yandex_oauth.YandexOAuthError):
        yandex_oauth.verify_flow_cookie(
            config,
            f"{cookie[:-1]}0",
            expected_state="state-value",
            now=1_100,
        )


def test_flow_cookie_rejects_wrong_state_and_expiry(monkeypatch: pytest.MonkeyPatch) -> None:
    config = _set_config(monkeypatch)
    cookie = yandex_oauth._sign_flow_cookie(
        config,
        state="state-value",
        verifier="verifier-value",
        issued_at=1_000,
    )
    with pytest.raises(yandex_oauth.YandexOAuthError):
        yandex_oauth.verify_flow_cookie(config, cookie, expected_state="other", now=1_100)
    with pytest.raises(yandex_oauth.YandexOAuthError):
        yandex_oauth.verify_flow_cookie(
            config,
            cookie,
            expected_state="state-value",
            now=1_000 + yandex_oauth.FLOW_TTL_SECONDS + 1,
        )


@pytest.mark.parametrize(
    ("profile", "expected"),
    [
        ({"id": "42", "login": "other"}, "manager"),
        ({"id": "7", "login": "OWNER"}, "admin"),
        ({"id": "7", "login": "other", "default_email": "USER@YANDEX.RU"}, "viewer"),
        ({"id": "7", "login": "unknown"}, None),
    ],
)
def test_explicit_mapping_only(
    monkeypatch: pytest.MonkeyPatch,
    profile: dict[str, str],
    expected: str | None,
) -> None:
    config = _set_config(monkeypatch)
    assert yandex_oauth.resolve_crm_username(config, profile) == expected


def test_explicit_disable_wins_over_complete_config(monkeypatch: pytest.MonkeyPatch) -> None:
    _set_config(monkeypatch)
    monkeypatch.setenv("YANDEX_OAUTH_ENABLED", "0")
    assert yandex_oauth.get_config() is None
