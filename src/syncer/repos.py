from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

from syncer.config import SyncerConfig

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
            return result.stdout.strip().replace('refs/remotes/origin/', '')
        for branch in ('main', 'master'):
            check = self._git('rev-parse', '--verify', f'refs/heads/{branch}')
            if check.returncode == 0:
                return branch
        return None

    @property
    def uncommitted_changes(self) -> list[str]:
        result = self._git('status', '--porcelain')
        return [line for line in result.stdout.strip().splitlines() if line]

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

    def fetch(self) -> bool:
        result = self._git('fetch', '--quiet')
        return result.returncode == 0

    def pull(self) -> bool:
        result = self._git('pull', '--ff-only')
        return result.returncode == 0

    def push(self) -> bool:
        result = self._git('push')
        return result.returncode == 0

    def rename_branch(self, old: str, new: str) -> bool:
        result = self._git('branch', '-m', old, new)
        return result.returncode == 0

    def push_branch(self, branch: str) -> bool:
        result = self._git('push', '-u', 'origin', branch)
        return result.returncode == 0

    def delete_remote_branch(self, branch: str) -> bool:
        result = self._git('push', 'origin', '--delete', branch)
        return result.returncode == 0

    def set_default_branch_on_github(self, branch: str) -> bool:
        result = subprocess.run(  # nosec B607
            ['gh', 'repo', 'edit', f'{self.owner}/{self.name}', '--default-branch', branch],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0

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


def find_repo_in_search_paths(name: str, search_paths: list[Path]) -> Path | None:
    for search_path in search_paths:
        if not search_path.exists():
            continue
        for item in search_path.iterdir():
            if item.is_dir() and item.name == name and (item / '.git').is_dir():
                return item
        for item in search_path.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                for sub in item.iterdir():
                    if sub.is_dir() and sub.name == name and (sub / '.git').is_dir():
                        return sub
    return None


def sync_repos(config: SyncerConfig, dry_run: bool = False) -> None:
    search_paths = [Path(p).expanduser() for p in config.search_paths]

    console.print()
    if dry_run:
        console.print('[yellow]' + '=' * 35 + '|  DRY RUN  |' + '=' * 35 + '[/yellow]')
        console.print()

    synced = 0
    pulled = 0
    pushed = 0
    issues = 0
    pullable = 0
    pushable = 0

    for repo_config in config.repos:
        path = Path(repo_config.path).expanduser()
        label = repo_config.path if repo_config.path.startswith('~') else repo_config.name
        repo = Repo(name=repo_config.name, path=path, owner=config.owner, host=config.host)

        if not repo.exists:
            found = find_repo_in_search_paths(repo.name, search_paths)
            if found:
                console.print(_status_line(ICON_MOVE, label, 'path mismatch', 'yellow'))
                console.print(f'    found at {found} (run syncer doctor --fix)')
                issues += 1
            else:
                console.print(_status_line(ICON_DOWNLOAD, label, 'cloning', 'cyan'))
                if not dry_run:
                    if repo.clone():
                        console.print(f'    cloned to {path}')
                    else:
                        console.print('    clone failed')
                        issues += 1
            console.print()
            continue

        if not repo.is_git_repo:
            console.print(_status_line(ICON_ERR, label, 'not a git repository', 'red'))
            console.print()
            issues += 1
            continue

        if not repo.has_remote:
            console.print(_status_line(ICON_ERR, label, 'no remote', 'red'))
            console.print()
            issues += 1
            continue

        repo.fetch()

        changes = repo.uncommitted_changes
        ahead = repo.unpushed_commits
        behind = repo.behind_remote
        stashes = repo.stash_count
        branch = repo.default_branch or '?'

        # Auto-pull if safe: behind remote with no local changes
        if behind and not changes and not ahead:
            if dry_run:
                pullable += 1
            elif repo.pull():
                console.print(_status_line(ICON_PULL, label, f'pulled {behind} commit(s)', 'green', branch=branch))
                pulled += 1
                console.print()
                continue

        # Auto-push if safe: unpushed commits with no uncommitted changes and not behind
        if ahead and not changes and not behind:
            if dry_run:
                pushable += 1
            elif repo.push():
                console.print(_status_line(ICON_PUSH, label, f'pushed {ahead} commit(s)', 'green', branch=branch))
                pushed += 1
                console.print()
                continue

        status_parts: list[str] = []
        if changes:
            status_parts.append(f'{len(changes)} uncommitted')
        if ahead:
            status_parts.append(f'{ahead} unpushed')
        if behind:
            status_parts.append(f'{behind} behind')
        if stashes:
            status_parts.append(f'{stashes} stash(es)')

        if not status_parts:
            console.print(_status_line(ICON_OK, label, 'synced', 'green', branch=branch))
            synced += 1
        else:
            status = ', '.join(status_parts)
            console.print(_status_line(ICON_WARN, label, status, 'yellow', branch=branch))
            if changes:
                for line in changes:
                    console.print(f'    {line}')
            if ahead:
                for line in repo.unpushed_commit_lines:
                    console.print(f'    {line}')
            if behind:
                for line in repo.behind_commit_lines:
                    console.print(f'    {line}')
            issues += 1
        console.print()

    summary_parts = [f'[green]{ICON_OK}  {synced} synced[/green]']
    if dry_run and pullable:
        summary_parts.append(f'[cyan]{ICON_PULL}  {pullable} pullable[/cyan]')
    if pulled:
        summary_parts.append(f'[green]{ICON_PULL}  {pulled} pulled[/green]')
    if dry_run and pushable:
        summary_parts.append(f'[cyan]{ICON_PUSH}  {pushable} pushable[/cyan]')
    if pushed:
        summary_parts.append(f'[green]{ICON_PUSH}  {pushed} pushed[/green]')
    if issues:
        summary_parts.append(f'[yellow]{ICON_WARN}  {issues} need attention[/yellow]')
    console.print()
    console.print('  │  '.join(summary_parts))

    if dry_run:
        console.print()
        console.print('[yellow]' + '=' * 30 + '|  END DRY RUN  |' + '=' * 30 + '[/yellow]')
