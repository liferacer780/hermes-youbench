"""Leaderboard maths and rendering (chat markdown + a standalone HTML report)."""

from __future__ import annotations

import html
import json
import time
from pathlib import Path
from typing import Any, Optional

from . import store

DONE_STATES = {"graded", "failed", "lost"}


def leaderboard(run_id: str) -> dict[str, Any]:
    run = store.get_run(run_id)
    if not run:
        return {"error": f"Unknown run {run_id}"}
    results = store.results_for(run_id)
    cases = {cid: store.get_case(cid) for cid in run["case_ids"]}
    rows: list[dict[str, Any]] = []
    for model in run["models"]:
        mine = [r for r in results if r["model"] == model]
        done = [r for r in mine if r["status"] in DONE_STATES]
        passed = sum(1 for r in done if r["passed"])
        costs = [r["cost_usd"] for r in done if r["cost_usd"] is not None]
        secs = [r["seconds"] for r in done if r["seconds"] is not None]
        steps = [r["steps"] for r in done if r["steps"] is not None]
        scores = [r["score"] for r in done if r["score"] is not None]
        rows.append({
            "model": model,
            "done": len(done),
            "total": len(run["case_ids"]),
            "passed": passed,
            "pass_rate": (passed / len(done)) if done else None,
            "avg_score": (sum(scores) / len(scores)) if scores else None,
            "cost_total": sum(costs) if costs else None,
            "cost_known": len(costs) == len(done),
            "cost_per_task": (sum(costs) / len(costs)) if costs else None,
            "avg_seconds": (sum(secs) / len(secs)) if secs else None,
            "avg_steps": (sum(steps) / len(steps)) if steps else None,
        })
    rows.sort(key=lambda r: (-(r["pass_rate"] or 0), -(r["avg_score"] or 0),
                             r["cost_per_task"] if r["cost_per_task"] is not None else 1e9, r["avg_seconds"] or 1e9))

    # Baseline: what the original runs cost (the model you used when you did the task).
    orig = [c for c in cases.values() if c]
    orig_costs = [c["orig_cost"] for c in orig if c.get("orig_cost") is not None]
    baseline = {
        "models": sorted({c["orig_model"] for c in orig if c.get("orig_model")}),
        "cost_per_task": (sum(orig_costs) / len(orig_costs)) if orig_costs else None,
        "avg_seconds": (sum(c["orig_seconds"] for c in orig if c.get("orig_seconds")) / len(orig)) if orig else None,
    }

    finished = [r for r in rows if r["done"]]
    best = finished[0] if finished and finished[0]["pass_rate"] else None
    value = None
    if finished and best and best["pass_rate"] is not None:
        good_enough = [r for r in finished if (r["pass_rate"] or 0) >= best["pass_rate"] - 0.1001]
        priced = [r for r in good_enough if r["cost_per_task"] is not None]
        value = min(priced, key=lambda r: r["cost_per_task"]) if priced else best
    all_done = all(r["done"] == r["total"] for r in rows)
    return {"run": run, "rows": rows, "baseline": baseline, "best": best, "value": value,
            "all_done": all_done, "results": results, "cases": cases}


def _money(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    if v == 0:
        return "$0 (free)"
    return f"${v:.4f}" if v < 0.1 else f"${v:.2f}"


def _secs(v: Optional[float]) -> str:
    if v is None:
        return "n/a"
    return f"{v:.0f}s" if v < 120 else f"{v / 60:.1f}m"


def _pct(v: Optional[float]) -> str:
    return "n/a" if v is None else f"{v * 100:.0f}%"


def markdown(board: dict[str, Any]) -> str:
    if "error" in board:
        return board["error"]
    run = board["run"]
    lines = [f"### YouBench run `{run['id']}` · {len(run['case_ids'])} of your tasks × {len(run['models'])} models",
             "", "| # | Model | Passed | Quality | Cost / task | Avg time | Avg steps |",
             "|---|---|---|---|---|---|---|"]
    for i, r in enumerate(board["rows"], 1):
        progress = f"{r['passed']}/{r['done']}" + ("" if r["done"] == r["total"] else f" (of {r['total']})")
        quality = "n/a" if r["avg_score"] is None else f"{r['avg_score']:.1f}/10"
        steps = "n/a" if r["avg_steps"] is None else f"{r['avg_steps']:.0f}"
        lines.append(f"| {i} | `{r['model']}` | {progress} ({_pct(r['pass_rate'])}) | {quality} | "
                     f"{_money(r['cost_per_task'])} | {_secs(r['avg_seconds'])} | {steps} |")
    base = board["baseline"]
    if base["models"]:
        lines += ["", f"Original runs ({', '.join(base['models'])}): {_money(base['cost_per_task'])} per task, "
                      f"{_secs(base['avg_seconds'])} on average."]
    if board["all_done"] and not board["best"]:
        lines += ["", "**No model passed these tasks yet.** Check the misses below; a task may need a clearer prompt or check command."]
    elif board["all_done"] and board["best"]:
        lines.append("")
        lines.append(f"**Best for your work:** `{board['best']['model']}` ({_pct(board['best']['pass_rate'])} passed).")
        v = board["value"]
        if v and v["model"] != board["best"]["model"]:
            lines.append(f"**Best value:** `{v['model']}`, within 10 points of the best at "
                         f"{_money(v['cost_per_task'])} per task.")
        if v and base["cost_per_task"] and v["cost_per_task"] is not None and base["cost_per_task"] > 0:
            saving = 1 - (v["cost_per_task"] / base["cost_per_task"])
            if saving > 0.05:
                lines.append(f"Switching to `{v['model']}` would cut cost per task by about {saving * 100:.0f}%. "
                             f"Use `/model {v['model']}` to switch.")
    elif not board["all_done"]:
        lines += ["", "_Still running. Call `youbench_report` again to collect the rest._"]
    return "\n".join(lines)


def failures(board: dict[str, Any], limit: int = 6) -> list[str]:
    out = []
    for r in board.get("results", []):
        if r["status"] in DONE_STATES and not r["passed"]:
            case = board["cases"].get(r["case_id"]) or {}
            out.append(f"- `{r['model']}` on **{case.get('name', r['case_id'])}**: {r['reason'] or r['error'] or 'failed'}")
    return out[:limit]


# --- HTML ---------------------------------------------------------------------------------------

_CSS = """
:root{color-scheme:dark;--bg:#0f1115;--card:#161a21;--line:#262c36;--text:#e6e8eb;--dim:#9aa3ae;--accent:#ff7a1a;--good:#3ccf91;--bad:#ff5c6c}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif}
main{max-width:1060px;margin:0 auto;padding:40px 24px}h1{font-size:28px;margin:0 0 4px;letter-spacing:-.01em}h1 span{color:var(--accent)}
.sub{color:var(--dim);margin-bottom:28px}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:14px;margin-bottom:28px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px}.card .k{color:var(--dim);font-size:12px;text-transform:uppercase;letter-spacing:.08em}
.card .v{font-size:20px;font-weight:600;margin-top:4px;word-break:break-all}table{width:100%;border-collapse:collapse;background:var(--card);border:1px solid var(--line);border-radius:12px;overflow:hidden}
th,td{padding:10px 12px;text-align:left;border-bottom:1px solid var(--line);font-variant-numeric:tabular-nums}th{color:var(--dim);font-weight:500;font-size:12px;text-transform:uppercase;letter-spacing:.06em}
tr:last-child td{border-bottom:0}.bar{height:6px;border-radius:3px;background:var(--line);min-width:90px}.bar i{display:block;height:100%;border-radius:3px;background:var(--good)}
.pass{color:var(--good)}.fail{color:var(--bad)}h2{font-size:17px;margin:32px 0 12px}code{font-family:ui-monospace,SFMono-Regular,Consolas,monospace;font-size:13px}
.grid td{font-size:13px}.reason{color:var(--dim);font-size:12px;max-width:420px}footer{color:var(--dim);font-size:12px;margin-top:36px}
"""


def html_report(board: dict[str, Any]) -> str:
    run = board["run"]
    esc = html.escape
    best, value, base = board["best"], board["value"], board["baseline"]
    cards = [
        ("Tasks replayed", f"{len(run['case_ids'])} × {len(run['models'])} models"),
        ("Best for your work", f"{best['model']} · {_pct(best['pass_rate'])}" if best else "n/a"),
        ("Best value", f"{value['model']} · {_money(value['cost_per_task'])}/task" if value else "n/a"),
        ("Your original runs", f"{_money(base['cost_per_task'])}/task" if base["models"] else "n/a"),
    ]
    rows_html = []
    for i, r in enumerate(board["rows"], 1):
        pct = (r["pass_rate"] or 0) * 100
        quality = "n/a" if r["avg_score"] is None else "%.1f/10" % r["avg_score"]
        steps = "n/a" if r["avg_steps"] is None else "%.0f" % r["avg_steps"]
        rows_html.append(
            f"<tr><td>{i}</td><td><code>{esc(r['model'])}</code></td>"
            f"<td><div class=bar><i style='width:{pct:.0f}%'></i></div>{r['passed']}/{r['done']} ({_pct(r['pass_rate'])})</td>"
            f"<td>{quality}</td>"
            f"<td>{_money(r['cost_per_task'])}</td><td>{_secs(r['avg_seconds'])}</td>"
            f"<td>{steps}</td></tr>")
    grid = []
    for r in board["results"]:
        case = board["cases"].get(r["case_id"]) or {}
        verdict = ("<span class=pass>PASS</span>" if r["passed"] else "<span class=fail>FAIL</span>") \
            if r["status"] in DONE_STATES else esc(r["status"])
        grid.append(f"<tr><td>{esc(case.get('name', r['case_id']))}</td><td><code>{esc(r['model'])}</code></td>"
                    f"<td>{verdict}</td><td>{_money(r['cost_usd'])}</td><td>{_secs(r['seconds'])}</td>"
                    f"<td class=reason>{esc((r['reason'] or r['error'] or '')[:300])}</td></tr>")
    when = time.strftime("%Y-%m-%d %H:%M", time.localtime(run["created_at"]))
    cards_html = "".join("<div class=card><div class=k>%s</div><div class=v>%s</div></div>" % (esc(k), esc(v))
                         for k, v in cards)
    rows_joined, grid_joined = "".join(rows_html), "".join(grid)
    return f"""<!DOCTYPE html><html lang=en><head><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>YouBench · {esc(run['id'])}</title><style>{_CSS}</style></head><body><main>
<h1>You<span>Bench</span> leaderboard</h1><div class=sub>Run {esc(run['id'])} · {when} · models replayed your real Hermes tasks from their exact starting files</div>
<div class=cards>{cards_html}</div>
<table><thead><tr><th>#</th><th>Model</th><th>Passed</th><th>Quality</th><th>Cost / task</th><th>Avg time</th><th>Avg steps</th></tr></thead>
<tbody>{rows_joined}</tbody></table>
<h2>Every attempt</h2><table class=grid><thead><tr><th>Task</th><th>Model</th><th>Result</th><th>Cost</th><th>Time</th><th>Why</th></tr></thead>
<tbody>{grid_joined}</tbody></table>
<footer>Generated by YouBench for Hermes Agent. Graded by your check command when set, otherwise by a judge comparing against the change you accepted.</footer>
</main></body></html>"""


def write_html(board: dict[str, Any]) -> Optional[str]:
    if "error" in board:
        return None
    path = store.data_dir() / "reports" / f"{board['run']['id']}.html"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(html_report(board), encoding="utf-8")
    store.set_run(board["run"]["id"], report_path=str(path))
    return str(path)


def board_json(board: dict[str, Any]) -> dict[str, Any]:
    """Compact JSON for the Desktop page and the tool result."""
    if "error" in board:
        return board
    run = board["run"]
    return json.loads(json.dumps({
        "run": {k: run[k] for k in ("id", "created_at", "models", "case_ids", "status", "report_path")},
        "rows": board["rows"], "baseline": board["baseline"],
        "best": board["best"]["model"] if board["best"] else None,
        "value": board["value"]["model"] if board["value"] else None,
        "all_done": board["all_done"],
        "results": [{k: r[k] for k in ("case_id", "model", "status", "passed", "score", "reason", "cost_usd",
                                       "seconds", "steps", "diff_stat", "error")} for r in board["results"]],
        "cases": {cid: ({"name": c["name"], "prompt": c["prompt"][:300]} if c else None)
                  for cid, c in board["cases"].items()},
    }, default=str))
