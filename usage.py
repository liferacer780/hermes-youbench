"""Read-only usage lookups in Hermes's own session database (``<HERMES_HOME>/state.db``).

YouBench never writes to state.db. It reads a session row's cost, token and tool counters so the
leaderboard can show what each replay (and the original turn) cost.
"""

from __future__ import annotations

import sqlite3
import time
from pathlib import Path
from typing import Any, Optional


def _db_path() -> Path:
    from hermes_constants import get_hermes_home

    return Path(get_hermes_home()) / "state.db"


def session_usage(session_id: Optional[str]) -> Optional[dict[str, Any]]:
    """Cost/tokens/tool counters for one session row, or None."""
    if not session_id:
        return None
    path = _db_path()
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True, timeout=2)
    except sqlite3.Error:
        return None
    try:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
        if row is None:
            return None
        data = dict(row)
    except sqlite3.Error:
        return None
    finally:
        conn.close()
    cost = data.get("actual_cost_usd")
    if cost is None:
        cost = data.get("estimated_cost_usd")
    return {
        "cost_usd": float(cost) if cost is not None else None,
        "tokens_in": int(data.get("input_tokens") or 0),
        "tokens_out": int(data.get("output_tokens") or 0),
        "tool_calls": int(data.get("tool_call_count") or 0),
        "api_calls": int(data.get("api_call_count") or 0),
        "model": data.get("model"),
    }


def wait_for_usage(session_id: Optional[str], attempts: int = 6, delay: float = 0.5) -> Optional[dict[str, Any]]:
    """A finished child flushes its row asynchronously; retry briefly."""
    usage = None
    for _ in range(attempts):
        usage = session_usage(session_id)
        if usage and (usage["api_calls"] or usage["tokens_in"]):
            return usage
        time.sleep(delay)
    return usage
