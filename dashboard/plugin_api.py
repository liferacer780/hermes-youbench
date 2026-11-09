"""YouBench backend, mounted at /api/plugins/youbench/ (read-only views for the Desktop page)."""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

from fastapi import APIRouter
from fastapi.responses import HTMLResponse

router = APIRouter()

_PKG_NAME = "_youbench_dashboard_pkg"


def _pkg():
    """Load this plugin's own package (store/report) from the folder above, once."""
    mod = sys.modules.get(_PKG_NAME)
    if mod is not None:
        return mod
    root = Path(__file__).resolve().parent.parent
    spec = importlib.util.spec_from_file_location(_PKG_NAME, root / "__init__.py",
                                                  submodule_search_locations=[str(root)])
    mod = importlib.util.module_from_spec(spec)
    sys.modules[_PKG_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


def _case_row(c: dict) -> dict:
    return {"id": c["id"], "name": c["name"], "prompt": c["prompt"][:240], "check": c["check_cmd"],
            "orig_model": c["orig_model"], "orig_seconds": c["orig_seconds"], "orig_cost": c["orig_cost"],
            "changes": ((c["ref_stat"] or "").splitlines() or [""])[-1].strip(), "created_at": c["created_at"]}


@router.get("/state")
async def state():
    pkg = _pkg()
    cases = [_case_row(c) for c in pkg.store.list_cases()]
    run = pkg.store.get_run()
    board = pkg.report.board_json(pkg.report.leaderboard(run["id"])) if run else None
    runs = [{"id": r["id"], "created_at": r["created_at"], "status": r["status"]} for r in pkg.store.list_runs(10)]
    return {"cases": cases, "board": board, "runs": runs}


@router.get("/runs/{run_id}")
async def run_board(run_id: str):
    pkg = _pkg()
    return pkg.report.board_json(pkg.report.leaderboard(run_id))


@router.get("/report/{run_id}", response_class=HTMLResponse)
async def html_report(run_id: str):
    pkg = _pkg()
    board = pkg.report.leaderboard(run_id)
    if "error" in board:
        return HTMLResponse(board["error"], status_code=404)
    return HTMLResponse(pkg.report.html_report(board))
