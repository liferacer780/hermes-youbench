"""Durable storage for YouBench: cases, pending turns, runs and results.

Everything lives under the profile's plugin-data directory
(``<HERMES_HOME>/plugin-data/youbench/``): a SQLite database plus the case
archives, replay sandboxes and HTML reports. Nothing is written into the
user's repositories.
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Optional

PLUGIN_NAME = "youbench"

_LOCK = threading.RLock()
_CONN: Optional[sqlite3.Connection] = None
_CONN_KEY: Optional[str] = None

_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS turns (
    session_id   TEXT PRIMARY KEY,
    prompt       TEXT NOT NULL,
    repo_root    TEXT,
    start_tree   TEXT,
    model        TEXT,
    cost_before  REAL,
    started_at   REAL NOT NULL,
    finished_at  REAL,
    response     TEXT,
    cost_after   REAL,
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    tools_before INTEGER,
    tools_after  INTEGER
);
CREATE TABLE IF NOT EXISTS cases (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    prompt       TEXT NOT NULL,
    repo_root    TEXT NOT NULL,
    archive      TEXT NOT NULL,
    reference    TEXT,
    ref_stat     TEXT,
    check_cmd    TEXT,
    orig_model   TEXT,
    orig_cost    REAL,
    orig_seconds REAL,
    orig_steps   INTEGER,
    created_at   REAL NOT NULL,
    session_id   TEXT
);
CREATE TABLE IF NOT EXISTS runs (
    id          TEXT PRIMARY KEY,
    created_at  REAL NOT NULL,
    models      TEXT NOT NULL,
    case_ids    TEXT NOT NULL,
    status      TEXT NOT NULL,
    session_id  TEXT,
    report_path TEXT
);
CREATE TABLE IF NOT EXISTS results (
    run_id        TEXT NOT NULL,
    case_id       TEXT NOT NULL,
    model         TEXT NOT NULL,
    status        TEXT NOT NULL,
    passed        INTEGER,
    score         REAL,
    reason        TEXT,
    check_ok      INTEGER,
    check_output  TEXT,
    cost_usd      REAL,
    tokens_in     INTEGER,
    tokens_out    INTEGER,
    steps         INTEGER,
    api_calls     INTEGER,
    seconds       REAL,
    diff_stat     TEXT,
    summary       TEXT,
    sandbox       TEXT,
    child_session TEXT,
    error         TEXT,
    PRIMARY KEY (run_id, case_id, model)
);
"""


def data_dir() -> Path:
    """Per-profile data root (created on first use)."""
    from plugins.plugin_storage import plugin_data_dir

    path = Path(plugin_data_dir(PLUGIN_NAME))
    path.mkdir(parents=True, exist_ok=True)
    return path


def _conn() -> sqlite3.Connection:
    global _CONN, _CONN_KEY
    key = str(data_dir())
    with _LOCK:
        if _CONN is None or _CONN_KEY != key:
            from plugins.plugin_storage import plugin_db

            conn = plugin_db(PLUGIN_NAME)
            conn.row_factory = sqlite3.Row
            conn.executescript(_SCHEMA)
            conn.commit()
            _CONN, _CONN_KEY = conn, key
        return _CONN


def _exec(sql: str, params: tuple = ()) -> None:
    with _LOCK:
        conn = _conn()
        conn.execute(sql, params)
        conn.commit()


def _rows(sql: str, params: tuple = ()) -> list[dict[str, Any]]:
    with _LOCK:
        return [dict(r) for r in _conn().execute(sql, params).fetchall()]


def new_id(prefix: str) -> str:
    return f"{prefix}{uuid.uuid4().hex[:8]}"


# --- meta ----------------------------------------------------------------------------------------

def get_meta(key: str) -> Optional[str]:
    rows = _rows("SELECT value FROM meta WHERE key=?", (key,))
    return rows[0]["value"] if rows else None


def set_meta(key: str, value: Optional[str]) -> None:
    if value is None:
        _exec("DELETE FROM meta WHERE key=?", (key,))
    else:
        _exec("INSERT OR REPLACE INTO meta (key, value) VALUES (?,?)", (key, value))


# --- turns (one pending/last turn per session) ------------------------------------------------

def start_turn(session_id: str, prompt: str, repo_root: Optional[str], start_tree: Optional[str],
               model: str, cost_before: Optional[float], tools_before: Optional[int]) -> None:
    _exec(
        "INSERT OR REPLACE INTO turns (session_id, prompt, repo_root, start_tree, model, cost_before, "
        "started_at, tools_before) VALUES (?,?,?,?,?,?,?,?)",
        (session_id, prompt, repo_root, start_tree, model, cost_before, time.time(), tools_before),
    )


def finish_turn(session_id: str, response: str, cost_after: Optional[float], tools_after: Optional[int]) -> None:
    _exec(
        "UPDATE turns SET finished_at=?, response=?, cost_after=?, tools_after=? WHERE session_id=?",
        (time.time(), (response or "")[:4000], cost_after, tools_after, session_id),
    )


def get_turn(session_id: str) -> Optional[dict[str, Any]]:
    rows = _rows("SELECT * FROM turns WHERE session_id=?", (session_id,))
    return rows[0] if rows else None


def latest_finished_turn() -> Optional[dict[str, Any]]:
    rows = _rows("SELECT * FROM turns WHERE finished_at IS NOT NULL ORDER BY finished_at DESC LIMIT 1")
    return rows[0] if rows else None


# --- cases ---------------------------------------------------------------------------------------

def add_case(case: dict[str, Any]) -> None:
    cols = ", ".join(case)
    marks = ", ".join("?" for _ in case)
    _exec(f"INSERT INTO cases ({cols}) VALUES ({marks})", tuple(case.values()))


def list_cases() -> list[dict[str, Any]]:
    return _rows("SELECT * FROM cases ORDER BY created_at")


def get_case(case_id: str) -> Optional[dict[str, Any]]:
    rows = _rows("SELECT * FROM cases WHERE id=? OR name=?", (case_id, case_id))
    return rows[0] if rows else None


def delete_case(case_id: str) -> Optional[dict[str, Any]]:
    case = get_case(case_id)
    if case:
        _exec("DELETE FROM cases WHERE id=?", (case["id"],))
    return case


def update_case(case_id: str, **fields: Any) -> None:
    if not fields:
        return
    sets = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE cases SET {sets} WHERE id=?", (*fields.values(), case_id))


# --- runs & results ------------------------------------------------------------------------------

def add_run(run_id: str, models: list[str], case_ids: list[str], session_id: Optional[str]) -> None:
    _exec(
        "INSERT INTO runs (id, created_at, models, case_ids, status, session_id) VALUES (?,?,?,?,?,?)",
        (run_id, time.time(), json.dumps(models), json.dumps(case_ids), "running", session_id),
    )


def set_run(run_id: str, **fields: Any) -> None:
    sets = ", ".join(f"{k}=?" for k in fields)
    _exec(f"UPDATE runs SET {sets} WHERE id=?", (*fields.values(), run_id))


def get_run(run_id: Optional[str] = None) -> Optional[dict[str, Any]]:
    if run_id:
        rows = _rows("SELECT * FROM runs WHERE id=?", (run_id,))
    else:
        rows = _rows("SELECT * FROM runs ORDER BY created_at DESC LIMIT 1")
    if not rows:
        return None
    run = rows[0]
    run["models"] = json.loads(run["models"])
    run["case_ids"] = json.loads(run["case_ids"])
    return run


def list_runs(limit: int = 20) -> list[dict[str, Any]]:
    return _rows("SELECT id, created_at, models, case_ids, status FROM runs ORDER BY created_at DESC LIMIT ?", (limit,))


def put_result(row: dict[str, Any]) -> None:
    cols = ", ".join(row)
    marks = ", ".join("?" for _ in row)
    _exec(f"INSERT OR REPLACE INTO results ({cols}) VALUES ({marks})", tuple(row.values()))


def results_for(run_id: str) -> list[dict[str, Any]]:
    return _rows("SELECT * FROM results WHERE run_id=? ORDER BY model, case_id", (run_id,))
