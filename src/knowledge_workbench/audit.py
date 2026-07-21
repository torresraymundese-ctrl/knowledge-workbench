from __future__ import annotations

import json
import sqlite3
from typing import Any

from .utils import utc_now


def record_event(
    connection: sqlite3.Connection,
    event_type: str,
    entity_type: str,
    entity_id: str,
    *,
    actor: str = "system",
    details: dict[str, Any] | None = None,
) -> None:
    connection.execute(
        """
        INSERT INTO audit_log(
            event_type, entity_type, entity_id, actor, details_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            entity_type,
            entity_id,
            actor,
            json.dumps(details or {}, ensure_ascii=False, sort_keys=True),
            utc_now(),
        ),
    )

