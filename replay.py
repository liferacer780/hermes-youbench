"""Replay orchestration: every saved case × every chosen model, in parallel, in isolated sandboxes.

Children are launched through Hermes's public subagent lifecycle API (``ctx.subagent_lifecycle``),
the same host-owned path ``delegate_task`` uses, so approvals, credentials, budgets and cost
accounting all stay Hermes's. Each child gets its own unpacked copy of the case's starting
files; ``guard`` keeps its tool calls inside that copy.

Runs are resumable within the session: ``youbench_run`` waits up to a budget and returns, and
``youbench_report`` keeps collecting (and launching queued replays) until everything is graded.
"""

from __future__ import annotations

import logging
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

from . import gitsnap, grade, guard, report, store, usage

logger = logging.getLogger(__name__)

_LOCK = threading.RLock()
_ACTIVE: dict[str, dict[str, Any]] = {}  # run_id -> {"queue": [...jobs], "live": {key: (job, handle)}, "parallel": n}

TERMINAL = {"SUCCEEDED", "FAILED", "INTERRUPTED", "CANCELLED", "UNKNOWN"}

GOAL_TEMPLATE = """You are being benchmarked by YouBench on a real task a user once gave an AI coding agent.
Do the task exactly as you would for that user.

WORKSPACE: the project is at {sandbox}
It is a private copy made for this attempt, so edit freely. Keep every file path and command inside that
folder (relative paths resolve there). Do not ask questions: nobody will answer. Make reasonable
assumptions, finish the work, then reply with a short summary of what you changed.

TASK (the user's words):
{prompt}

{token}"""


def _slug(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "-", text).strip("-")[:60] or "model"


def parse_model(spec: str) -> tuple[str, Optional[str]]:
    """``model`` or ``model@provider`` → (model, provider)."""
    spec = spec.strip()
    if "@" in spec:
        model, provider = spec.rsplit("@", 1)
        return model.strip(), (provider.strip() or None)
    return spec, None


def _post(ctx: Any, handle_box: dict, text: str) -> None:
    """Live progress line in the chat (edited in place) + an event for the Desktop page."""
    try:
        if handle_box.get("msg") is None:
            handle_box["msg"] = ctx.post_message(text)
        else:
            handle_box["msg"].edit(text)
    except Exception:
        pass


def _broadcast(run_id: str) -> None:
    try:
        from hermes_cli.plugin_events import broadcast_plugin_event

        broadcast_plugin_event("youbench", "run.updated", {"run_id": run_id})
    except Exception:
        pass


# --- launching ----------------------------------------------------------------------------------

def start_run(ctx: Any, models: list[str], case_ids: list[str], parallel: int, session_id: Optional[str]) -> str:
    run_id = store.new_id("r")
    store.add_run(run_id, models, case_ids, session_id)
    jobs = []
    for cid in case_ids:
        for model in models:
            jobs.append({"run_id": run_id, "case_id": cid, "model": model})
            store.put_result({"run_id": run_id, "case_id": cid, "model": model, "status": "queued"})
    with _LOCK:
        _ACTIVE[run_id] = {"queue": jobs, "live": {}, "parallel": max(1, min(8, int(parallel or 4)))}
    _broadcast(run_id)
    return run_id


def _toolsets_for(ctx: Any) -> Optional[tuple[str, ...]]:
    return ("terminal", "file")


def _launch(ctx: Any, job: dict) -> Optional[Any]:
    from agent.subagent_lifecycle import SubagentLaunchRequest, SubagentLifecycleError

    case = store.get_case(job["case_id"])
    if not case:
        _fail(job, "case was deleted")
        return None
    sandbox = store.data_dir() / "runs" / job["run_id"] / case["id"] / _slug(job["model"])
    try:
        gitsnap.make_sandbox(Path(case["archive"]), sandbox)
    except Exception as exc:
        _fail(job, f"could not prepare sandbox: {exc}")
        return None
    token = f"{job['run_id']}-{case['id']}-{_slug(job['model']).lower()}"[:80]
    token = re.sub(r"[^a-z0-9_-]", "-", token.lower())
    guard.register_token(token, str(sandbox), case["repo_root"])
    prompt = guard.rewrite_repo_refs(case["prompt"], str(sandbox), case["repo_root"])
    goal = GOAL_TEMPLATE.format(sandbox=sandbox, prompt=prompt, token=f"[youbench:{token}]")
    model, provider = parse_model(job["model"])
    base = dict(goal=goal, role="leaf", model=model, provider=provider, correlation_id=f"youbench-{token}",
                metadata={"youbench_run": job["run_id"], "case": case["id"]})
    handle = None
    for toolsets in (_toolsets_for(ctx), None):
        try:
            handle = ctx.subagent_lifecycle.launch(SubagentLaunchRequest(allowed_toolsets=toolsets, **base))
            break
        except SubagentLifecycleError as exc:
            if toolsets and ("toolset" in str(exc).lower() or "broaden" in str(exc).lower()):
                continue
            guard.forget_token(token)
            _fail(job, str(exc))
            return None
    job.update(sandbox=str(sandbox), token=token, started=time.time())
    store.put_result({"run_id": job["run_id"], "case_id": case["id"], "model": job["model"],
                      "status": "running", "sandbox": str(sandbox)})
    return handle


def _fail(job: dict, error: str) -> None:
    store.put_result({"run_id": job["run_id"], "case_id": job["case_id"], "model": job["model"],
                      "status": "failed", "passed": 0, "score": 0, "error": error[:1000],
                      "reason": f"Could not run: {error[:300]}"})


# --- collecting ---------------------------------------------------------------------------------

def _finalize(ctx: Any, job: dict, handle: Any) -> None:
    service = ctx.subagent_lifecycle
    result = service.result(handle)
    state = getattr(result.terminal_state, "value", str(result.terminal_state))
    case = store.get_case(job["case_id"]) or {}
    sandbox = job.get("sandbox", "")
    child_sid = guard.child_session_for(job.get("token", ""))
    try:
        diff_stat, candidate = gitsnap.sandbox_diff(Path(sandbox)) if sandbox else ("", "")
    except Exception as exc:
        diff_stat, candidate = "", ""
        logger.debug("youbench: diff failed: %s", exc)
    store.put_result({"run_id": job["run_id"], "case_id": job["case_id"], "model": job["model"],
                      "status": "grading", "sandbox": sandbox})
    verdict = grade.grade(ctx, case, sandbox=sandbox, candidate=candidate,
                          summary=result.summary or result.error_message or "", status=state)
    used = usage.wait_for_usage(child_sid) or {}
    seconds = None
    if result.started_at and result.completed_at:
        seconds = round(result.completed_at - result.started_at, 1)
    store.put_result({
        "run_id": job["run_id"], "case_id": job["case_id"], "model": job["model"], "status": "graded",
        "passed": int(bool(verdict["passed"])), "score": verdict["score"], "reason": verdict["reason"],
        "check_ok": None if verdict["check_ok"] is None else int(verdict["check_ok"]),
        "check_output": verdict["check_output"], "cost_usd": used.get("cost_usd"),
        "tokens_in": used.get("tokens_in"), "tokens_out": used.get("tokens_out"),
        "steps": used.get("tool_calls"), "api_calls": used.get("api_calls") or (result.usage_metadata or {}).get("api_calls"),
        "seconds": seconds, "diff_stat": diff_stat[-2000:], "summary": (result.summary or "")[:4000],
        "sandbox": sandbox, "child_session": child_sid,
        "error": result.error_message if state != "SUCCEEDED" else None,
    })
    guard.forget_token(job.get("token", ""))


def pump(ctx: Any, run_id: str, budget_seconds: float) -> dict[str, Any]:
    """Launch queued replays, collect finished ones, until done or *budget_seconds* elapse."""
    with _LOCK:
        state = _ACTIVE.get(run_id)
    if state is None:
        # Process restarted or run from another session: anything not graded is lost.
        for r in store.results_for(run_id):
            if r["status"] in ("queued", "running", "grading"):
                store.put_result({**{k: r[k] for k in ("run_id", "case_id", "model")}, "status": "lost",
                                  "passed": 0, "error": "Replay was interrupted (Hermes restarted or a different session)."})
        store.set_run(run_id, status="done")
        return report.leaderboard(run_id)

    service = ctx.subagent_lifecycle
    deadline = time.time() + max(5.0, budget_seconds)
    box: dict[str, Any] = {}
    total = sum(1 for _ in store.results_for(run_id))
    while True:
        with _LOCK:
            while state["queue"] and len(state["live"]) < state["parallel"]:
                job = state["queue"].pop(0)
                handle = _launch(ctx, job)
                if handle is not None:
                    state["live"][f"{job['case_id']}|{job['model']}"] = (job, handle)
                _broadcast(run_id)
            live = list(state["live"].items())
        if not live and not state["queue"]:
            break
        for key, (job, handle) in live:
            status = service.status(handle)
            current = getattr(status.state, "value", str(status.state))
            if current in TERMINAL:
                try:
                    _finalize(ctx, job, handle)
                except Exception as exc:
                    logger.warning("youbench: finalize failed", exc_info=True)
                    _fail(job, f"grading failed: {exc}")
                with _LOCK:
                    state["live"].pop(key, None)
                _broadcast(run_id)
        done = sum(1 for r in store.results_for(run_id) if r["status"] in report.DONE_STATES)
        if box.get("last_done") != done:
            box["last_done"] = done
            _post(ctx, box, f"🏁 YouBench `{run_id}`: {done}/{total} replays graded…")
        if time.time() >= deadline:
            break
        if live:
            # Block briefly on the first live child instead of busy-polling.
            service.wait(live[0][1], timeout_seconds=2.0)

    with _LOCK:
        finished = not state["queue"] and not state["live"]
        if finished:
            _ACTIVE.pop(run_id, None)
    board = report.leaderboard(run_id)
    if finished:
        store.set_run(run_id, status="done")
        report.write_html(report.leaderboard(run_id))
        board = report.leaderboard(run_id)
    _broadcast(run_id)
    if finished:
        _post(ctx, box, f"🏁 YouBench `{run_id}`: all {total} replays graded ✓")
    return board


def is_active(run_id: str) -> bool:
    with _LOCK:
        return run_id in _ACTIVE
