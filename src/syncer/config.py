from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any
from typing import Literal

from pydantic import BaseModel
from pydantic import ValidationError
from pydantic import field_validator

from syncer.output import error
from syncer.output import hint
from syncer.policy import BUILTIN_POLICIES
from syncer.policy import Policy
from syncer.repos import GIT_TIMEOUT_SECONDS


def xdg_config_home() -> Path:
    override = os.environ.get('XDG_CONFIG_HOME')
    return Path(override).expanduser() if override else Path.home() / '.config'


def xdg_state_home() -> Path:
    override = os.environ.get('XDG_STATE_HOME')
    return Path(override).expanduser() if override else Path.home() / '.local' / 'state'


CONFIG_DIR = xdg_config_home() / 'syncer'
TOOL_CONFIG_PATH = CONFIG_DIR / 'config.toml'

# Registry location when config.toml names none. Deliberately a syncer-owned path: sharing one
# registry with forge and indy (the fleet keeps it at ~/dev/repos.json) is an arrangement between
# those tools, so it belongs in repos_file on the machines that want it — never in the default.
DEFAULT_REPOS_FILE = CONFIG_DIR / 'repos.json'

# Run history is state, not data: it persists across runs, nobody authors it, and deleting it
# changes behaviour (stale-repo warnings restart) rather than merely costing a recompute.
STATE_DIR = xdg_state_home() / 'syncer'

# Where run history lived before the state move. Swept by migrate_legacy_events().
LEGACY_DATA_DIR = Path.home() / '.local' / 'share' / 'syncer'

# The single source for both `config init` and `config example`, so the annotated example a user
# reads is byte-for-byte the file `init` writes. Every option appears, exercised — the template
# doubles as side-by-side reference while editing. A round-trip test parses both of these into
# their models, which is what stops them drifting from the schema.

TEMPLATE_TOOL_CONFIG = """\
# syncer tool config — machine-local. Policies live here rather than in the repo registry
# because they are a property of this box: the same repo can sync aggressively on an always-on
# desktop and report-only on a laptop.

# Repo registry to read. Defaults to ~/.config/syncer/repos.json. Point it elsewhere only if the
# registry is shared with other tools — on the fleet, forge and indy read the same file, so it
# lives at ~/dev/repos.json and every machine names it here.
# repos_file = "~/dev/repos.json"

# Policy for any repo that names no other. Built-ins: standard, observe, mirror.
default_policy = "standard"

# Ceiling on a single git call, in seconds; clones get five times this. Machine-local because it
# is a property of this box's network — a VPN fetching a large monorepo needs more headroom.
git_timeout = 120

# Per-repo policy, keyed by the name in the registry. Beats the registry's sync_policy hint,
# loses to --policy.
[repo_overrides]
"some-shared-repo" = "observe"

# A custom policy. Its name is the table key, so this one is `--policy laptop`.
[policies.laptop]
# Which branches are evaluated: default | current | tracked | all
scope = "all"
# Prune remote-tracking refs on fetch, so a deleted upstream branch classifies as gone rather
# than sitting there looking synced forever.
prune = true
# Action when no rule below matches.
fallback = "report"
# Branch a gone branch must provably be integrated into before delete_local will touch it.
# Defaults to the repo's default branch. Set it where the trunk you merge into is not the
# default: a develop-centric flow never makes a feature branch an ancestor of main, so the
# default-branch guard would refuse every genuinely merged branch forever.
merge_target = "develop"

# Rules are "<selector>:<state>" = "<action>". Run `syncer policy show laptop` to see the
# decision this table actually produces for every state — that matrix is computed from the
# rules engine itself, so it cannot drift from what --apply will do.
#
#   selector   exact branch name  >  glob  >  role (default > current)  >  "*"
#   state      synced | ahead | behind | diverged | no_upstream | gone | detached
#   action     skip | report | fast_forward | push | rebase_push | set_upstream_push
#              | delete_local | prompt | pull_ff | ff_ref
#
# Name intents, not mechanisms: fast_forward dispatches to pull_ff (merge --ff-only, which needs
# the branch checked out) or ff_ref (update-ref, which needs it not checked out). A rule naming
# either mechanism directly is refused for half of all checkout states.
[policies.laptop.rules]
"main:diverged"       = "rebase_push"
"release/*:ahead"     = "report"
"default:synced"      = "skip"
"default:ahead"       = "push"
"current:no_upstream" = "set_upstream_push"
"*:behind"            = "fast_forward"
"*:ahead"             = "report"
"*:diverged"          = "report"
"*:detached"          = "report"
"*:gone"              = "delete_local"
"""

# JSON carries no comments, so the annotation rides in the `description` field of each entry —
# a real field, and the one place prose already belongs.
TEMPLATE_REGISTRY = """\
{
  "owner": "your-github-username",
  "host": "https://github.com",
  "url_template": "{host}/{owner}/{name}",
  "owns_branch_naming": true,
  "search_paths": ["~/code", "~/tools"],
  "exclude_paths": ["~/code/refs"],
  "repos": [
    {
      "name": "example-repo",
      "path": "~/code/example-repo",
      "status": "active",
      "description": "status is active (the default), dormant, or retired; retired repos are skipped"
    },
    {
      "name": "example-clone",
      "path": "~/code/refs/example-clone",
      "status": "dormant",
      "owner": "upstream-owner",
      "description": "owner overrides the registry owner; a repo that is not ours skips the 'using master' check"
    },
    {
      "name": "example-work-repo",
      "path": "~/code/work/example-work-repo",
      "status": "active",
      "sync_policy": "observe",
      "clone_url": "ssh://git@bitbucket.example.com:7999/proj/example-work-repo.git",
      "description": "sync_policy is a portable hint and must name a built-in; clone_url overrides url_template for one repo"
    }
  ]
}
"""


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


class ConfigError(Exception):
    """A config file that cannot be loaded, carrying one readable line per problem.

    Every load path raises this instead of letting a pydantic ValidationError or a
    TOMLDecodeError escape, so a bad key reads as `policies.laptop.rules: ...` rather than a
    traceback — and so `config validate` and an ordinary run report the same text.
    """

    def __init__(self, problems: list[str]) -> None:
        super().__init__('\n'.join(problems))
        self.problems = problems


def _validation_problems(exc: ValidationError, prefix: str = '') -> list[str]:
    """One line per pydantic error: the key path that failed, then why."""
    problems = []
    for err in exc.errors():
        parts = [prefix, *(str(part) for part in err['loc'])]
        location = '.'.join(part for part in parts if part) or '(root)'
        # Our own field_validators already write a complete sentence; pydantic's 'Value error, '
        # prefix just repeats what the red text and the key path already said.
        problems.append(f'{location}: {err["msg"].removeprefix("Value error, ")}')
    return problems


def _report_config_error(path: Path, exc: ConfigError) -> None:
    error(f'{path} is invalid:')
    for problem in exc.problems:
        hint(f'  {problem}')


def _load_repos_file(path: Path) -> SyncerConfig:
    """Load the repo registry from a JSON file."""
    if not path.exists():
        error(f'Repo registry not found: {path}')
        hint(f'Scaffold one with: syncer config example --registry > {path}')
        sys.exit(1)
    try:
        config = parse_registry(json.loads(path.read_text()))
    except json.JSONDecodeError as exc:
        _report_config_error(path, ConfigError([f'not valid JSON: {exc}']))
        sys.exit(1)
    except ConfigError as exc:
        _report_config_error(path, exc)
        sys.exit(1)
    config.repos.sort(key=lambda r: r.path)
    return config


def parse_registry(raw: dict[str, Any]) -> SyncerConfig:
    """Build a SyncerConfig from parsed JSON, as a ConfigError on failure."""
    try:
        return SyncerConfig(**raw)
    except ValidationError as exc:
        raise ConfigError(_validation_problems(exc)) from exc


def parse_tool_config(raw: dict[str, Any]) -> ToolConfig:
    """Build a ToolConfig from parsed TOML, injecting each policy's name from its table key.

    Split out from load_tool_config so `config validate` and the template round-trip test go
    through the same construction the real load does, rather than a second approximation of it.
    Policies are built one at a time so a bad rule reports which policy it is in — pydantic's
    own error names only `rules`, which is no help in a file holding several.
    """
    policies = {}
    for name, body in raw.get('policies', {}).items():
        try:
            policies[name] = Policy(name=name, **body)
        except ValidationError as exc:
            raise ConfigError(_validation_problems(exc, prefix=f'policies.{name}')) from exc

    try:
        return ToolConfig(
            repos_file=raw.get('repos_file'),
            default_policy=raw.get('default_policy', 'standard'),
            policies=policies,
            repo_overrides=raw.get('repo_overrides', {}),
            git_timeout=raw.get('git_timeout', GIT_TIMEOUT_SECONDS),
        )
    except ValidationError as exc:
        raise ConfigError(_validation_problems(exc)) from exc


def read_tool_config() -> dict[str, Any]:
    """Parse config.toml to raw TOML, as a ConfigError on a syntax error."""
    try:
        return tomllib.loads(TOOL_CONFIG_PATH.read_text())
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError([f'not valid TOML: {exc}']) from exc


def load_tool_config() -> ToolConfig:
    """Load the machine-local tool config. Returns an empty ToolConfig when no file exists.

    A broken file prints exactly what is wrong with it and exits, rather than a traceback or a
    generic 'run validate' — the error the user needs is already in hand here.
    """
    if not TOOL_CONFIG_PATH.exists():
        return ToolConfig()
    try:
        return parse_tool_config(read_tool_config())
    except ConfigError as exc:
        _report_config_error(TOOL_CONFIG_PATH, exc)
        sys.exit(1)


def init_tool_config() -> Path:
    """Write the annotated tool config template. Callers check for an existing file first."""
    TOOL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOOL_CONFIG_PATH.write_text(TEMPLATE_TOOL_CONFIG)
    return TOOL_CONFIG_PATH


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


def registry_path_for(tool_config: ToolConfig, override: Path | None = None) -> Path:
    """Resolve the registry path: --repos-file, then config.toml's repos_file, then the default.

    Takes an already-loaded ToolConfig so `config validate` can resolve the registry from the
    config it just parsed and diagnosed, instead of loading the same broken file a second time.
    """
    if override is not None:
        return override.expanduser()
    if tool_config.repos_file:
        return Path(tool_config.repos_file).expanduser()
    return DEFAULT_REPOS_FILE


def get_repos_file_path(override: Path | None = None) -> Path:
    return registry_path_for(load_tool_config(), override)


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
