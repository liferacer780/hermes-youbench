"""Turn capture and case saving.

``pre_llm_call`` records the user's prompt and a git tree of the workspace *before* the agent
touches it; ``post_llm_call`` marks the turn finished. ``save_case`` turns the latest finished
turn into a replayable benchmark case: the starting files (as an archive in YouBench's data
dir), the prompt, the reference change the user accepted, and what the original run cost.
"""

from __future__ import annotations

import logging
import re
import time
from pathlib import Path
from typing import Any, Optional

from . import gitsnap, guard, store, usage

logger = logging.getLogger(__name__)

_CTX: Any = None


def bind(ctx: Any) -> None:
    global _CTX
    _CTX = ctx


def _setting(key: str, default: Any) -> Any:
    try:
        value = _CTX.get_config(key, default=default) if _CTX is not None else default
    except Exception:
        return default
    return default if value is None else value


def _session_cwd() -> Optional[str]:
    if _CTX is not None:
        try:
            cwd = _CTX.session_cwd()
            if cwd:
                return str(cwd)
        except Exception:
            pass
    try:
        from agent.runtime_cwd import resolve_agent_cwd

        return str(resolve_agent_cwd())
    except Exception:
        return None


def _skip(session_id: str, platform: str) -> bool:
    return not session_id or platform == "subagent" or guard.is_replay_session(session_id)


# --- hooks --------------------------------------------------------------------------------------

def on_pre_llm_call(session_id: str = "", user_message: str = "", model: str = "", platform: str = "",
                    **_: Any) -> None:
    """Observer only: never injects context, never blocks."""
    if _skip(session_id, platform) or not (user_message or "").strip():
        return None
    if (user_message or "").lstrip().startswith(("/", "[YouBench]")):
        return None
    try:
        root = tree = None
        if bool(_setting("auto_capture", True)):
            cwd = _session_cwd()
            root = gitsnap.repo_root(cwd) if cwd else None
            if root:
                try:
                    tree = gitsnap.snapshot_tree(root)
                except gitsnap.GitError as exc:
                    logger.debug("youbench: snapshot skipped: %s", exc)
        before = usage.session_usage(session_id) or {}
        store.start_turn(session_id, user_message, root, tree, model or "",
                         before.get("cost_usd"), before.get("tool_calls"))
    except Exception:
        logger.debug("youbench: pre_llm_call capture failed", exc_info=True)
    return None


def on_post_llm_call(session_id: str = "", assistant_response: str = "", platform: str = "", **_: Any) -> None:
    if _skip(session_id, platform):
        return None
    try:
        after = usage.session_usage(session_id) or {}
        store.finish_turn(session_id, assistant_response or "", after.get("cost_usd"), after.get("tool_calls"))
    except Exception:
        logger.debug("youbench: post_llm_call capture failed", exc_info=True)
    return None


# --- saving -------------------------------------------------------------------------------------

def _slug(text: str, limit: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", (text or "").lower()).strip("-")
    return (slug[:limit].rstrip("-")) or "case"


def _unique_name(base: str) -> str:
    existing = {c["name"] for c in store.list_cases()}
    name, n = base, 2
    while name in existing:
        name, n = f"{base}-{n}", n + 1
    return name


class SaveError(RuntimeError):
    pass


def save_case(session_id: Optional[str], name: Optional[str] = None, check_cmd: Optional[str] = None) -> dict:
    turn = store.get_turn(session_id) if session_id else None
    if not turn or not turn.get("finished_at"):
        turn = store.latest_finished_turn()
    if not turn:
        raise SaveError("Nothing to save yet. Give Hermes a task in a git project first, let it finish, "
                        "then run /youbench save.")
    if not turn.get("repo_root") or not turn.get("start_tree"):
        raise SaveError("The last task did not run inside a git repository, so YouBench has no starting "
                        "snapshot to replay from. Open a git project in Hermes and try again.")
    root = turn["repo_root"]
    if not Path(root).is_dir():
        raise SaveError(f"The project folder {root} no longer exists.")

    end_tree = gitsnap.snapshot_tree(root)
    case_id = store.new_id("c")
    archive = store.data_dir() / "cases" / f"{case_id}.tar.gz"
    size = gitsnap.archive_tree(root, turn["start_tree"], archive)
    max_mb = int(_setting("max_snapshot_mb", 50) or 50)
    if size > max_mb * 1024 * 1024:
        archive.unlink(missing_ok=True)
        raise SaveError(f"This workspace is {size / 1e6:.0f} MB compressed, above the {max_mb} MB limit "
                        "(max_snapshot_mb). Add large build output to .gitignore or raise the limit.")
    ref_stat, reference = gitsnap.diff_trees(root, turn["start_tree"], end_tree)

    # A session's first turn has no usage row yet when the turn starts: "before" is zero then.
    cost = None
    if turn.get("cost_after") is not None:
        cost = max(0.0, float(turn["cost_after"]) - float(turn.get("cost_before") or 0.0))
    steps = None
    if turn.get("tools_after") is not None:
        steps = max(0, int(turn["tools_after"]) - int(turn.get("tools_before") or 0))

    case = {
        "id": case_id,
        "name": _unique_name(_slug(name) if name else _slug(turn["prompt"])),
        "prompt": turn["prompt"],
        "repo_root": root,
        "archive": str(archive),
        "reference": reference,
        "ref_stat": ref_stat,
        "check_cmd": (check_cmd or "").strip() or None,
        "orig_model": turn.get("model") or None,
        "orig_cost": cost,
        "orig_seconds": round(float(turn["finished_at"]) - float(turn["started_at"]), 1),
        "orig_steps": steps,
        "created_at": time.time(),
        "session_id": turn["session_id"],
    }
    store.add_case(case)
    return case
