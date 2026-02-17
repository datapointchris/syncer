from __future__ import annotations

import importlib.metadata
import json
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from syncer.config import CONFIG_DIR
from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import resolve_config
from syncer.repos import ICON_ERR
from syncer.repos import ICON_MOVE
from syncer.repos import ICON_WARN
from syncer.repos import Repo
from syncer.repos import _status_line
from syncer.repos import find_repo_in_search_paths
from syncer.repos import sync_repos

app = typer.Typer(invoke_without_command=True)
console = Console()

TEMPLATE_CONFIG = {
    'owner': '',
    'host': 'https://github.com',
    'search_paths': ['~/code', '~/tools'],
    'repos': [
        {'name': 'example-repo', 'path': '~/code/example-repo'},
    ],
}


@app.callback()
def main(
    ctx: typer.Context,
    config: Annotated[str | None, typer.Option('--config', '-c', help='Config name to use')] = None,
    dry_run: Annotated[bool, typer.Option('--dry-run', '-n', help='Show what would happen without making changes')] = False,
) -> None:
    """Syncer — check if local git repos are fully synced."""
    ctx.ensure_object(dict)
    ctx.obj['config_name'] = config
    ctx.obj['dry_run'] = dry_run

    if ctx.invoked_subcommand is None:
        syncer_config = resolve_config(config)
        sync_repos(syncer_config, dry_run=dry_run)


@app.command()
def doctor(
    ctx: typer.Context,
    fix: Annotated[bool, typer.Option('--fix', help='Automatically update config with fixes')] = False,
) -> None:
    """Reconcile config vs filesystem using search_paths."""
    config_name = ctx.obj.get('config_name')
    syncer_config = resolve_config(config_name)
    search_paths = [Path(p).expanduser() for p in syncer_config.search_paths]

    issues_found = 0
    config_changed = False

    console.print()

    for repo_config in syncer_config.repos:
        path = Path(repo_config.path).expanduser()
        label = repo_config.path if repo_config.path.startswith('~') else repo_config.name

        # Path mismatch or missing
        if not path.exists():
            found = find_repo_in_search_paths(repo_config.name, search_paths)
            if found:
                console.print(_status_line(ICON_MOVE, label, 'path mismatch', 'yellow'))
                console.print(f'    found at {found} (run syncer doctor --fix)')
                if fix:
                    repo_config.path = str(found).replace(str(Path.home()), '~')
                    console.print(f'    [green]fixed → {repo_config.path}[/green]')
                    config_changed = True
                issues_found += 1
            else:
                console.print(_status_line(ICON_WARN, label, 'missing', 'yellow'))
                issues_found += 1
            console.print()
            continue

        if not (path / '.git').is_dir():
            continue

        repo = Repo(name=repo_config.name, path=path, owner=syncer_config.owner, host=syncer_config.host)

        # Master branch check
        if repo.default_branch == 'master':
            if repo.is_fork:
                console.print(_status_line(ICON_WARN, label, 'using master, fork skipping', 'yellow', branch='master'))
            else:
                changes = repo.uncommitted_changes
                ahead = repo.unpushed_commits
                if changes or ahead:
                    console.print(_status_line(ICON_ERR, label, 'using master, has local changes', 'red', branch='master'))
                elif fix:
                    console.print(_status_line(ICON_WARN, label, 'renaming master → main...', 'yellow', branch='master'))
                    if repo.rename_branch('master', 'main') and repo.push_branch('main'):
                        repo.set_default_branch_on_github('main')
                        repo.delete_remote_branch('master')
                        console.print('    [green]done[/green]')
                    else:
                        console.print('    [red]rename failed[/red]')
                else:
                    console.print(_status_line(ICON_WARN, label, 'using master', 'yellow', branch='master'))
            issues_found += 1
            console.print()

    # Check for untracked repos in search paths
    known_names = {r.name for r in syncer_config.repos}
    untracked: list[dict[str, str]] = []
    for search_path in search_paths:
        if not search_path.exists():
            continue
        for item in search_path.iterdir():
            if item.is_dir() and (item / '.git').is_dir() and item.name not in known_names:
                item_path = str(item).replace(str(Path.home()), '~')
                console.print(_status_line(ICON_WARN, item_path, 'untracked', 'yellow'))
                console.print()
                untracked.append({'name': item.name, 'path': item_path})
                issues_found += 1

    if fix and untracked:
        for u in untracked:
            syncer_config.repos.append(RepoConfig(**u))
        config_changed = True

    # Save config if changed
    if fix and config_changed:
        config_file = CONFIG_DIR / f'{config_name or _get_default_config_name()}.json'
        config_file.write_text(json.dumps(syncer_config.model_dump(), indent=2) + '\n')
        console.print(f'\n[green] Config updated: {config_file}[/green]')

    console.print()
    if issues_found == 0:
        console.print('[blue] All repos healthy.[/blue]')
    else:
        console.print(f'[yellow] {issues_found} issue(s) found.[/yellow]')
        if not fix:
            console.print('[white]Run with --fix to auto-repair[/white]')


@app.command()
def version() -> None:
    """Print the installed version of syncer."""
    v = importlib.metadata.version('syncer')
    console.print(f'syncer {v}')


@app.command()
def update() -> None:
    """Update syncer to the latest GitHub release."""
    result = subprocess.run(  # nosec B607
        ['gh', 'release', '--repo', 'datapointchris/syncer', 'view', '--json', 'tagName', '--jq', '.tagName'],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        console.print('[red]Failed to fetch latest release tag[/red]')
        sys.exit(1)

    tag = result.stdout.strip()
    console.print(f'Latest release: [cyan]{tag}[/cyan]')
    console.print('Installing...')

    install = subprocess.run(  # nosec B607
        ['uv', 'tool', 'install', '--force', f'git+https://github.com/datapointchris/syncer.git@{tag}'],
        capture_output=True,
        text=True,
    )
    if install.returncode != 0:
        console.print(f'[red]Install failed:[/red] {install.stderr}')
        sys.exit(1)
    console.print(f'[green]Updated to {tag}[/green]')


@app.command()
def init(name: Annotated[str, typer.Argument(help='Config name')]) -> None:
    """Create a template config file."""
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    config_file = CONFIG_DIR / f'{name}.json'
    if config_file.exists():
        console.print(f'[red]Config already exists: {config_file}[/red]')
        sys.exit(1)
    config_file.write_text(json.dumps(TEMPLATE_CONFIG, indent=2) + '\n')
    console.print(f'[green]Created config: {config_file}[/green]')
    console.print('[yellow]Edit the file to add your repos and owner.[/yellow]')


def _git(path: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)  # nosec B607


def _setup_demo_repos(base: Path) -> SyncerConfig:
    """Create real git repos in various states for demo."""

    def make_repo(name: str) -> Path:
        bare = base / 'remotes' / f'{name}.git'
        bare.mkdir(parents=True)
        subprocess.run(['git', 'init', '--bare', str(bare)], capture_output=True)  # nosec B607
        repo = base / 'repos' / name
        subprocess.run(['git', 'clone', str(bare), str(repo)], capture_output=True)  # nosec B607
        _git(repo, 'config', 'user.email', 'demo@syncer')
        _git(repo, 'config', 'user.name', 'Demo')
        (repo / 'README.md').write_text(f'# {name}\n')
        _git(repo, 'add', '.')
        _git(repo, 'commit', '-m', 'init')
        _git(repo, 'push')
        return repo

    # 1. Synced — clean, up to date
    make_repo('synced-repo')

    # 2. Uncommitted changes
    repo = make_repo('uncommitted-repo')
    (repo / 'src').mkdir()
    (repo / 'src' / 'main.py').write_text('print("hello")\n')
    (repo / 'new_file.txt').write_text('untracked\n')
    _git(repo, 'add', 'src/main.py')

    # 3. Unpushed commits
    repo = make_repo('unpushed-repo')
    (repo / 'feature.py').write_text('def feature(): pass\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'feat: add new feature')
    (repo / 'fix.py').write_text('def fix(): pass\n')
    _git(repo, 'add', '.')
    _git(repo, 'commit', '-m', 'fix: resolve bug')

    # 4. Behind remote (push from a second clone)
    make_repo('behind-repo')
    second = base / 'second-clone'
    bare = base / 'remotes' / 'behind-repo.git'
    subprocess.run(['git', 'clone', str(bare), str(second)], capture_output=True)  # nosec B607
    _git(second, 'config', 'user.email', 'demo@syncer')
    _git(second, 'config', 'user.name', 'Demo')
    (second / 'update.txt').write_text('remote change\n')
    _git(second, 'add', '.')
    _git(second, 'commit', '-m', 'chore: update deps')
    _git(second, 'push')

    # 5. Stashes
    repo = make_repo('stashed-repo')
    (repo / 'wip.txt').write_text('work in progress\n')
    _git(repo, 'add', '.')
    _git(repo, 'stash', 'push', '-m', 'saving work')
    (repo / 'wip2.txt').write_text('more wip\n')
    _git(repo, 'add', '.')
    _git(repo, 'stash', 'push', '-m', 'saving more work')

    # 6. Not a git repo
    not_git = base / 'repos' / 'not-a-repo'
    not_git.mkdir(parents=True)
    (not_git / 'file.txt').write_text('just a directory\n')

    # 7. No remote
    no_remote = base / 'repos' / 'no-remote-repo'
    no_remote.mkdir(parents=True)
    subprocess.run(['git', 'init', str(no_remote)], capture_output=True)  # nosec B607
    _git(no_remote, 'config', 'user.email', 'demo@syncer')
    _git(no_remote, 'config', 'user.name', 'Demo')
    (no_remote / 'README.md').write_text('# no remote\n')
    _git(no_remote, 'add', '.')
    _git(no_remote, 'commit', '-m', 'init')

    repos_dir = base / 'repos'
    return SyncerConfig(
        owner='demo',
        host='https://github.com',
        search_paths=[],
        repos=[RepoConfig(name=d.name, path=str(d)) for d in sorted(repos_dir.iterdir()) if d.is_dir()],
    )


@app.command()
def demo() -> None:
    """Run sync against real temp repos to show each status state."""
    tmp = Path(tempfile.mkdtemp(prefix='syncer-demo-'))
    try:
        config = _setup_demo_repos(tmp)
        sync_repos(config)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def _get_default_config_name() -> str:
    config_files = list(CONFIG_DIR.glob('*.json'))
    if config_files:
        return config_files[0].stem
    return 'default'


if __name__ == '__main__':
    app()
