import importlib.util, os, sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("yb_guard", ROOT / "guard.py")
guard = importlib.util.module_from_spec(spec); spec.loader.exec_module(guard)

SB, REPO = os.path.abspath("/tmp/yb/sandbox"), os.path.abspath("/tmp/yb/repo")


def setup_module():
    guard.register_token("tok123", SB, REPO)
    guard.on_subagent_start(child_session_id="child-1", child_goal="do it [youbench:tok123]")


def call(tool, args, sid="child-1"):
    return guard.on_pre_tool_call(tool_name=tool, args=args, session_id=sid)


def test_other_sessions_untouched():
    assert call("write_file", {"path": "x.py"}, sid="someone-else") is None


def test_relative_paths_go_to_sandbox():
    assert call("write_file", {"path": "add.py", "content": ""})["args"]["path"] == os.path.join(SB, "add.py")


def test_original_repo_paths_are_mapped():
    out = call("read_file", {"path": os.path.join(REPO, "src", "a.py")})
    assert out["args"]["path"] == os.path.join(SB, "src", "a.py")


def test_writes_outside_are_blocked():
    assert call("write_file", {"path": "/etc/passwd", "content": ""})["action"] == "block"


def test_terminal_pinned_and_rewritten():
    out = call("terminal", {"command": f"cd {REPO} && pytest -q"})
    assert out["args"]["workdir"] == SB and REPO not in out["args"]["command"]


def test_terminal_outside_workdir_reset():
    assert call("terminal", {"command": "ls", "workdir": "/"})["args"]["workdir"] == SB


def test_forget():
    guard.forget_token("tok123")
    assert call("write_file", {"path": "x.py"}) is None
