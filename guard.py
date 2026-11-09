"""Sandbox guard: keeps replay subagents inside their private copy of the project.

The public subagent API does not take a working directory, so every replay child is tagged
with a token in its goal. ``subagent_start`` maps the child's session id to its sandbox, and
``pre_tool_call`` then uses the documented ``modify`` / ``block`` directives on that child's
calls only:

* ``terminal``: ``workdir`` is pinned to the sandbox; literal references to the original
  repository path are rewritten to the sandbox path.
* file tools: relative paths resolve inside the sandbox; paths into the original repository
  are mapped to the same file in the sandbox; writes anywhere else are blocked.

Sessions that are not YouBench replays are never touched. Approvals are untouched: this hook
only narrows what a replay can reach, it never approves anything.
"""

from __future__ import annotations

import os
import re
import threading
from typing import Any, Optional

TOKEN_RE = re.compile(r"\[youbench:([a-z0-9_-]{6,80})\]")

_LOCK = threading.RLock()
_TOKENS: dict[str, dict[str, str]] = {}      # token -> {"sandbox", "repo_root"}
_CHILDREN: dict[str, dict[str, str]] = {}    # child session id -> {"sandbox", "repo_root", "token"}

_READ_TOOLS = {"read_file", "search_files"}
_WRITE_TOOLS = {"write_file", "patch"}
_PATCH_PATH_RE = re.compile(r"^(\*\*\* (?:Update|Add|Delete) File: )(.+)$", re.MULTILINE)


def register_token(token: str, sandbox: str, repo_root: str) -> None:
    with _LOCK:
        _TOKENS[token] = {"sandbox": sandbox, "repo_root": repo_root}


def forget_token(token: str) -> None:
    with _LOCK:
        _TOKENS.pop(token, None)
        for sid in [s for s, info in _CHILDREN.items() if info.get("token") == token]:
            _CHILDREN.pop(sid, None)


def child_session_for(token: str) -> Optional[str]:
    with _LOCK:
        for sid, info in _CHILDREN.items():
            if info.get("token") == token:
                return sid
    return None


def is_replay_session(session_id: Optional[str]) -> bool:
    with _LOCK:
        return bool(session_id) and session_id in _CHILDREN


def on_subagent_start(child_session_id: Optional[str] = None, child_goal: Optional[str] = None, **_: Any) -> None:
    if not child_session_id or not child_goal:
        return
    match = TOKEN_RE.search(child_goal)
    if not match:
        return
    with _LOCK:
        info = _TOKENS.get(match.group(1))
        if info:
            _CHILDREN[child_session_id] = {**info, "token": match.group(1)}


# --- path helpers -------------------------------------------------------------------------------

def _norm(path: str) -> str:
    return os.path.normcase(os.path.abspath(os.path.expanduser(path)))


def _inside(path: str, root: str) -> bool:
    try:
        return os.path.commonpath([_norm(path), _norm(root)]) == _norm(root)
    except ValueError:  # different drives on Windows
        return False


def map_path(path: str, sandbox: str, repo_root: str) -> tuple[str, bool]:
    """(mapped path, is_inside_sandbox)."""
    raw = os.path.expanduser(path or ".")
    if not os.path.isabs(raw):
        return os.path.join(sandbox, raw), True
    if _inside(raw, sandbox):
        return raw, True
    if repo_root and _inside(raw, repo_root):
        rel = os.path.relpath(_norm(raw), _norm(repo_root))
        return os.path.join(sandbox, rel) if rel != "." else sandbox, True
    return raw, False


def rewrite_repo_refs(text: str, sandbox: str, repo_root: str) -> str:
    if not text or not repo_root:
        return text
    variants = {repo_root, repo_root.replace("\\", "/"), repo_root.replace("/", "\\")}
    for variant in sorted(variants, key=len, reverse=True):
        text = text.replace(variant, sandbox)
    return text


# --- the hook -----------------------------------------------------------------------------------

def on_pre_tool_call(tool_name: str = "", args: Any = None, session_id: str = "", **_: Any) -> Optional[dict]:
    with _LOCK:
        info = _CHILDREN.get(session_id or "")
    if not info or not isinstance(args, dict):
        return None
    sandbox, repo_root = info["sandbox"], info["repo_root"]
    changes: dict[str, Any] = {}

    if tool_name == "terminal":
        workdir = args.get("workdir")
        mapped, inside = map_path(workdir, sandbox, repo_root) if workdir else (sandbox, True)
        target = mapped if inside else sandbox
        if target != workdir:
            changes["workdir"] = target
        command = args.get("command")
        if isinstance(command, str):
            new_command = rewrite_repo_refs(command, sandbox, repo_root)
            if new_command != command:
                changes["command"] = new_command

    elif tool_name in _READ_TOOLS or tool_name in _WRITE_TOOLS:
        path = args.get("path")
        if isinstance(path, str) and path:
            mapped, inside = map_path(path, sandbox, repo_root)
            if not inside and tool_name in _WRITE_TOOLS:
                return {"action": "block", "message": (
                    f"YouBench sandbox: this benchmark replay may only change files inside {sandbox}. "
                    f"Use a path inside that folder (relative paths work)."
                )}
            if mapped != path:
                changes["path"] = mapped
        elif tool_name == "search_files":
            changes["path"] = sandbox
        patch_text = args.get("patch")
        if tool_name == "patch" and isinstance(patch_text, str) and patch_text:
            def _fix(m: re.Match) -> str:
                mapped, inside = map_path(m.group(2).strip(), sandbox, repo_root)
                return m.group(1) + (mapped if inside else m.group(2))
            new_patch = _PATCH_PATH_RE.sub(_fix, patch_text)
            if new_patch != patch_text:
                changes["patch"] = new_patch

    return {"action": "modify", "args": changes} if changes else None
