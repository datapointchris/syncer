from __future__ import annotations

import importlib.metadata
import json
import subprocess
import sys
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console

from syncer.config import CONFIG_DIR
from syncer.config import RepoConfig
from syncer.config import resolve_config
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

    for repo_config in syncer_config.repos:
        path = Path(repo_config.path).expanduser()
        if not path.exists():
            found = find_repo_in_search_paths(repo_config.name, search_paths)
            if found:
                console.print(f'[yellow]PATH MISMATCH[/yellow] {repo_config.name}: configured {path} → found {found}')
                if fix:
                    repo_config.path = str(found).replace(str(Path.home()), '~')
                    console.print(f'  [green]fixed → {repo_config.path}[/green]')
                issues_found += 1
            else:
                console.print(f'[red]MISSING[/red] {repo_config.name}: not found at {path} or in search paths')
                issues_found += 1

    known_names = {r.name for r in syncer_config.repos}
    untracked: list[dict[str, str]] = []
    for search_path in search_paths:
        if not search_path.exists():
            continue
        for item in search_path.iterdir():
            if item.is_dir() and (item / '.git').is_dir() and item.name not in known_names:
                console.print(f'[cyan]UNTRACKED[/cyan] {item.name}: found at {item}')
                untracked.append({'name': item.name, 'path': str(item).replace(str(Path.home()), '~')})
                issues_found += 1

    if fix and (untracked or issues_found):
        for u in untracked:
            syncer_config.repos.append(RepoConfig(**u))
        config_file = CONFIG_DIR / f'{config_name or _get_default_config_name()}.json'
        config_file.write_text(json.dumps(syncer_config.model_dump(), indent=2) + '\n')
        console.print(f'[green]Config updated: {config_file}[/green]')

    if issues_found == 0:
        console.print('[green]All repos accounted for.[/green]')
    else:
        console.print(f'\n[yellow]{issues_found} issue(s) found.[/yellow]')


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


def _get_default_config_name() -> str:
    config_files = list(CONFIG_DIR.glob('*.json'))
    if config_files:
        return config_files[0].stem
    return 'default'


if __name__ == '__main__':
    app()
