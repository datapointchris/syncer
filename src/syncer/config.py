from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel
from rich.console import Console

TOOL_CONFIG_PATH = Path.home() / '.config' / 'syncer' / 'config.toml'
DATA_DIR = Path.home() / '.local' / 'share' / 'syncer'

# Legacy path for deprecation fallback
_LEGACY_CONFIG_DIR = Path.home() / '.config' / 'syncer'

console = Console()


class RepoConfig(BaseModel):
    name: str
    path: str
    status: Literal['active', 'dormant', 'retired'] = 'active'
    description: str | None = None
    owner: str | None = None


class SyncerConfig(BaseModel):
    owner: str
    host: str
    search_paths: list[str] = []
    repos: list[RepoConfig]


def _load_repos_file(path: Path) -> SyncerConfig:
    """Load the repo registry from a JSON file."""
    if not path.exists():
        console.print(f'[red]Repos file not found: {path}[/red]')
        sys.exit(1)
    data = json.loads(path.read_text())
    config = SyncerConfig(**data)
    config.repos.sort(key=lambda r: r.path)
    return config


def get_repos_file_path() -> Path:
    """Resolve the repos file path from the tool config or legacy fallback."""
    if TOOL_CONFIG_PATH.exists():
        tool_config = tomllib.loads(TOOL_CONFIG_PATH.read_text())
        repos_file = tool_config.get('repos_file')
        if repos_file:
            return Path(repos_file).expanduser()

    # Legacy fallback: look for JSON files in ~/.config/syncer/
    legacy_files = list(_LEGACY_CONFIG_DIR.glob('*.json')) if _LEGACY_CONFIG_DIR.exists() else []
    if legacy_files:
        console.print(
            f'[yellow]Warning: using legacy config at {legacy_files[0]}. '
            f'Migrate to {TOOL_CONFIG_PATH} with repos_file pointing to ~/dev/repos.json[/yellow]'
        )
        return legacy_files[0]

    console.print(f'[red]No config found. Create {TOOL_CONFIG_PATH} with repos_file = "~/dev/repos.json"[/red]')
    sys.exit(1)


def resolve_config() -> SyncerConfig:
    repos_file = get_repos_file_path()
    return _load_repos_file(repos_file)
