from __future__ import annotations

import json
import sys
from pathlib import Path

from pydantic import BaseModel
from rich.console import Console

CONFIG_DIR = Path.home() / '.config' / 'syncer'

console = Console()


class RepoConfig(BaseModel):
    name: str
    path: str


class SyncerConfig(BaseModel):
    owner: str
    host: str
    search_paths: list[str] = []
    repos: list[RepoConfig]


def load_config(name: str) -> SyncerConfig:
    config_file = CONFIG_DIR / f'{name}.json'
    if not config_file.exists():
        console.print(f'[red]Config file not found: {config_file}[/red]')
        sys.exit(1)
    data = json.loads(config_file.read_text())
    return SyncerConfig(**data)


def resolve_config(name: str | None = None) -> SyncerConfig:
    if name:
        return load_config(name)

    if not CONFIG_DIR.exists():
        console.print(f'[red]Config directory not found: {CONFIG_DIR}[/red]')
        console.print('[yellow]Create a config with: syncer init <name>[/yellow]')
        sys.exit(1)

    config_files = list(CONFIG_DIR.glob('*.json'))

    if not config_files:
        console.print(f'[red]No config files found in {CONFIG_DIR}[/red]')
        console.print('[yellow]Create a config with: syncer init <name>[/yellow]')
        sys.exit(1)

    if len(config_files) == 1:
        return load_config(config_files[0].stem)

    console.print('[red]Multiple config files found. Specify one with --config:[/red]')
    for f in sorted(config_files):
        console.print(f'  [cyan]{f.stem}[/cyan]')
    sys.exit(1)
