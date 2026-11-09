"""Grading a replay: the user's own check command first, a judge model second.

* With a check command (``pytest -q``, ``npm test``…), pass/fail is the command's exit code,
  run in the replay's sandbox through Hermes's ``terminal`` tool (so normal approvals apply).
* Without one, a judge compares the replay's change against the change the user accepted
  originally, using ``ctx.llm`` on the user's active model (no model override, no extra keys).
"""

from __future__ import annotations

import json
import logging
from typing import Any, Optional

logger = logging.getLogger(__name__)

JUDGE_SCHEMA = {
    "type": "object",
    "properties": {
        "passed": {"type": "boolean"},
        "score": {"type": "integer", "minimum": 0, "maximum": 10},
        "reason": {"type": "string"},
    },
    "required": ["passed", "score", "reason"],
    "additionalProperties": False,
}

JUDGE_INSTRUCTIONS = """You grade an AI coding agent's attempt at a real task.

You get: the task as the user wrote it, the REFERENCE change (what the user originally accepted),
the CANDIDATE change (the attempt being graded), the agent's final message, and test output if any.

Decide whether the candidate accomplishes the task as well as the reference would for the user.
It does not need to match the reference line for line: different but correct solutions pass.
Fail it if it is incomplete, breaks something, changes unrelated code, or only describes a fix
without making it. If tests ran and failed, it fails.

Return JSON: passed (bool), score (0-10 quality, 10 = as good as or better than the reference),
reason (one or two plain sentences a developer can act on)."""


def _clip(text: Optional[str], limit: int) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + f"\n… [truncated {len(text) - limit} chars]"


def run_check(ctx: Any, check_cmd: str, sandbox: str, timeout: int = 600) -> tuple[Optional[bool], str]:
    """(ok, output) for the user's check command inside *sandbox*; ok=None if it could not run."""
    try:
        raw = ctx.dispatch_tool("terminal", {"command": check_cmd, "workdir": sandbox, "timeout": timeout})
    except Exception as exc:  # pragma: no cover - host failure
        return None, f"check could not run: {exc}"
    try:
        data = json.loads(raw) if isinstance(raw, str) else dict(raw or {})
    except (TypeError, ValueError):
        return None, _clip(str(raw), 4000)
    output = str(data.get("output") or "") + (f"\n{data['error']}" if data.get("error") else "")
    if output.lstrip().startswith("BLOCKED") or data.get("status") in ("blocked", "denied"):
        # Hermes's approval gate refused the command: that says nothing about the replay.
        return None, _clip("Check command was not approved, so it did not run: " + output.strip(), 4000)
    code = data.get("exit_code")
    if code is None:
        return None, _clip(output or "check did not report an exit code", 4000)
    return int(code) == 0, _clip(output, 4000)


def judge(ctx: Any, *, prompt: str, reference: str, candidate: str, summary: str,
          check_output: Optional[str]) -> dict[str, Any]:
    """{"passed", "score", "reason"} from the judge, or {"error"} when it could not answer."""
    blocks = [
        f"TASK:\n{_clip(prompt, 6000)}",
        f"REFERENCE CHANGE (accepted by the user):\n{_clip(reference, 14000) or '(no file changes)'}",
        f"CANDIDATE CHANGE:\n{_clip(candidate, 14000) or '(no file changes)'}",
        f"AGENT'S FINAL MESSAGE:\n{_clip(summary, 2500) or '(none)'}",
    ]
    if check_output is not None:
        blocks.append(f"TEST OUTPUT:\n{_clip(check_output, 3000)}")
    try:
        result = ctx.llm.complete_structured(
            instructions=JUDGE_INSTRUCTIONS,
            input=[{"type": "text", "text": "\n\n".join(blocks)}],
            json_schema=JUDGE_SCHEMA,
            schema_name="youbench_grade",
            temperature=0.0,
            max_tokens=400,
            purpose="youbench.judge",
        )
    except Exception as exc:
        logger.warning("youbench: judge call failed: %s", exc)
        return {"error": f"judge unavailable: {exc}"}
    parsed = getattr(result, "parsed", None)
    if not isinstance(parsed, dict):
        parsed = _lenient_parse(getattr(result, "text", "") or "")
    if not isinstance(parsed, dict) or "passed" not in parsed:
        return {"error": "judge returned no usable verdict"}
    try:
        score = max(0, min(10, int(parsed.get("score", 0))))
    except (TypeError, ValueError):
        score = 0
    return {"passed": bool(parsed.get("passed")), "score": score, "reason": str(parsed.get("reason") or "")[:600]}


def _lenient_parse(text: str) -> Optional[dict]:
    start, end = text.find("{"), text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except ValueError:
        return None


def grade(ctx: Any, case: dict, *, sandbox: str, candidate: str, summary: str, status: str) -> dict[str, Any]:
    """Final verdict for one replay."""
    out: dict[str, Any] = {"passed": False, "score": 0.0, "reason": "", "check_ok": None, "check_output": None}
    if status != "SUCCEEDED" and not candidate.strip():
        out["reason"] = f"The agent did not finish ({status.lower()})."
        return out
    if case.get("reference", "").strip() and not candidate.strip():
        out["reason"] = "No files were changed, but the task required changes."
        if case.get("check_cmd"):
            ok, output = run_check(ctx, case["check_cmd"], sandbox)
            out.update(check_ok=ok, check_output=output)
        return out

    check_output = None
    if case.get("check_cmd"):
        ok, check_output = run_check(ctx, case["check_cmd"], sandbox)
        out.update(check_ok=ok, check_output=check_output)

    verdict = judge(ctx, prompt=case["prompt"], reference=case.get("reference") or "",
                    candidate=candidate, summary=summary, check_output=check_output)
    if "error" in verdict:
        if out["check_ok"] is not None:
            out["passed"] = bool(out["check_ok"])
            out["score"] = 10.0 if out["passed"] else 0.0
            out["reason"] = "Graded by your check command (judge unavailable)."
        else:
            out["reason"] = verdict["error"]
        return out

    out["score"] = float(verdict["score"])
    out["reason"] = verdict["reason"]
    # Your own check command is the source of truth when there is one.
    out["passed"] = bool(out["check_ok"]) if out["check_ok"] is not None else verdict["passed"]
    return out
