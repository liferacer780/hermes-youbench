"""Git plumbing for YouBench: snapshot a working tree, archive it, and build replay sandboxes.

Snapshots never touch the user's index, branches, stash or refs. ``snapshot_tree`` copies the
repository's index to a temporary file and writes a tree object from it with ``GIT_INDEX_FILE``,
so the only side effect in the user's repository is loose git objects (unreferenced, collected
by ``git gc`` like any other). Case archives and sandboxes live in YouBench's own data directory.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import tarfile
import tempfile
from pathlib import Path
from typing import Optional

GIT_TIMEOUT = 20
_IDENTITY = {
    "GIT_AUTHOR_NAME": "youbench", "GIT_AUTHOR_EMAIL": "youbench@localhost",
    "GIT_COMMITTER_NAME": "youbench", "GIT_COMMITTER_EMAIL": "youbench@localhost",
}


class GitError(RuntimeError):
    pass


def _git(args: list[str], cwd: str | Path, *, env: Optional[dict] = None, timeout: int = GIT_TIMEOUT,
         check: bool = True) -> subprocess.CompletedProcess:
    full_env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GIT_OPTIONAL_LOCKS": "0", **(env or {})}
    try:
        proc = subprocess.run(["git", *args], cwd=str(cwd), env=full_env, capture_output=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {args[0]} failed: {exc}") from exc
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args[:2])}: {proc.stderr.decode(errors='replace').strip()[:300]}")
    return proc


def git_available() -> bool:
    return shutil.which("git") is not None


def repo_root(path: str | Path) -> Optional[str]:
    """Top-level directory of the git work tree containing *path*, or None."""
    if not git_available() or not Path(path).is_dir():
        return None
    try:
        out = _git(["rev-parse", "--show-toplevel"], path, timeout=5).stdout.decode().strip()
    except GitError:
        return None
    if not out:
        return None
    # Never treat a home directory or filesystem root as a project.
    resolved = Path(out).resolve()
    if resolved == Path.home().resolve() or resolved == Path(resolved.anchor):
        return None
    return str(resolved)


def snapshot_tree(root: str | Path) -> str:
    """Write a tree object for the current working tree (tracked + untracked, .gitignore honoured).

    Uses a throwaway copy of the index so the user's staging area is untouched.
    """
    git_index = _git(["rev-parse", "--git-path", "index"], root).stdout.decode().strip()
    index_path = Path(git_index) if os.path.isabs(git_index) else Path(root) / git_index
    fd, tmp_index = tempfile.mkstemp(prefix="youbench-index-")
    os.close(fd)
    try:
        if index_path.exists():
            shutil.copyfile(index_path, tmp_index)  # stat cache → only changed files are re-hashed
        else:
            os.unlink(tmp_index)
        env = {"GIT_INDEX_FILE": tmp_index}
        _git(["add", "-A", "--", "."], root, env=env, timeout=GIT_TIMEOUT)
        return _git(["write-tree"], root, env=env).stdout.decode().strip()
    finally:
        with _suppress():
            os.unlink(tmp_index)


def archive_tree(root: str | Path, tree: str, dest: Path) -> int:
    """Write *tree* as a .tar.gz at *dest*; returns the archive size in bytes."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    _git(["archive", "--format=tar.gz", "-o", str(dest), tree], root, timeout=120)
    return dest.stat().st_size


def diff_trees(root: str | Path, a: str, b: str, max_chars: int = 60_000) -> tuple[str, str]:
    """(stat, patch) between two tree objects in *root*; patch truncated to *max_chars*."""
    stat = _git(["diff", "--stat", a, b], root).stdout.decode(errors="replace")
    patch = _git(["diff", "--no-color", "--no-ext-diff", a, b], root, timeout=60).stdout.decode(errors="replace")
    return stat.strip(), _truncate(patch, max_chars)


def _truncate(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + f"\n… [truncated {len(text) - max_chars} chars]"


def _safe_members(tar: tarfile.TarFile, dest: Path):
    dest = dest.resolve()
    for member in tar.getmembers():
        target = (dest / member.name).resolve()
        if dest not in target.parents and target != dest:
            continue  # path traversal
        if member.issym() or member.islnk():
            link = (target.parent / member.linkname).resolve()
            if dest not in link.parents and link != dest:
                continue  # link escaping the sandbox
        if member.isdev():
            continue
        yield member


def make_sandbox(archive: Path, dest: Path) -> None:
    """Unpack a case archive into *dest* and commit it as the baseline for diffing."""
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    with tarfile.open(archive, "r:gz") as tar:
        members = list(_safe_members(tar, dest))
        try:
            tar.extractall(dest, members=members, filter="fully_trusted")  # members already vetted above
        except TypeError:  # Python without extraction filters
            tar.extractall(dest, members=members)
    _git(["init", "-q"], dest)
    _git(["config", "core.autocrlf", "false"], dest)
    _git(["add", "-A"], dest, timeout=120)
    _git(["commit", "-q", "--allow-empty", "--no-verify", "-m", "youbench baseline"], dest, env=_IDENTITY, timeout=120)


def sandbox_diff(dest: Path, max_chars: int = 60_000) -> tuple[str, str]:
    """(stat, patch) of everything the replay changed in *dest* relative to its baseline."""
    _git(["add", "-A"], dest, timeout=120)
    stat = _git(["diff", "--cached", "--stat", "HEAD"], dest).stdout.decode(errors="replace")
    patch = _git(["diff", "--cached", "--no-color", "--no-ext-diff", "HEAD"], dest, timeout=60).stdout.decode(errors="replace")
    return stat.strip(), _truncate(patch, max_chars)


class _suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True
