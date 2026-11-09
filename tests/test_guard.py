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


# --- Windows rules (Git Bash /c/... paths), exercised with ntpath on any OS -------------------
import ntpath


def test_windows_msys_paths():
    old_p, old_w = guard._P, guard.WINDOWS
    guard._P, guard.WINDOWS = ntpath, True
    try:
        sb = r"C:\Users\USER\AppData\Local\hermes\plugin-data\youbench\runs\r1\c1\solar"
        repo = r"C:\Users\USER\yb-demo"
        guard.register_token("tokwin", sb, repo)
        guard.on_subagent_start(child_session_id="child-w", child_goal="x [youbench:tokwin]")
        # Git Bash path into the ORIGINAL repo is mapped into the sandbox
        out = guard.on_pre_tool_call(tool_name="patch", args={"path": "/c/Users/USER/yb-demo/shop.py"}, session_id="child-w")
        assert out["action"] == "modify" and out["args"]["path"].lower() == (sb + r"\shop.py").lower()
        # Git Bash path inside the sandbox is accepted, not blocked
        out = guard.on_pre_tool_call(tool_name="write_file", args={"path": guard.to_msys(sb) + "/shop.py"}, session_id="child-w")
        assert out is None or out["action"] == "modify"
        # Commands using either spelling of the repo are rewritten to the sandbox
        out = guard.on_pre_tool_call(tool_name="terminal", args={"command": "cd /c/Users/USER/yb-demo && python3 -m pytest -q"}, session_id="child-w")
        assert "/c/Users/USER/yb-demo" not in out["args"]["command"] and guard.to_msys(sb) in out["args"]["command"]
        # Writes elsewhere are still blocked
        out = guard.on_pre_tool_call(tool_name="write_file", args={"path": "/c/Windows/x.txt"}, session_id="child-w")
        assert out["action"] == "block"
    finally:
        guard.forget_token("tokwin")
        guard._P, guard.WINDOWS = old_p, old_w
