from __future__ import annotations

import subprocess
from pathlib import Path

from rich.console import Console

from syncer.config import SyncerConfig

console = Console()


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
    def stash_count(self) -> int:
        result = self._git('stash', 'list')
        if not result.stdout.strip():
            return 0
        return len(result.stdout.strip().splitlines())

    def fetch(self) -> bool:
        result = self._git('fetch', '--quiet')
        return result.returncode == 0

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
    issues = 0

    for repo_config in config.repos:
        path = Path(repo_config.path).expanduser()
        repo = Repo(name=repo_config.name, path=path, owner=config.owner, host=config.host)

        if not repo.exists:
            found = find_repo_in_search_paths(repo.name, search_paths)
            if found:
                console.print(f'[yellow]{repo.name}[/yellow] — not at configured path [dim]{path}[/dim]')
                console.print(f'  [cyan]found at {found}[/cyan] (update config or run [bold]syncer doctor --fix[/bold])')
                issues += 1
                continue
            console.print(f'[blue]{repo.name}[/blue] — cloning...')
            if not dry_run:
                if repo.clone():
                    console.print(f'  [green]cloned to {path}[/green]')
                else:
                    console.print('  [red]clone failed[/red]')
                    issues += 1
            continue

        if not repo.is_git_repo:
            console.print(f'[red]{repo.name}[/red] — not a git repository')
            issues += 1
            continue

        if not repo.has_remote:
            console.print(f'[red]{repo.name}[/red] — no remote configured')
            issues += 1
            continue

        repo.fetch()

        status_parts: list[str] = []

        if changes := repo.uncommitted_changes:
            status_parts.append(f'[yellow]{len(changes)} uncommitted[/yellow]')

        if ahead := repo.unpushed_commits:
            status_parts.append(f'[yellow]{ahead} unpushed[/yellow]')

        if behind := repo.behind_remote:
            status_parts.append(f'[cyan]{behind} behind[/cyan]')

        if stashes := repo.stash_count:
            status_parts.append(f'[dim]{stashes} stash(es)[/dim]')

        branch = repo.default_branch or '?'

        if not status_parts:
            console.print(f'[green]{repo.name}[/green] [dim]({branch})[/dim] — synced')
            synced += 1
        else:
            status = ', '.join(status_parts)
            console.print(f'[yellow]{repo.name}[/yellow] [dim]({branch})[/dim] — {status}')
            issues += 1

    console.print()
    console.print(f'[green]{synced} synced[/green], [yellow]{issues} need attention[/yellow]')

    if dry_run:
        console.print()
        console.print('[yellow]' + '=' * 30 + '|  END DRY RUN  |' + '=' * 30 + '[/yellow]')
