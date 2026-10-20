from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from analystbot.core.config import Settings
from analystbot.core.log_config import setup_logger
from analystbot.db import StockRepository, connect
from analystbot.db.date_utils import unix_to_date_str

logger = setup_logger("services.session_delivery")

SESSION_SEND_FILENAME = "morning_send_state.json"
MARKET_SEND_ITEMS = ("vixfix", "supertrend", "heatmap")


def get_session_send_state_path(settings: Settings):
    return settings.data_dir / SESSION_SEND_FILENAME


def latest_session_date(settings: Settings) -> str | None:
    conn = connect(settings.db_path)
    try:
        timestamp = StockRepository(conn).get_latest_session_timestamp()
    finally:
        conn.close()
    if timestamp is None:
        return None
    return unix_to_date_str(timestamp)


def read_session_send_state(settings: Settings) -> dict[str, Any]:
    path = get_session_send_state_path(settings)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        logger.warning("Ignoring invalid session send state: %s", path)
        return {}
    if not isinstance(data, dict):
        return {}
    return data


def already_sent(
    settings: Settings,
    item: str,
    session_date: str | None = None,
) -> bool:
    session_date = session_date or latest_session_date(settings)
    if session_date is None:
        return False
    state = read_session_send_state(settings)
    if state.get("session_date") != session_date:
        return False
    sent = state.get("sent") or []
    return item in sent


def mark_sent(
    settings: Settings,
    item: str,
    session_date: str | None = None,
) -> None:
    session_date = session_date or latest_session_date(settings)
    if session_date is None:
        return
    state = read_session_send_state(settings)
    if state.get("session_date") != session_date:
        sent: list[str] = []
    else:
        sent = [str(value) for value in (state.get("sent") or [])]
    if item not in sent:
        sent.append(item)
    payload = {
        "session_date": session_date,
        "sent": sent,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    path = get_session_send_state_path(settings)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
