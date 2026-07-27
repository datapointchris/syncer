from __future__ import annotations

import json
import sys
import tomllib
from pathlib import Path
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import field_validator
from rich.console import Console

from syncer.policy import BUILTIN_POLICIES
from syncer.policy import Policy
from syncer.repos import GIT_TIMEOUT_SECONDS

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
    # Weak, portable policy hint. Sits at precedence level 3 — below the CLI flag and
    # machine-local repo_overrides, above the machine default_policy. The one
    # policy-adjacent field in the registry — syncer reads nothing else here.
    sync_policy: str | None = None
    # Explicit clone URL, bypassing both url_template and the default three-part path. For the
    # one repo in a registry that does not follow the host's own convention.
    clone_url: str | None = None
    # Declared build surface (components, sql_dialect), owned and consumed by forge.
    # syncer neither reads nor validates the shape; it is modelled only so the
    # registry schema documents what is actually in the file. Kept as a dict so
    # forge can extend it without touching syncer.
    toolchain: dict[str, Any] | None = None


class SyncerConfig(BaseModel):
    # Fallback owner for repos that don't name their own. Optional because a
    # registry can be entirely third-party — the exemplar clones each carry their
    # own upstream owner, so there is no single owner for the file.
    owner: str = ''
    host: str = 'https://github.com'
    # Directories to scan for repos that exist on disk but aren't registered.
    # A registry that isn't a claim over any directory leaves this empty.
    search_paths: list[str] = []
    # Subtrees inside search_paths that this registry explicitly does not claim,
    # so they are never reported as untracked. Registries are separate sets: the
    # exemplar clones under ~/code/refs belong to exemplar-repos.json, and work
    # repos belong to no personal registry at all.
    exclude_paths: list[str] = []
    # Clone URL shape for this registry, in place of the default '{host}/{owner}/{name}'.
    # Placeholders: {host}, {owner}, {name}. The default path cannot express every host —
    # scp-style SSH (git@host:owner/repo.git) has no slash after the host, and Bitbucket Data
    # Center wants the .git suffix — and a registry is one host, so this belongs here rather
    # than repeated as a clone_url on all thirty entries.
    url_template: str | None = None
    # Whether default-branch naming is ours to change. `syncer issues` flags repos still
    # defaulting to master only where it is — at a company the default branch is the org's
    # decision, so the check would fire on every repo forever with nothing to do about any of them.
    owns_branch_naming: bool = True
    repos: list[RepoConfig]

    @field_validator('url_template')
    @classmethod
    def validate_url_template(cls, template: str | None) -> str | None:
        if template is None:
            return template
        if '{name}' not in template:
            raise ValueError(f'url_template {template!r} must include {{name}}')
        try:
            template.format(host='host', owner='owner', name='name')
        except (KeyError, IndexError) as exc:
            raise ValueError(f'url_template {template!r} has an unknown placeholder: {exc}') from exc
        return template


class ToolConfig(BaseModel):
    """Machine-local tool config from ~/.config/syncer/config.toml.

    Policies live here (never in repos.json) because they are per-machine: the same
    repo can sync aggressively on an always-on box and report-only on a laptop.
    """

    repos_file: str | None = None
    default_policy: str = 'standard'
    policies: dict[str, Policy] = {}
    repo_overrides: dict[str, str] = {}
    # Ceiling on a single git call. Machine-local because it is a property of this box's network
    # — a corporate VPN fetching a large monorepo needs more headroom than a home connection.
    git_timeout: int = GIT_TIMEOUT_SECONDS


def _load_repos_file(path: Path) -> SyncerConfig:
    """Load the repo registry from a JSON file."""
    if not path.exists():
        console.print(f'[red]Repos file not found: {path}[/red]')
        sys.exit(1)
    data = json.loads(path.read_text())
    config = SyncerConfig(**data)
    config.repos.sort(key=lambda r: r.path)
    return config


def load_tool_config() -> ToolConfig:
    """Load the machine-local tool config, injecting each policy's name from its table key.

    Returns an empty ToolConfig when no config file exists. Malformed policies (unknown
    action names, bad scope, invalid rule keys) raise loudly via pydantic validation.
    """
    if not TOOL_CONFIG_PATH.exists():
        return ToolConfig()
    raw = tomllib.loads(TOOL_CONFIG_PATH.read_text())
    policies = {name: Policy(name=name, **body) for name, body in raw.get('policies', {}).items()}
    return ToolConfig(
        repos_file=raw.get('repos_file'),
        default_policy=raw.get('default_policy', 'standard'),
        policies=policies,
        repo_overrides=raw.get('repo_overrides', {}),
        git_timeout=raw.get('git_timeout', GIT_TIMEOUT_SECONDS),
    )


def resolve_clone_url(repo_config: RepoConfig, config: SyncerConfig) -> str:
    """Clone URL for one repo: per-repo override, then registry template, then the default
    '{host}/{owner}/{name}'."""
    if repo_config.clone_url:
        return repo_config.clone_url
    owner = repo_config.owner or config.owner
    if config.url_template:
        return config.url_template.format(host=config.host, owner=owner, name=repo_config.name)
    return f'{config.host}/{owner}/{repo_config.name}'


def resolve_policies(tool_config: ToolConfig) -> dict[str, Policy]:
    """Built-in policies overlaid with any user-defined policies of the same name."""
    merged = dict(BUILTIN_POLICIES)
    merged.update(tool_config.policies)
    return merged


def resolve_policy_name(repo_config: RepoConfig, tool_config: ToolConfig, cli_policy: str | None = None) -> str:
    """Resolve which policy applies to a repo (first hit wins):

    1. CLI --policy   2. machine repo_overrides   3. repos.json sync_policy hint
    4. machine default_policy   5. built-in 'standard'
    """
    if cli_policy:
        return cli_policy
    if repo_config.name in tool_config.repo_overrides:
        return tool_config.repo_overrides[repo_config.name]
    if repo_config.sync_policy:
        return repo_config.sync_policy
    return tool_config.default_policy or 'standard'


def get_repos_file_path(override: Path | None = None) -> Path:
    """Resolve the repos file path: explicit override, then tool config, then legacy."""
    if override is not None:
        return override.expanduser()
    tool_config = load_tool_config()
    if tool_config.repos_file:
        return Path(tool_config.repos_file).expanduser()

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


def resolve_registry(repos_file: Path | None = None) -> tuple[SyncerConfig, Path]:
    """Load a repo registry and report the file it came from. Each registry is a self-contained
    set of repos: passing a different file swaps the whole working set, it does not merge with
    the default. The path comes back because it is the registry's identity — what the per-registry
    event stream is keyed on."""
    path = get_repos_file_path(repos_file)
    return _load_repos_file(path), path


def resolve_config(repos_file: Path | None = None) -> SyncerConfig:
    """The registry alone, for callers that do not record history."""
    return resolve_registry(repos_file)[0]
