from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from typing import Any

from app.db import get_connection


# Analytics v99
# -----------------------------------------------------------------------------
# Business definition for "обращение": first real client inbound message per chat per
# local day. Marketplace bot/support/system messages are excluded. If a client writes again on the same day, it is not counted as a new
# request. If the same client writes on the next day, it is counted once for that
# next day. Raw inbound message count is still returned as a separate load metric.
# -----------------------------------------------------------------------------


def _env_int(name: str, default: int, *, minimum: int | None = None, maximum: int | None = None) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except Exception:
        value = default
    if minimum is not None:
        value = max(minimum, value)
    if maximum is not None:
        value = min(maximum, value)
    return value


def _date_or_default(value: str | None, default: str) -> str:
    if not value:
        return default
    text = str(value).strip()[:10]
    try:
        datetime.strptime(text, "%Y-%m-%d")
    except Exception:
        return default
    return text


def _hour_or_none(value: int | None) -> int | None:
    if value is None:
        return None
    try:
        hour = int(value)
    except Exception:
        return None
    return hour if 0 <= hour <= 23 else None


def _tz_modifier(offset_minutes: int) -> str:
    sign = "+" if offset_minutes >= 0 else "-"
    return f"{sign}{abs(int(offset_minutes))} minutes"


def _period_defaults(tz_offset_minutes: int) -> tuple[str, str]:
    today = (datetime.now(timezone.utc) + timedelta(minutes=tz_offset_minutes)).date()
    return (today - timedelta(days=13)).isoformat(), today.isoformat()


def _round_seconds(value: Any) -> int | None:
    if value is None:
        return None
    try:
        return int(round(float(value)))
    except Exception:
        return None


def _row_dict(row: Any) -> dict[str, Any]:
    return dict(row) if row is not None else {}


def _marketplace_filter_sql(alias: str, marketplace: str | None, params: list[Any]) -> str:
    if not marketplace:
        return ""
    params.append(marketplace)
    return f" AND {alias}.marketplace = ?"


def _analytics_client_message_sql(alias: str = "m") -> str:
    """SQL predicate for real customer inbound messages.

    Marketplace APIs can put bot/support/system messages into the same local
    `messages` table. They should remain visible in chat history, but they must
    not distort analytics: requests, peak hours, outside-hours counts and average
    response time.

    The filter is intentionally conservative:
    - direction='inbound' is still required by callers;
    - obvious seller/manager authors are excluded;
    - raw marketplace payloads with system/support/bot/notification user markers
      are excluded;
    - ordinary customer messages remain counted.
    """
    author = f"LOWER(COALESCE({alias}.author, ''))"
    raw = f"LOWER(COALESCE({alias}.raw_json, ''))"
    return f"""
        (
            {author} NOT IN (
                'seller', 'manager', 'operator', 'admin', 'support', 'system',
                'marketplace', 'bot', 'robot', 'notificationuser',
                'продавец', 'менеджер', 'оператор', 'поддержка', 'система', 'бот'
            )
            AND {author} NOT LIKE '%support%'
            AND {author} NOT LIKE '%system%'
            AND {author} NOT LIKE '%bot%'
            AND {author} NOT LIKE '%robot%'
            AND {author} NOT LIKE '%notificationuser%'
            AND {author} NOT LIKE '%notification_user%'
            AND {author} NOT LIKE '%поддерж%'
            AND {author} NOT LIKE '%систем%'
            AND {author} NOT LIKE '%бот%'
            AND {raw} NOT LIKE '%"user_type": "support"%'
            AND {raw} NOT LIKE '%"user_type":"support"%'
            AND {raw} NOT LIKE '%"user_type": "system"%'
            AND {raw} NOT LIKE '%"user_type":"system"%'
            AND {raw} NOT LIKE '%"user_type": "bot"%'
            AND {raw} NOT LIKE '%"user_type":"bot"%'
            AND {raw} NOT LIKE '%"author_type": "support"%'
            AND {raw} NOT LIKE '%"author_type":"support"%'
            AND {raw} NOT LIKE '%"author_type": "system"%'
            AND {raw} NOT LIKE '%"author_type":"system"%'
            AND {raw} NOT LIKE '%"author_type": "bot"%'
            AND {raw} NOT LIKE '%"author_type":"bot"%'
            AND {raw} NOT LIKE '%"sender_type": "support"%'
            AND {raw} NOT LIKE '%"sender_type":"support"%'
            AND {raw} NOT LIKE '%"sender_type": "system"%'
            AND {raw} NOT LIKE '%"sender_type":"system"%'
            AND {raw} NOT LIKE '%"sender_type": "bot"%'
            AND {raw} NOT LIKE '%"sender_type":"bot"%'
            AND {raw} NOT LIKE '%"sendertype": "support"%'
            AND {raw} NOT LIKE '%"sendertype":"support"%'
            AND {raw} NOT LIKE '%"sendertype": "system"%'
            AND {raw} NOT LIKE '%"sendertype":"system"%'
            AND {raw} NOT LIKE '%"sendertype": "bot"%'
            AND {raw} NOT LIKE '%"sendertype":"bot"%'
            AND {raw} NOT LIKE '%"type": "support"%'
            AND {raw} NOT LIKE '%"type":"support"%'
            AND {raw} NOT LIKE '%"type": "system"%'
            AND {raw} NOT LIKE '%"type":"system"%'
            AND {raw} NOT LIKE '%"type": "bot"%'
            AND {raw} NOT LIKE '%"type":"bot"%'
            AND {raw} NOT LIKE '%"type": "notificationuser"%'
            AND {raw} NOT LIKE '%"type":"notificationuser"%'
            AND {raw} NOT LIKE '%"notificationuser"%'
            AND {raw} NOT LIKE '%"notification_user"%'
            AND {raw} NOT LIKE '%"systemuser"%'
            AND {raw} NOT LIKE '%"system_user"%'
            AND {raw} NOT LIKE '%"_crm_source": "marketplace_bot"%'
            AND {raw} NOT LIKE '%"_crm_source":"marketplace_bot"%'
            AND {raw} NOT LIKE '%"_crm_excluded_as_system": true%'
            AND {raw} NOT LIKE '%"_crm_excluded_as_system":true%'
        )
    """


def _hour_filter_sql(column: str, hour_from: int | None, hour_to: int | None, params: list[Any]) -> str:
    if hour_from is None and hour_to is None:
        return ""
    if hour_from is not None and hour_to is not None:
        params.extend([hour_from, hour_to])
        if hour_from <= hour_to:
            return f" AND {column} BETWEEN ? AND ?"
        return f" AND ({column} >= ? OR {column} <= ?)"
    if hour_from is not None:
        params.append(hour_from)
        return f" AND {column} >= ?"
    params.append(hour_to)
    return f" AND {column} <= ?"


def _closed_filter_sql(*, modifier: str, start: str, end: str, marketplace: str | None) -> tuple[str, list[Any]]:
    params: list[Any] = [modifier, start, end]
    sql = "c.status='closed' AND date(datetime(COALESCE(c.last_message_at, c.updated_at, c.created_at), ?)) BETWEEN ? AND ?"
    sql += _marketplace_filter_sql("c", marketplace, params)
    return sql, params


def _daily_requests_cte_sql(
    *,
    modifier: str,
    start: str,
    end: str,
    marketplace: str | None,
    hour_from: int | None = None,
    hour_to: int | None = None,
    include_hour_filter: bool = True,
) -> tuple[str, list[Any]]:
    """CTE for one analytical request per chat per local day.

    A request is the first inbound message in a chat on a local calendar day.
    The timezone is applied through SQLite's datetime modifier.
    """
    params: list[Any] = [modifier, modifier, modifier, modifier, start, end]
    marketplace_sql = _marketplace_filter_sql("c", marketplace, params)
    hour_sql = _hour_filter_sql("local_hour", hour_from, hour_to, params) if include_hour_filter else ""

    return f"""
        WITH inbound_ranked AS (
            SELECT
                m.id,
                m.chat_id,
                m.created_at,
                c.marketplace,
                c.external_chat_id,
                COALESCE(NULLIF(c.customer_public_id, ''), c.marketplace || ':' || c.external_chat_id) AS client_key,
                date(datetime(m.created_at, ?)) AS local_day,
                CAST(strftime('%H', datetime(m.created_at, ?)) AS INTEGER) AS local_hour,
                ROW_NUMBER() OVER (
                    PARTITION BY m.chat_id, date(datetime(m.created_at, ?))
                    ORDER BY julianday(m.created_at), m.id
                ) AS daily_inbound_rank
            FROM messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE m.direction='inbound'
              AND {_analytics_client_message_sql('m')}
              AND date(datetime(m.created_at, ?)) BETWEEN ? AND ?
              {marketplace_sql}
        ),
        daily_requests AS (
            SELECT *
            FROM inbound_ranked
            WHERE daily_inbound_rank = 1
              {hour_sql}
        )
    """, params


def _raw_inbound_where_sql(
    *,
    modifier: str,
    start: str,
    end: str,
    marketplace: str | None,
    hour_from: int | None,
    hour_to: int | None,
) -> tuple[str, list[Any]]:
    params: list[Any] = [modifier, start, end]
    sql = f"m.direction='inbound' AND {_analytics_client_message_sql('m')} AND date(datetime(m.created_at, ?)) BETWEEN ? AND ?"
    sql += _marketplace_filter_sql("c", marketplace, params)
    if hour_from is not None or hour_to is not None:
        params.append(modifier)
        hour_expr = "CAST(strftime('%H', datetime(m.created_at, ?)) AS INTEGER)"
        sql += _hour_filter_sql(hour_expr, hour_from, hour_to, params)
    return sql, params


def _fetch_raw_inbound_count(conn: Any, where_sql: str, params: list[Any]) -> int:
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS inbound_messages
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE {where_sql}
        """,
        params,
    ).fetchone()
    return int((_row_dict(row).get("inbound_messages") or 0))


def _fetch_excluded_service_message_count(
    conn: Any,
    *,
    modifier: str,
    start: str,
    end: str,
    marketplace: str | None,
    hour_from: int | None,
    hour_to: int | None,
) -> int:
    """Count inbound-looking messages excluded from analytics as service/bot/system."""
    params: list[Any] = [modifier, start, end]
    marketplace_sql = _marketplace_filter_sql("c", marketplace, params)
    hour_sql = ""
    if hour_from is not None or hour_to is not None:
        params.append(modifier)
        hour_sql = _hour_filter_sql("CAST(strftime('%H', datetime(m.created_at, ?)) AS INTEGER)", hour_from, hour_to, params)
    row = conn.execute(
        f"""
        SELECT COUNT(*) AS excluded_messages
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE m.direction='inbound'
          AND NOT {_analytics_client_message_sql('m')}
          AND date(datetime(m.created_at, ?)) BETWEEN ? AND ?
          {marketplace_sql}
          {hour_sql}
        """,
        params,
    ).fetchone()
    return int((_row_dict(row).get("excluded_messages") or 0))


def _fetch_summary(conn: Any, cte_sql: str, params: list[Any]) -> dict[str, Any]:
    row = conn.execute(
        cte_sql
        + """
        SELECT
            COUNT(*) AS requests,
            COUNT(DISTINCT chat_id) AS unique_chats,
            COUNT(DISTINCT client_key) AS unique_clients
        FROM daily_requests
        """,
        params,
    ).fetchone()
    return _row_dict(row)


def _fetch_daily(
    conn: Any,
    cte_sql: str,
    request_params: list[Any],
    raw_where_sql: str,
    raw_params: list[Any],
    closed_where_sql: str,
    closed_params: list[Any],
    modifier: str,
) -> list[dict[str, Any]]:
    request_rows = conn.execute(
        cte_sql
        + """
        SELECT
            local_day AS day,
            COUNT(*) AS requests,
            COUNT(DISTINCT chat_id) AS unique_chats,
            COUNT(DISTINCT client_key) AS unique_clients
        FROM daily_requests
        GROUP BY local_day
        ORDER BY local_day
        """,
        request_params,
    ).fetchall()

    raw_rows = conn.execute(
        f"""
        SELECT
            date(datetime(m.created_at, ?)) AS day,
            COUNT(*) AS inbound_messages
        FROM messages m
        JOIN chats c ON c.id = m.chat_id
        WHERE {raw_where_sql}
        GROUP BY day
        ORDER BY day
        """,
        [modifier] + raw_params,
    ).fetchall()

    closed_rows = conn.execute(
        f"""
        SELECT
            date(datetime(COALESCE(c.last_message_at, c.updated_at, c.created_at), ?)) AS day,
            COUNT(*) AS closed_chats
        FROM chats c
        WHERE {closed_where_sql}
        GROUP BY day
        ORDER BY day
        """,
        [modifier] + closed_params,
    ).fetchall()

    daily: dict[str, dict[str, Any]] = {}
    for row in request_rows:
        daily[row["day"]] = {
            "date": row["day"],
            "requests": int(row["requests"] or 0),
            "inbound_messages": 0,
            "unique_chats": int(row["unique_chats"] or 0),
            "unique_clients": int(row["unique_clients"] or 0),
            "closed_chats": 0,
        }
    for row in raw_rows:
        day = row["day"]
        daily.setdefault(day, {"date": day, "requests": 0, "inbound_messages": 0, "unique_chats": 0, "unique_clients": 0, "closed_chats": 0})
        daily[day]["inbound_messages"] = int(row["inbound_messages"] or 0)
    for row in closed_rows:
        day = row["day"]
        daily.setdefault(day, {"date": day, "requests": 0, "inbound_messages": 0, "unique_chats": 0, "unique_clients": 0, "closed_chats": 0})
        daily[day]["closed_chats"] = int(row["closed_chats"] or 0)

    return [daily[key] for key in sorted(daily)]


def _fetch_hourly(conn: Any, cte_sql: str, params: list[Any]) -> list[dict[str, Any]]:
    rows = conn.execute(
        cte_sql
        + """
        SELECT
            local_hour AS hour,
            COUNT(*) AS requests,
            COUNT(DISTINCT chat_id) AS unique_chats,
            COUNT(DISTINCT client_key) AS unique_clients
        FROM daily_requests
        GROUP BY local_hour
        ORDER BY local_hour
        """,
        params,
    ).fetchall()

    by_hour = {
        int(row["hour"]): {
            "hour": int(row["hour"]),
            "requests": int(row["requests"] or 0),
            "inbound_messages": int(row["requests"] or 0),  # backward-compatible alias
            "unique_chats": int(row["unique_chats"] or 0),
            "unique_clients": int(row["unique_clients"] or 0),
        }
        for row in rows
        if row["hour"] is not None
    }
    return [by_hour.get(hour, {"hour": hour, "requests": 0, "inbound_messages": 0, "unique_chats": 0, "unique_clients": 0}) for hour in range(24)]


def _fetch_outside_hours(conn: Any, cte_sql: str, params: list[Any]) -> dict[str, Any]:
    row = conn.execute(
        cte_sql
        + """
        SELECT
            SUM(CASE WHEN local_hour < 10 THEN 1 ELSE 0 END) AS before_10_requests,
            SUM(CASE WHEN local_hour >= 19 THEN 1 ELSE 0 END) AS after_19_requests,
            COUNT(DISTINCT CASE WHEN local_hour < 10 THEN client_key END) AS before_10_clients,
            COUNT(DISTINCT CASE WHEN local_hour >= 19 THEN client_key END) AS after_19_clients
        FROM daily_requests
        """,
        params,
    ).fetchone()
    return _row_dict(row)



def _response_blocks_cte_sql(
    *,
    modifier: str,
    start: str,
    end: str,
    marketplace: str | None,
    hour_from: int | None = None,
    hour_to: int | None = None,
    include_hour_filter: bool = True,
) -> tuple[str, list[Any]]:
    """CTE for response-time calculation by dialogue blocks.

    A response block starts with the first inbound message after the previous
    outbound manager message in the same chat. Several consecutive inbound
    messages from the client before the manager replies are one block.

    Example:
    - 10:58 inbound -> 11:02 outbound = one answered block, 4 minutes.
    - 11:09 inbound -> 11:25 outbound = next answered block, 16 minutes.

    This is intentionally different from the KPI "обращение" definition used
    for peak hours (first inbound per chat per day). Response time must include
    every manager response cycle, not only the first daily request.
    """
    params: list[Any] = [modifier, modifier, modifier, start, end]
    marketplace_sql = _marketplace_filter_sql("c", marketplace, params)
    hour_sql = _hour_filter_sql("request_hour", hour_from, hour_to, params) if include_hour_filter else ""

    return f"""
        WITH ordered_messages AS (
            SELECT
                m.id,
                m.chat_id,
                m.direction,
                m.created_at,
                c.marketplace,
                c.external_chat_id,
                c.status AS chat_status,
                COALESCE(NULLIF(c.customer_public_id, ''), c.marketplace || ':' || c.external_chat_id) AS client_key,
                COALESCE(
                    SUM(CASE WHEN m.direction='outbound' THEN 1 ELSE 0 END) OVER (
                        PARTITION BY m.chat_id
                        ORDER BY julianday(m.created_at), m.id
                        ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
                    ),
                    0
                ) AS outbound_before
            FROM messages m
            JOIN chats c ON c.id = m.chat_id
            WHERE m.direction IN ('inbound', 'outbound')
              AND (m.direction != 'inbound' OR {_analytics_client_message_sql('m')})
        ),
        inbound_blocks_all AS (
            SELECT
                chat_id,
                outbound_before AS response_block_id,
                MIN(created_at) AS request_at,
                MIN(id) AS first_inbound_id,
                COUNT(*) AS inbound_messages_in_block
            FROM ordered_messages
            WHERE direction='inbound'
            GROUP BY chat_id, outbound_before
        ),
        response_blocks AS (
            SELECT
                b.chat_id,
                b.response_block_id,
                b.request_at,
                b.first_inbound_id,
                b.inbound_messages_in_block,
                c.marketplace,
                c.external_chat_id,
                c.status AS chat_status,
                COALESCE(NULLIF(c.customer_public_id, ''), c.marketplace || ':' || c.external_chat_id) AS client_key,
                date(datetime(b.request_at, ?)) AS local_day,
                CAST(strftime('%H', datetime(b.request_at, ?)) AS INTEGER) AS request_hour,
                (
                    SELECT MIN(o.created_at)
                    FROM messages o
                    WHERE o.chat_id = b.chat_id
                      AND o.direction = 'outbound'
                      AND julianday(o.created_at) > julianday(b.request_at)
                ) AS response_at
            FROM inbound_blocks_all b
            JOIN chats c ON c.id = b.chat_id
            WHERE date(datetime(b.request_at, ?)) BETWEEN ? AND ?
              {marketplace_sql}
              {hour_sql}
        )
    """, params

def _fetch_response_stats(conn: Any, cte_sql: str, params: list[Any]) -> dict[str, Any]:
    row = conn.execute(
        cte_sql
        + """
        SELECT
            COUNT(*) AS response_blocks,
            SUM(CASE WHEN response_at IS NOT NULL THEN 1 ELSE 0 END) AS answered_response_blocks,
            SUM(CASE WHEN response_at IS NULL AND chat_status != 'closed' THEN 1 ELSE 0 END) AS unanswered_response_blocks,
            SUM(CASE WHEN response_at IS NULL AND chat_status = 'closed' THEN 1 ELSE 0 END) AS closed_without_response_blocks,
            SUM(inbound_messages_in_block) AS inbound_messages_in_response_blocks,
            AVG(CASE WHEN response_at IS NOT NULL THEN (julianday(response_at) - julianday(request_at)) * 86400.0 END) AS avg_response_seconds,
            MIN(CASE WHEN response_at IS NOT NULL THEN (julianday(response_at) - julianday(request_at)) * 86400.0 END) AS min_response_seconds,
            MAX(CASE WHEN response_at IS NOT NULL THEN (julianday(response_at) - julianday(request_at)) * 86400.0 END) AS max_response_seconds
        FROM response_blocks
        """,
        params,
    ).fetchone()
    return _row_dict(row)


def _fetch_closed_count(conn: Any, closed_where_sql: str, closed_params: list[Any]) -> int:
    row = conn.execute(f"SELECT COUNT(*) AS closed_chats FROM chats c WHERE {closed_where_sql}", closed_params).fetchone()
    return int((_row_dict(row).get("closed_chats") or 0))


def _fetch_marketplace_breakdown(conn: Any, modifier: str, start: str, end: str) -> list[dict[str, Any]]:
    cte_sql, params = _daily_requests_cte_sql(
        modifier=modifier,
        start=start,
        end=end,
        marketplace=None,
        include_hour_filter=False,
    )
    rows = conn.execute(
        cte_sql
        + """
        SELECT
            marketplace,
            COUNT(*) AS requests,
            COUNT(DISTINCT chat_id) AS unique_chats,
            COUNT(DISTINCT client_key) AS unique_clients
        FROM daily_requests
        GROUP BY marketplace
        ORDER BY requests DESC
        """,
        params,
    ).fetchall()
    return [dict(row) for row in rows]


def _top_hours(hourly: list[dict[str, Any]]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
    max_count = max((int(row.get("requests") or 0) for row in hourly), default=0)
    if max_count <= 0:
        return None, []
    top = [row for row in hourly if int(row.get("requests") or 0) == max_count][:5]
    return top[0], top


def build_chat_analytics(
    *,
    date_from: str | None = None,
    date_to: str | None = None,
    marketplace: str | None = None,
    hour_from: int | None = None,
    hour_to: int | None = None,
    tz_offset_minutes: int | None = None,
) -> dict[str, Any]:
    """Return chat analytics dashboard data.

    v87 business definitions:
    - Request / обращение = first inbound client message in a chat on a local day.
    - Repeated inbound messages in the same chat on the same day are load, not new requests.
    - Average response time is calculated by dialogue response blocks: first inbound
      after previous manager reply -> next outbound manager reply. This counts
      every response cycle in the chat, not only the first daily request.
    - Unanswered response blocks are counted only for non-closed chats.
    - Peak hours use the first-daily-request definition.
    """
    offset = int(
        tz_offset_minutes
        if tz_offset_minutes is not None
        else _env_int("CRM_ANALYTICS_TZ_OFFSET_MINUTES", 180, minimum=-720, maximum=840)
    )
    default_from, default_to = _period_defaults(offset)
    start = _date_or_default(date_from, default_from)
    end = _date_or_default(date_to, default_to)
    if start > end:
        start, end = end, start

    market = str(marketplace or "").strip().lower() or None
    h_from = _hour_or_none(hour_from)
    h_to = _hour_or_none(hour_to)
    modifier = _tz_modifier(offset)

    request_cte, request_params = _daily_requests_cte_sql(
        modifier=modifier,
        start=start,
        end=end,
        marketplace=market,
        hour_from=h_from,
        hour_to=h_to,
        include_hour_filter=True,
    )
    hourly_cte, hourly_params = _daily_requests_cte_sql(
        modifier=modifier,
        start=start,
        end=end,
        marketplace=market,
        include_hour_filter=False,
    )
    outside_cte, outside_params = _daily_requests_cte_sql(
        modifier=modifier,
        start=start,
        end=end,
        marketplace=market,
        include_hour_filter=False,
    )
    response_cte, response_params = _response_blocks_cte_sql(
        modifier=modifier,
        start=start,
        end=end,
        marketplace=market,
        hour_from=h_from,
        hour_to=h_to,
        include_hour_filter=True,
    )
    raw_where, raw_params = _raw_inbound_where_sql(
        modifier=modifier,
        start=start,
        end=end,
        marketplace=market,
        hour_from=h_from,
        hour_to=h_to,
    )
    closed_where, closed_params = _closed_filter_sql(modifier=modifier, start=start, end=end, marketplace=market)

    with get_connection() as conn:
        summary = _fetch_summary(conn, request_cte, request_params)
        raw_inbound_messages = _fetch_raw_inbound_count(conn, raw_where, raw_params)
        excluded_service_messages = _fetch_excluded_service_message_count(
            conn,
            modifier=modifier,
            start=start,
            end=end,
            marketplace=market,
            hour_from=h_from,
            hour_to=h_to,
        )
        response = _fetch_response_stats(conn, response_cte, response_params)
        closed_chats = _fetch_closed_count(conn, closed_where, closed_params)
        outside = _fetch_outside_hours(conn, outside_cte, outside_params)
        daily = _fetch_daily(conn, request_cte, request_params, raw_where, raw_params, closed_where, closed_params, modifier)
        hourly = _fetch_hourly(conn, hourly_cte, hourly_params)
        marketplace_breakdown = _fetch_marketplace_breakdown(conn, modifier, start, end)

    peak_hour, top_hours = _top_hours(hourly)
    before_10_requests = int(outside.get("before_10_requests") or 0)
    after_19_requests = int(outside.get("after_19_requests") or 0)
    requests_count = int(summary.get("requests") or 0)
    response_blocks = int(response.get("response_blocks") or 0)
    answered_response_blocks = int(response.get("answered_response_blocks") or 0)
    unanswered_response_blocks = int(response.get("unanswered_response_blocks") or 0)
    closed_without_response_blocks = int(response.get("closed_without_response_blocks") or 0)

    return {
        "ok": True,
        "filters": {
            "date_from": start,
            "date_to": end,
            "marketplace": market or "",
            "hour_from": h_from,
            "hour_to": h_to,
            "tz_offset_minutes": offset,
        },
        "definitions": {
            "request": "Первое реальное входящее сообщение клиента в конкретном чате за локальный день; сообщения ботов/поддержки/системы маркетплейсов исключаются",
            "raw_inbound_message": "Любое реальное входящее сообщение клиента; сообщения ботов/поддержки/системы маркетплейсов исключаются",
            "average_response": "Время от первого входящего сообщения в очередном диалоговом блоке до ближайшего следующего исходящего ответа менеджера",
            "response_block": "Цепочка входящих сообщений клиента после предыдущего ответа менеджера; закрывается первым следующим исходящим ответом",
            "unanswered_response_block": "Ожидающий ответа блок считается без ответа только если чат не находится в статусе closed",
            "peak_hour": "Час первого дневного обращения клиента, а не каждое повторное сообщение",
            "closed": "Чаты со статусом closed и последней активностью в выбранном периоде",
            "timezone": "Группировка по дням/часам выполняется с фиксированным смещением tz_offset_minutes",
        },
        "summary": {
            "requests": requests_count,
            "daily_first_requests": requests_count,
            "inbound_messages": raw_inbound_messages,
            "raw_inbound_messages": raw_inbound_messages,
            "excluded_service_messages": excluded_service_messages,
            "unique_chats": int(summary.get("unique_chats") or 0),
            "unique_clients": int(summary.get("unique_clients") or 0),
            "closed_chats": closed_chats,
            "before_10_requests": before_10_requests,
            "after_19_requests": after_19_requests,
            "outside_hours_requests": before_10_requests + after_19_requests,
            # Backward-compatible aliases for the v85 UI/API shape.
            "before_10_messages": before_10_requests,
            "after_19_messages": after_19_requests,
            "outside_hours_messages": before_10_requests + after_19_requests,
            "before_10_clients": int(outside.get("before_10_clients") or 0),
            "after_19_clients": int(outside.get("after_19_clients") or 0),
            "response_blocks": response_blocks,
            "answered_response_blocks": answered_response_blocks,
            "unanswered_response_blocks": unanswered_response_blocks,
            "closed_without_response_blocks": closed_without_response_blocks,
            "inbound_messages_in_response_blocks": int(response.get("inbound_messages_in_response_blocks") or 0),
            # Backward-compatible aliases for the v85/v86 UI/API shape.
            "answered_requests": answered_response_blocks,
            "unanswered_requests": unanswered_response_blocks,
            "answered_inbound_messages": answered_response_blocks,
            "unanswered_inbound_messages": unanswered_response_blocks,
            "avg_response_seconds": _round_seconds(response.get("avg_response_seconds")),
            "min_response_seconds": _round_seconds(response.get("min_response_seconds")),
            "max_response_seconds": _round_seconds(response.get("max_response_seconds")),
            "avg_first_response_seconds": _round_seconds(response.get("avg_response_seconds")),
            # Backward-compatible names for older frontend labels. These are response-block counts,
            # not unique chat counts. v91 accidentally referenced undefined local variables here,
            # which broke /api/analytics/chats.
            "answered_chats": answered_response_blocks,
            "unanswered_chats": unanswered_response_blocks,
            "peak_hour": peak_hour,
        },
        "daily": daily,
        "hourly": hourly,
        "top_hours": top_hours,
        "marketplace_breakdown": marketplace_breakdown,
    }
