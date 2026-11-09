"""YouBench: benchmark models on your own work.

Save real tasks from your Hermes sessions (``/youbench save``), replay them on any models in
isolated sandboxes (``youbench_run``), and get a leaderboard of pass rate, quality, cost, time
and steps for the work you actually do.
"""

from __future__ import annotations

import json
import logging
import shlex
from typing import Any

from . import capture, guard, replay, report, schemas, store

logger = logging.getLogger(__name__)

_CTX: Any = None


def _j(obj: Any) -> str:
    return json.dumps(obj, default=str, ensure_ascii=False)


def _setting(key: str, default: Any) -> Any:
    try:
        value = _CTX.get_config(key, default=default)
    except Exception:
        return default
    return default if value is None else value


# --- tools --------------------------------------------------------------------------------------

def _resolve_cases(wanted: list[str] | None, limit: int | None) -> list[dict]:
    cases = store.list_cases()
    if wanted:
        picked = []
        for w in wanted:
            c = store.get_case(str(w))
            if c and c not in picked:
                picked.append(c)
        cases = picked
    if limit:
        cases = cases[-int(limit):]
    return cases


def tool_run(args: dict, **kwargs: Any) -> str:
    try:
        models = [m for m in (args.get("models") or _setting("default_models", []) or []) if str(m).strip()]
        if not models:
            return _j({"error": "Name at least one model, e.g. models=['anthropic/claude-sonnet-4.6', "
                                "'nousresearch/hermes-4-70b'], or set default_models in the YouBench settings."})
        cases = _resolve_cases(args.get("cases"), args.get("limit"))
        if not cases:
            return _j({"error": "No saved cases yet. Do a real task in a git project, then type "
                                "/youbench save to turn it into a benchmark case."})
        run_id = replay.start_run(_CTX, [str(m) for m in models], [c["id"] for c in cases],
                                  int(_setting("parallel", 4) or 4), kwargs.get("session_id"))
        board = replay.pump(_CTX, run_id, float(args.get("wait_seconds") or 300))
        return _result(board)
    except Exception as exc:
        logger.warning("youbench_run failed", exc_info=True)
        return _j({"error": f"youbench_run failed: {exc}"})


def tool_report(args: dict, **kwargs: Any) -> str:
    try:
        run = store.get_run(args.get("run_id"))
        if not run:
            return _j({"error": "No YouBench runs yet. Use youbench_run first."})
        if run["status"] != "done":
            board = replay.pump(_CTX, run["id"], float(args.get("wait_seconds") or 240))
        else:
            board = report.leaderboard(run["id"])
        return _result(board)
    except Exception as exc:
        logger.warning("youbench_report failed", exc_info=True)
        return _j({"error": f"youbench_report failed: {exc}"})


def _result(board: dict) -> str:
    if "error" in board:
        return _j(board)
    out = {
        "run_id": board["run"]["id"],
        "complete": board["all_done"],
        "leaderboard_markdown": report.markdown(board),
        "failures": report.failures(board),
        "html_report": board["run"].get("report_path"),
        "next": ("Show leaderboard_markdown to the user as-is, then one sentence of advice."
                 if board["all_done"] else
                 f"Replays still running: call youbench_report with run_id='{board['run']['id']}'."),
    }
    return _j(out)


def tool_cases(args: dict, **kwargs: Any) -> str:
    return _j({"cases": [_case_brief(c) for c in store.list_cases()]})


def _case_brief(c: dict) -> dict:
    return {"id": c["id"], "name": c["name"], "prompt": c["prompt"][:200], "check": c["check_cmd"],
            "original_model": c["orig_model"], "files_changed": (c["ref_stat"] or "").splitlines()[-1:] or []}


# --- slash command ------------------------------------------------------------------------------

HELP = """**YouBench**: benchmark models on your own work.

- `/youbench project <folder>`: the git project YouBench watches when the session's own folder is not one
- `/youbench save [name] [--check "<test command>"]`: save the task Hermes just finished as a benchmark case
- `/youbench list`: your saved cases
- `/youbench check <case> <command>`: set the test command that decides pass/fail for a case
- `/youbench run <model> [<model> ...]`: replay every case on these models and rank them
- `/youbench report [run]`: show the latest (or a given) leaderboard
- `/youbench delete <case>`: remove a case

Tip: a check command like `pytest -q` or `npm test` makes grading objective."""


def _cmd_save(rest: list[str], session_id: str) -> str:
    check, name_parts, i = None, [], 0
    while i < len(rest):
        if rest[i] in ("--check", "-c") and i + 1 < len(rest):
            check, i = rest[i + 1], i + 2
            continue
        name_parts.append(rest[i])
        i += 1
    try:
        case = capture.save_case(session_id or None, " ".join(name_parts) or None, check)
    except capture.SaveError as exc:
        return f"⚠️ {exc}"
    except Exception as exc:
        logger.warning("youbench save failed", exc_info=True)
        return f"⚠️ Could not save: {exc}"
    replay._broadcast("cases")
    changed = (case["ref_stat"] or "no file changes").splitlines()[-1].strip()
    lines = [f"✅ Saved case **{case['name']}** (`{case['id']}`)",
             f"> {case['prompt'][:160]}{'…' if len(case['prompt']) > 160 else ''}",
             f"Starting files snapshotted · reference change: {changed}"]
    if case["orig_model"]:
        lines.append(f"Original run: `{case['orig_model']}` in {case['orig_seconds']:.0f}s")
    lines.append(f"Graded by: {'`' + case['check_cmd'] + '`' if case['check_cmd'] else 'judge (add --check for tests)'}")
    lines.append(f"You now have {len(store.list_cases())} case(s). Run `/youbench run <model> <model>` to compare.")
    return "\n".join(lines)


def _cmd_project(path: str) -> str:
    from . import gitsnap

    path = path.strip().strip('"').strip("'")
    if not path:
        current = store.get_meta("project")
        return (f"YouBench is watching `{current}` when a session's own folder is not a git project."
                if current else "Usage: `/youbench project <folder>` (a git project folder). `/youbench project off` to clear.")
    if path.lower() in ("off", "none", "clear"):
        store.set_meta("project", None)
        return "YouBench now only watches each session's own folder."
    root = gitsnap.repo_root(path)
    if not root:
        return (f"⚠️ `{path}` is not inside a git project. Run `git init` there first "
                "(YouBench needs git to snapshot your starting files).")
    store.set_meta("project", root)
    return (f"✅ YouBench is now watching **{root}**. Give Hermes a task in this project, then "
            "`/youbench save --check \"<test command>\"` when it finishes.")


def _cmd_list() -> str:
    cases = store.list_cases()
    if not cases:
        return "No cases yet. Finish a task in a git project, then `/youbench save`."
    lines = [f"**{len(cases)} YouBench case(s)**", "", "| Case | Task | Graded by | Original model |", "|---|---|---|---|"]
    for c in cases:
        prompt = c["prompt"].replace("\n", " ").replace("|", "\\|")[:70]
        lines.append(f"| `{c['name']}` | {prompt} | {('`' + c['check_cmd'] + '`') if c['check_cmd'] else 'judge'} | "
                     f"{c['orig_model'] or 'n/a'} |")
    return "\n".join(lines)


def _cmd_run(rest: list[str], session_key: str) -> str:
    models = [m for m in rest if m.strip()] or list(_setting("default_models", []) or [])
    if not models:
        return "Usage: `/youbench run <model> [<model> ...]` (e.g. `/youbench run anthropic/claude-sonnet-4.6 nousresearch/hermes-4-70b`)"
    if not store.list_cases():
        return "No cases yet. Finish a task in a git project, then `/youbench save`."
    message = (f"[YouBench] Run youbench_run with models={json.dumps(models)} on all saved cases. "
               "Keep calling youbench_report until complete, then show me the leaderboard table as returned "
               "and tell me which model to use.")
    try:
        if _CTX.inject_message(message, session_key=session_key or None):
            return f"🏁 Starting YouBench: {len(store.list_cases())} case(s) × {len(models)} model(s)…"
    except Exception:
        logger.debug("youbench: inject_message failed", exc_info=True)
    return ("Ask Hermes: *“Run YouBench on " + ", ".join(models) + "”*. Benchmarks run inside an agent turn.")


def _cmd_report(rest: list[str]) -> str:
    run = store.get_run(rest[0] if rest else None)
    if not run:
        return "No YouBench runs yet."
    board = report.leaderboard(run["id"])
    text = report.markdown(board)
    fails = report.failures(board)
    if fails:
        text += "\n\n**Misses**\n" + "\n".join(fails)
    if run.get("report_path"):
        text += f"\n\nFull report: `{run['report_path']}`"
    return text


_PLAIN_SURFACES = {"desktop", "cli", "tui", "local", "acp"}


def _plain(text: str) -> str:
    """Desktop/CLI show command replies as plain text: drop markdown emphasis and code ticks."""
    return text.replace("**", "").replace("`", "")


def handle_command(raw_args: str = "", *, session_id: str = "", session_key: str = "", platform: str = "",
                   **kw: Any) -> str:
    text = _handle_command(raw_args, session_id=session_id, session_key=session_key)
    return _plain(text) if (platform or "local") in _PLAIN_SURFACES else text


def _handle_command(raw_args: str = "", *, session_id: str = "", session_key: str = "") -> str:
    try:
        parts = shlex.split(raw_args or "", posix=True)
    except ValueError:
        parts = (raw_args or "").split()
    if not parts or parts[0] in ("help", "-h", "--help"):
        return HELP
    sub, rest = parts[0].lower(), parts[1:]
    try:
        if sub == "save":
            return _cmd_save(rest, session_id)
        if sub == "project":
            return _cmd_project(raw_args.strip()[len(parts[0]):].strip())
        if sub in ("list", "ls", "cases"):
            return _cmd_list()
        if sub == "run":
            return _cmd_run(rest, session_key)
        if sub in ("report", "board", "leaderboard"):
            return _cmd_report(rest)
        if sub == "check" and len(rest) >= 2:
            case = store.get_case(rest[0])
            if not case:
                return f"No case `{rest[0]}`."
            store.update_case(case["id"], check_cmd=" ".join(rest[1:]))
            return f"✅ `{case['name']}` will be graded by `{' '.join(rest[1:])}`."
        if sub in ("delete", "rm") and rest:
            case = store.delete_case(rest[0])
            replay._broadcast("cases")
            return f"🗑️ Deleted `{case['name']}`." if case else f"No case `{rest[0]}`."
    except Exception as exc:
        logger.warning("youbench command failed", exc_info=True)
        return f"⚠️ {exc}"
    return HELP


# --- CLI: hermes youbench ... -------------------------------------------------------------------

def _cli_setup(parser) -> None:
    subs = parser.add_subparsers(dest="youbench_command")
    subs.add_parser("list", help="List saved cases")
    rep = subs.add_parser("report", help="Print the latest leaderboard")
    rep.add_argument("run_id", nargs="?")
    rep.add_argument("--html", action="store_true", help="Write the HTML report and print its path")
    rm = subs.add_parser("delete", help="Delete a case")
    rm.add_argument("case")
    parser.set_defaults(func=_cli_handle)


def _cli_handle(args) -> None:
    sub = getattr(args, "youbench_command", None)
    if sub == "list":
        print(_cmd_list())
    elif sub == "report":
        run = store.get_run(getattr(args, "run_id", None))
        if not run:
            print("No YouBench runs yet.")
            return
        board = report.leaderboard(run["id"])
        print(report.markdown(board))
        if getattr(args, "html", False):
            print(f"\nHTML report: {report.write_html(board)}")
    elif sub == "delete":
        case = store.delete_case(args.case)
        print(f"Deleted {case['name']}" if case else f"No case {args.case}")
    else:
        print(HELP)


# --- registration -------------------------------------------------------------------------------

def register(ctx) -> None:
    global _CTX
    _CTX = ctx
    capture.bind(ctx)

    ctx.register_tool(name="youbench_run", toolset="youbench", schema=schemas.YOUBENCH_RUN, handler=tool_run,
                      emoji="🏁", description="Benchmark models on your saved tasks")
    ctx.register_tool(name="youbench_report", toolset="youbench", schema=schemas.YOUBENCH_REPORT,
                      handler=tool_report, emoji="📊", description="Collect a YouBench leaderboard")
    ctx.register_tool(name="youbench_cases", toolset="youbench", schema=schemas.YOUBENCH_CASES,
                      handler=tool_cases, emoji="🗂️", description="List saved YouBench cases")

    ctx.register_hook("pre_llm_call", capture.on_pre_llm_call)
    ctx.register_hook("post_llm_call", capture.on_post_llm_call)
    ctx.register_hook("subagent_start", guard.on_subagent_start)
    ctx.register_hook("pre_tool_call", guard.on_pre_tool_call)

    ctx.register_command("youbench", handle_command, description="Benchmark models on your own tasks",
                         args_hint="save|list|run|report|check|delete")
    ctx.register_cli_command(name="youbench", help="Benchmark models on your own tasks",
                             setup_fn=_cli_setup, handler_fn=_cli_handle)
