from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

from analystbot.core.config import Settings
from analystbot.core.json_state import write_json_atomically
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
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        logger.warning("Ignoring invalid session send state: %s", path)
        return {}
    if not isinstance(data, dict):
        return {}
    sent = data.get("sent")
    if not isinstance(sent, list) or not all(isinstance(item, str) for item in sent):
        logger.warning("Ignoring malformed session send state: %s", path)
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
    return item in state["sent"]


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
        sent = state["sent"]
    if item not in sent:
        sent.append(item)
    payload = {
        "session_date": session_date,
        "sent": sent,
        "timestamp": datetime.now(UTC).isoformat(),
    }
    write_json_atomically(get_session_send_state_path(settings), payload)
