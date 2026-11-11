"""Managed Yandex login rules and lookups used during CRM session creation."""
from __future__ import annotations

import sqlite3
from typing import Any

from app.db import get_connection


class YandexOAuthManagedLinkConflictError(RuntimeError):
    """A managed identifier or confirmed Yandex identity is already assigned."""


class YandexOAuthManagedLinkUserNotFoundError(RuntimeError):
    """The selected CRM user does not exist."""


def _normalize_yandex_oauth_identifier(identifier: str) -> str:
    return str(identifier or '').strip().casefold()


def _yandex_oauth_managed_link_to_dict(row: Any) -> dict[str, Any]:
    data = dict(row)
    return {
        'id': int(data['id']),
        'identifier_type': data['identifier_type'],
        'identifier': data['display_identifier'],
        'normalized_identifier': data['normalized_identifier'],
        'crm_user_id': int(data['crm_user_id']),
        'crm_username': data.get('crm_username'),
        'crm_display_name': data.get('crm_display_name'),
        'crm_user_is_active': bool(data.get('crm_user_is_active')),
        'is_active': bool(data['is_active']),
        'confirmed': bool(data.get('yandex_user_id')),
        'created_at': data['created_at'],
        'updated_at': data['updated_at'],
    }


_YANDEX_OAUTH_MANAGED_LINK_SELECT = """
    SELECT
        managed.id,
        managed.identifier_type,
        managed.display_identifier,
        managed.normalized_identifier,
        managed.crm_user_id,
        managed.yandex_user_id,
        managed.is_active,
        managed.created_at,
        managed.updated_at,
        users.username AS crm_username,
        users.display_name AS crm_display_name,
        users.is_active AS crm_user_is_active
    FROM yandex_oauth_managed_links managed
    LEFT JOIN users ON users.id = managed.crm_user_id
"""


def list_yandex_oauth_managed_links() -> list[dict[str, Any]]:
    with get_connection() as conn:
        rows = conn.execute(
            _YANDEX_OAUTH_MANAGED_LINK_SELECT + " ORDER BY managed.id ASC"
        ).fetchall()
        return [_yandex_oauth_managed_link_to_dict(row) for row in rows]


def create_yandex_oauth_managed_link(
    *,
    identifier_type: str,
    identifier: str,
    crm_user_id: int,
) -> dict[str, Any]:
    link_type = str(identifier_type or '').strip().lower()
    display_identifier = str(identifier or '').strip()
    normalized_identifier = _normalize_yandex_oauth_identifier(display_identifier)
    if link_type not in {'login', 'email'}:
        raise ValueError('Unsupported Yandex OAuth identifier type')
    if not display_identifier or not normalized_identifier:
        raise ValueError('Yandex OAuth identifier is required')

    try:
        with get_connection() as conn:
            conn.execute("BEGIN IMMEDIATE")
            if not conn.execute("SELECT 1 FROM users WHERE id=?", (crm_user_id,)).fetchone():
                raise YandexOAuthManagedLinkUserNotFoundError
            cursor = conn.execute(
                """
                INSERT INTO yandex_oauth_managed_links (
                    identifier_type,
                    display_identifier,
                    normalized_identifier,
                    crm_user_id
                ) VALUES (?, ?, ?, ?)
                """,
                (link_type, display_identifier, normalized_identifier, crm_user_id),
            )
            row = conn.execute(
                _YANDEX_OAUTH_MANAGED_LINK_SELECT + " WHERE managed.id=?",
                (cursor.lastrowid,),
            ).fetchone()
            return _yandex_oauth_managed_link_to_dict(row)
    except sqlite3.IntegrityError as exc:
        raise YandexOAuthManagedLinkConflictError from exc


def update_yandex_oauth_managed_link(
    link_id: int,
    *,
    is_active: bool,
) -> dict[str, Any] | None:
    with get_connection() as conn:
        updated = conn.execute(
            """
            UPDATE yandex_oauth_managed_links
            SET is_active=?, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (1 if is_active else 0, link_id),
        )
        if updated.rowcount != 1:
            return None
        row = conn.execute(
            _YANDEX_OAUTH_MANAGED_LINK_SELECT + " WHERE managed.id=?",
            (link_id,),
        ).fetchone()
        return _yandex_oauth_managed_link_to_dict(row)


def delete_yandex_oauth_managed_link(link_id: int) -> bool:
    with get_connection() as conn:
        archived = conn.execute(
            """
            UPDATE yandex_oauth_managed_links
            SET is_active=0, updated_at=CURRENT_TIMESTAMP
            WHERE id=?
            """,
            (link_id,),
        )
        return archived.rowcount == 1


def normalize_candidates(
    login: str | None,
    emails: list[str] | None,
) -> tuple[str, list[str]]:
    normalized_login = _normalize_yandex_oauth_identifier(login or '')
    normalized_emails: list[str] = []
    for email in emails or []:
        normalized = _normalize_yandex_oauth_identifier(email)
        if normalized and normalized not in normalized_emails:
            normalized_emails.append(normalized)
    return normalized_login, normalized_emails


def find_managed_link_row(
    conn: sqlite3.Connection,
    yandex_user_id: str,
    *,
    normalized_login: str,
    normalized_emails: list[str],
) -> sqlite3.Row | None:
    """Use the caller's connection so session creation can recheck the rule atomically."""
    row = None
    if yandex_user_id:
        row = conn.execute(
            _YANDEX_OAUTH_MANAGED_LINK_SELECT + " WHERE managed.yandex_user_id=?",
            (yandex_user_id,),
        ).fetchone()
    if row is None and normalized_login:
        row = conn.execute(
            _YANDEX_OAUTH_MANAGED_LINK_SELECT
            + " WHERE managed.identifier_type='login' AND managed.normalized_identifier=?",
            (normalized_login,),
        ).fetchone()
    if row is None:
        for normalized_email in normalized_emails:
            row = conn.execute(
                _YANDEX_OAUTH_MANAGED_LINK_SELECT
                + " WHERE managed.identifier_type='email' AND managed.normalized_identifier=?",
                (normalized_email,),
            ).fetchone()
            if row is not None:
                break
    return row


def find_yandex_oauth_managed_link(
    yandex_user_id: str,
    *,
    login: str | None = None,
    emails: list[str] | None = None,
) -> dict[str, Any] | None:
    identity = str(yandex_user_id or '').strip()
    normalized_login, normalized_emails = normalize_candidates(login, emails)

    with get_connection() as conn:
        row = find_managed_link_row(
            conn,
            identity,
            normalized_login=normalized_login,
            normalized_emails=normalized_emails,
        )
        if row is None:
            return None
        return _yandex_oauth_managed_link_to_dict(row)
