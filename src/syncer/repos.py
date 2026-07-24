from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

console = Console(highlight=False)

# Nerd font icons
ICON_OK = '\uf00c'
ICON_WARN = '\uf071'
ICON_ERR = '\uf00d'
ICON_DOWNLOAD = '\uf0ed'
ICON_PULL = '\uf019'
ICON_PUSH = '\uf093'
ICON_MOVE = '\uf0ec'
ICON_DOT = '\uf444'

LINE_WIDTH = 80

ALL_ICONS = {ICON_OK, ICON_WARN, ICON_ERR, ICON_DOWNLOAD, ICON_PULL, ICON_PUSH, ICON_MOVE}


def _display_width(text: str) -> int:
    """Calculate display width accounting for double-width nerd font icons."""
    return sum(2 if ch in ALL_ICONS else 1 for ch in text)


def _status_line(icon: str, name: str, msg: str, color: str, branch: str | None = None) -> str:
    prefix = f'{icon}  {name} '
    prefix_w = _display_width(prefix)
    if branch:
        # Displayed: {prefix}{padding} ({branch}) {msg}
        branch_w = len(f' ({branch}) ')
        msg_w = len(msg)
        padding = '_' * max(1, LINE_WIDTH - prefix_w - branch_w - msg_w)
        return f'[{color}]{prefix}{padding}[/{color}] [blue]({branch})[/blue] [{color}]{msg}[/{color}]'
    # Displayed: {prefix}{padding} {msg}
    suffix_w = len(f' {msg}')
    padding = '_' * max(1, LINE_WIDTH - prefix_w - suffix_w)
    return f'[{color}]{prefix}{padding} {msg}[/{color}]'


class Repo:
    def __init__(self, name: str, path: Path, owner: str, host: str):
        self.name = name
        self.path = path
        self.owner = owner
        self.url = f'{host}/{owner}/{name}'

    def _git(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(['git', *args], cwd=self.path, capture_output=True, text=True)  # nosec B607

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def is_git_repo(self) -> bool:
        return (self.path / '.git').is_dir()

    @property
    def has_remote(self) -> bool:
        result = self._git('remote')
        return bool(result.stdout.strip())

    @property
    def current_branch(self) -> str:
        result = self._git('rev-parse', '--abbrev-ref', 'HEAD')
        return result.stdout.strip()

    @property
    def default_branch(self) -> str | None:
        result = self._git('symbolic-ref', 'refs/remotes/origin/HEAD')
        if result.returncode == 0:
            branch = result.stdout.strip().replace('refs/remotes/origin/', '')
            # Verify the tracking ref exists (could be stale after a rename)
            if self._git('rev-parse', '--verify', f'refs/remotes/origin/{branch}').returncode == 0:
                return branch
        for branch in ('main', 'master'):
            check = self._git('rev-parse', '--verify', f'refs/heads/{branch}')
            if check.returncode == 0:
                return branch
        return None

    @property
    def is_detached(self) -> bool:
        return self.current_branch == 'HEAD'

    @property
    def uncommitted_changes(self) -> list[str]:
        result = self._git('status', '--porcelain')
        return [line for line in result.stdout.strip().splitlines() if line]

    def local_branches(self) -> list[str]:
        result = self._git('for-each-ref', '--format=%(refname:short)', 'refs/heads/')
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line]

    def branch_upstream(self, branch: str) -> tuple[str, bool]:
        """Return (upstream_short, is_gone) for a branch.

        upstream_short is '' when the branch has no tracking config (NO_UPSTREAM).
        is_gone is True when the tracking config exists but the remote branch was
        deleted (post-prune) — git reports this as an '[gone]' upstream:track.
        """
        result = self._git('for-each-ref', '--format=%(upstream:short)%09%(upstream:track)', f'refs/heads/{branch}')
        if result.returncode != 0 or not result.stdout.strip():
            return '', False
        upstream_short, _, track = result.stdout.rstrip('\n').partition('\t')
        return upstream_short, track.strip() == '[gone]'

    def ahead_behind(self, branch: str, upstream: str) -> tuple[int, int]:
        """Return (ahead, behind) commit counts of `branch` relative to `upstream`."""
        result = self._git('rev-list', '--left-right', '--count', f'{branch}...{upstream}')
        if result.returncode != 0:
            return 0, 0
        parts = result.stdout.split()
        if len(parts) != 2:
            return 0, 0
        return int(parts[0]), int(parts[1])

    def is_merged_into(self, branch: str, target: str) -> bool:
        """True if `branch` is an ancestor of `target` (prefer origin/<target> if present)."""
        ref = f'origin/{target}'
        if self._git('rev-parse', '--verify', ref).returncode != 0:
            ref = target
        return self._git('merge-base', '--is-ancestor', branch, ref).returncode == 0

    @property
    def unpushed_commits(self) -> int:
        branch = self.default_branch
        if not branch:
            return 0
        result = self._git('rev-list', f'origin/{branch}..{branch}', '--count')
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip())

    @property
    def behind_remote(self) -> int:
        branch = self.default_branch
        if not branch:
            return 0
        result = self._git('rev-list', f'{branch}..origin/{branch}', '--count')
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip())

    @property
    def unpushed_commit_lines(self) -> list[str]:
        branch = self.default_branch
        if not branch:
            return []
        result = self._git('log', '--oneline', f'origin/{branch}..{branch}')
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.strip().splitlines() if line]

    @property
    def behind_commit_lines(self) -> list[str]:
        branch = self.default_branch
        if not branch:
            return []
        result = self._git('log', '--oneline', f'{branch}..origin/{branch}')
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.strip().splitlines() if line]

    @property
    def stash_count(self) -> int:
        result = self._git('stash', 'list')
        if not result.stdout.strip():
            return 0
        return len(result.stdout.strip().splitlines())

    @property
    def last_commit_date(self) -> str | None:
        result = self._git('log', '-1', '--format=%aI')
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip()

    @property
    def first_commit_date(self) -> str | None:
        result = self._git('log', '--reverse', '--format=%aI')
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip().splitlines()[0]

    @property
    def total_commits(self) -> int:
        result = self._git('rev-list', '--count', 'HEAD')
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip())

    def fetch(self) -> bool:
        result = self._git('fetch', '--quiet')
        return result.returncode == 0

    def fetch_prune(self) -> bool:
        result = self._git('fetch', '--prune', '--quiet')
        return result.returncode == 0

    def set_head_auto(self) -> None:
        """Repoint origin/HEAD to the remote's real default (fixes stale ref after a
        default-branch rename). No-op when there's no remote."""
        if not self.has_remote:
            return
        self._git('remote', 'set-head', 'origin', '--auto')

    def pull(self) -> bool:
        result = self._git('pull', '--ff-only')
        return result.returncode == 0

    def pull_rebase(self) -> bool:
        result = self._git('pull', '--rebase')
        return result.returncode == 0

    def rebase_abort(self) -> bool:
        result = self._git('rebase', '--abort')
        return result.returncode == 0

    def push(self) -> bool:
        result = self._git('push')
        return result.returncode == 0

    def merge_ff_only(self, upstream: str) -> tuple[bool, str]:
        """Fast-forward the current branch to its upstream. Fails (never merges) if the
        upstream is not strictly ahead."""
        result = self._git('merge', '--ff-only', upstream)
        return result.returncode == 0, result.stderr.strip()

    def update_ref(self, branch: str, target: str) -> tuple[bool, str]:
        """Advance a (non-current) local branch ref to `target` without a checkout."""
        result = self._git('update-ref', f'refs/heads/{branch}', target)
        return result.returncode == 0, result.stderr.strip()

    def push_branch(self, branch: str, set_upstream: bool = False) -> tuple[bool, str]:
        """Push a specific branch with an explicit refspec (never 'whatever is checked out')."""
        args = ['push']
        if set_upstream:
            args += ['-u', 'origin', branch]
        else:
            args += ['origin', f'{branch}:{branch}']
        result = self._git(*args)
        return result.returncode == 0, result.stderr.strip()

    def delete_local_branch(self, branch: str) -> tuple[bool, str]:
        """Delete a local branch. Uses -D because a GONE branch has no upstream for git's
        own merged-check to consult — safety is enforced upstream by the delete_local guard
        (merged-into-default, not current, not default, clean), not by git's -d heuristic."""
        result = self._git('branch', '-D', branch)
        return result.returncode == 0, result.stderr.strip()

    @property
    def is_fork(self) -> bool:
        result = subprocess.run(  # nosec B607
            ['gh', 'repo', 'view', f'{self.owner}/{self.name}', '--json', 'isFork', '--jq', '.isFork'],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0 and result.stdout.strip() == 'true'

    def clone(self) -> bool:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(  # nosec B607
            ['git', 'clone', self.url, str(self.path)],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0


def find_repo_in_search_paths(name: str, search_paths: list[Path], claimed_paths: set[Path] | None = None) -> Path | None:
    claimed = claimed_paths or set()
    for search_path in search_paths:
        if not search_path.exists():
            continue
        for item in search_path.iterdir():
            if item.is_dir() and item.name == name and (item / '.git').is_dir() and item not in claimed:
                return item
        for item in search_path.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                for sub in item.iterdir():
                    if sub.is_dir() and sub.name == name and (sub / '.git').is_dir() and sub not in claimed:
                        return sub
    return None
