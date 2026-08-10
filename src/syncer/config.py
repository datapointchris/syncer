from __future__ import annotations

import json
import os
import sys
import tomllib
from pathlib import Path
from typing import Any
from typing import Literal
from typing import NamedTuple

from pydantic import BaseModel
from pydantic import ValidationError
from pydantic import field_validator
from pydantic import model_validator

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
# registry with other tools is an arrangement between them, so it belongs in repos_file on the
# machines that want it — never in the default.
DEFAULT_REPOS_FILE = CONFIG_DIR / 'repos.json'

# Run history is state, not data: it persists across runs, nobody authors it, and deleting it
# changes behaviour (stale-repo warnings restart) rather than merely costing a recompute.
STATE_DIR = xdg_state_home() / 'syncer'

# Where run history lived before the state move. Swept by migrate_legacy_events().
LEGACY_DATA_DIR = Path.home() / '.local' / 'share' / 'syncer'

# Two pairs, deliberately: STARTER_* is what `config init` writes, TEMPLATE_* is what
# `config example` prints.
#
# These were one pair, on the reasoning that the annotated example a user reads should be
# byte-for-byte the file `init` writes. That conflated *teaching* with *scaffolding*, and the cost
# was observed rather than theoretical: `init` shipped a `[policies.laptop]` block that
# `policy list` renders indistinguishably from a built-in, a `[repo_overrides]` entry for a repo
# nobody has, and three fake repos — so the very first `syncer` run printed three `would clone`
# lines for repos that never existed, teaching a new user on run one that this tool's output is
# noise. A scaffold should have nothing in it to delete.
#
# The invariant's actual purpose — a template cannot drift from the schema — survives, because the
# round-trip test in test_config_cmd.py parses all four back into their models, and the
# discoverability test still asserts every PrimaryState and Action appears in TEMPLATE_TOOL_CONFIG.

STARTER_TOOL_CONFIG = """\
# syncer tool config — machine-local. Every option, annotated: syncer config example config

default_policy = "standard"
"""

# No `repos_file` line, not even commented out. It is the one setting whose correct value differs
# per machine and whose wrong value fails every run outright rather than degrading — a config
# deployed from a shared source pointed one machine at a directory that only existed on another.
# A scaffold should not put the idea in front of someone unprompted.

STARTER_REGISTRY = """\
{
  "owner": "",
  "host": "https://github.com",
  "search_paths": [],
  "repos": []
}
"""

TEMPLATE_TOOL_CONFIG = """\
# syncer tool config — machine-local. Policies live here rather than in the repo registry
# because they are a property of this box: the same repo can sync aggressively on an always-on
# desktop and report-only on a laptop.

# Repo registry to read. Defaults to whatever `syncer config path registry` prints, which is the
# file `syncer config init` scaffolds. Point it elsewhere only when another tool reads the same
# registry, in which case every machine that has that file names the shared path here — a machine
# without it must leave this commented out, or every run fails on a path it cannot have.
# repos_file = "~/shared/repos.json"

# Policy for any repo that names no other. Built-ins: standard, observe, mirror.
default_policy = "standard"

# Ceiling on a single git call, in seconds; clones get five times this. Machine-local because it
# is a property of this box's network — a VPN fetching a large monorepo needs more headroom.
git_timeout = 120

# Per-repo policy, keyed by the name in the registry. Beats the registry's sync_policy hint,
# loses to --policy.
[repo_overrides]
"some-shared-repo" = "observe"

# Changing one decision does not need a policy of your own. A [policies.*] table is merged onto
# whatever that name already is, so naming a built-in patches it cell by cell and leaves the rest
# of its table alone. Here: prune a feature branch whose remote is gone. standard's own
# "default:gone" = "report" still wins on the default branch, because a role selector beats "*".
[policies.standard.rules]
"*:gone" = "delete_local"

# A policy of your own. Its name is the table key, so this one is `--policy laptop`. Every field
# below is optional; a table naming no built-in and no `extend` starts from the model defaults.
[policies.laptop]
# Start from an existing policy instead of restating its table. Only needed when the name is not
# already a built-in's — `[policies.standard]` patches standard without this.
extend = "standard"
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

# Branches nothing may publish to or destroy. fnmatch patterns, the same grammar the rule
# selectors use. Enforced in execute() before any action runs, so it is a hard guard rather
# than a matter of writing the right exact-name rule for every branch — under a fallback like
# "*:ahead" = "push", one forgotten branch is one stray local commit on a shared branch.
# push, rebase_push, set_upstream_push and delete_local are refused; fast_forward still runs,
# because advancing to what the upstream already contains publishes nothing and destroys
# nothing. `syncer policy show <name> --branch develop` marks exactly which actions this stops.
protected = ["develop", "dev", "uat", "prod", "release/*"]

# Branches to report on even when you have no local copy. A fetch already brings down every
# remote branch, but the pipeline only iterates local ones, so a long-lived branch you
# deliberately never check out is invisible — nothing tells you origin/prod moved. Purely
# informational: there is no local branch to sync, so no action is ever taken. Opt-in and empty
# by default, because every repo has remote branches you will never care about and a check that
# fires on all of them is one you learn to ignore. Browse one with `git log origin/<branch>`.
watch_remote = ["develop", "dev", "uat", "prod"]

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
    # Declared build surface (components, sql_dialect), owned and consumed by a separate tool.
    # syncer neither reads nor validates the shape; it is modelled only so the
    # registry schema documents what is actually in the file. Kept as a dict so
    # that tool can extend it without touching syncer.
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

    @model_validator(mode='after')
    def validate_resolved_url_is_well_formed(self) -> SyncerConfig:
        """Catch a doubled scheme, which the default `host` makes an easy mistake.

        `host` ships as 'https://github.com' — scheme included — so writing the obvious-looking
        template 'https://{host}/{owner}/{name}' yields 'https://https://github.com/...'. git
        then reports `Could not resolve host: https`, which names neither setting responsible.
        Structural, so it belongs here rather than in a check that has to reach the network.
        """
        if self.url_template is None:
            return self
        sample = self.url_template.format(host=self.host, owner=self.owner or 'owner', name='name')
        if sample.count('://') > 1:
            raise ValueError(
                f'url_template and host both supply a scheme, giving {sample!r} — drop the scheme from one of them (host is {self.host!r})'
            )
        return self


class ToolConfig(BaseModel):
    """Machine-local tool config from ~/.config/syncer/config.toml.

    Policies live here (never in repos.json) because they are per-machine: the same
    repo can sync aggressively on an always-on box and report-only on a laptop.
    """

    repos_file: str | None = None
    default_policy: str = 'standard'
    policies: dict[str, Policy] = {}
    # policy name -> the policy it was merged onto, for `policy show` to mark which rules a patch
    # changed. Empty for a policy defined from nothing.
    policy_bases: dict[str, str] = {}
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


def _load_repos_file(path: Path, source: str | None = None) -> SyncerConfig:
    """Load the repo registry from a JSON file.

    A missing registry names its own provenance, because the actionable fact is usually not that
    the file is absent but that something chose a path this machine was never going to have.
    """
    if not path.exists():
        error(f'Repo registry not found: {path}{registry_source_note(source)}')
        hint('Create an annotated one there with: syncer config init registry')
        # When something explicitly chose the path, creating a registry there is only half the
        # menu — a machine that was never going to have that path wants the other half.
        if source:
            hint(f'Or drop {source} to fall back to {DEFAULT_REPOS_FILE}')
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


# Keys a [policies.*] table may hold: every Policy field except the name (which is the table key)
# plus `extend`, which is config syntax rather than a property of a resolved policy. Checked
# explicitly because pydantic ignores unknown fields by default, so `extends = "standard"` would
# otherwise be dropped in silence — and a policy that silently did not inherit reads as syncer
# ignoring the whole block.
_POLICY_BODY_KEYS = (set(Policy.model_fields) - {'name'}) | {'extend'}


def _merged_policy_body(base: Policy | None, body: dict[str, Any]) -> dict[str, Any]:
    """A [policies.X] table applied over its base: only the keys actually present override.

    `rules` merges cell by cell rather than replacing the table, which is what makes patching one
    decision a one-line block. Merging at the raw-dict level is load-bearing: constructing a
    Policy from the body first would fill every absent field with a model default and clobber the
    base with it — patching `[policies.mirror.rules]` would silently reset mirror's scope from
    `all` to `tracked`.
    """
    if base is None:
        return {key: value for key, value in body.items() if key != 'extend'}
    merged = base.model_dump(exclude={'name'})
    merged['rules'] = {**base.rules, **body.get('rules', {})}
    merged.update({key: value for key, value in body.items() if key not in ('extend', 'rules')})
    return merged


def _build_policies(raw_policies: dict[str, Any], bases: dict[str, str] | None = None) -> dict[str, Policy]:
    """Resolve every [policies.*] table against the policy it patches or extends.

    A table named for a built-in patches that built-in; `extend` names a base for a table that
    is not. Resolution is recursive so a policy may extend one the same file defines, with the
    in-progress set turning a cycle into a readable error rather than a RecursionError.
    """
    resolved: dict[str, Policy] = {}
    in_progress: list[str] = []
    # Which policy each entry was merged onto. Config metadata rather than policy semantics, so
    # it stays off Policy and out of decide()'s reach — `policy show` needs it to say which rules
    # a patch actually changed, and the merge is what discards it.
    bases = bases if bases is not None else {}

    def build(name: str) -> Policy:
        if name in resolved:
            return resolved[name]
        body = raw_policies.get(name)
        if body is None:
            builtin = BUILTIN_POLICIES.get(name)
            if builtin is None:
                raise ConfigError([f'unknown policy {name!r}; known: {", ".join(sorted(BUILTIN_POLICIES))}'])
            return builtin
        if name in in_progress:
            raise ConfigError([f'policies.{name}: extend cycle ({" -> ".join([*in_progress, name])})'])

        unknown = sorted(set(body) - _POLICY_BODY_KEYS)
        if unknown:
            raise ConfigError([f'policies.{name}: unknown key {key!r}' for key in unknown])

        in_progress.append(name)
        try:
            base_name = body.get('extend')
            if base_name is None:
                # No `extend`: the base is the built-in of the same name, if there is one. This is
                # what makes patching a built-in and defining a new policy the same syntax.
                base = BUILTIN_POLICIES.get(name)
                if base is not None:
                    bases[name] = name
            else:
                try:
                    base = build(base_name)
                except ConfigError as exc:
                    raise ConfigError([f'policies.{name}: extend — {line}' for line in exc.problems]) from exc
                bases[name] = base_name
            try:
                resolved[name] = Policy(name=name, **_merged_policy_body(base, body))
            except ValidationError as exc:
                raise ConfigError(_validation_problems(exc, prefix=f'policies.{name}')) from exc
        finally:
            in_progress.pop()
        return resolved[name]

    for name in raw_policies:
        build(name)
    return resolved


def parse_tool_config(raw: dict[str, Any]) -> ToolConfig:
    """Build a ToolConfig from parsed TOML, injecting each policy's name from its table key.

    Split out from load_tool_config so `config validate` and the template round-trip test go
    through the same construction the real load does, rather than a second approximation of it.
    Policies are built one at a time so a bad rule reports which policy it is in — pydantic's
    own error names only `rules`, which is no help in a file holding several.
    """
    bases: dict[str, str] = {}
    policies = _build_policies(raw.get('policies', {}), bases)

    try:
        return ToolConfig(
            repos_file=raw.get('repos_file'),
            default_policy=raw.get('default_policy', 'standard'),
            policies=policies,
            policy_bases=bases,
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
    """Write the minimal starter config. Callers check for an existing file first.

    The starter, not the annotated template: the annotated one is reference material, printed by
    `config example`, and scaffolding a file whose contents you must mostly delete is worse than
    scaffolding nothing.
    """
    TOOL_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOOL_CONFIG_PATH.write_text(STARTER_TOOL_CONFIG)
    return TOOL_CONFIG_PATH


def init_registry(path: Path) -> Path:
    """Write an empty starter registry to the path syncer reads. Callers check for an existing
    file first: creating a registry that is absent is scaffolding, but rewriting one that exists
    would clobber shared infrastructure other tools may also read.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(STARTER_REGISTRY)
    return path


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
    """Built-in policies overlaid with the config's own, which are already merged onto their base.

    The merge happened in _build_policies, so a `[policies.standard]` entry here is the built-in
    with the file's cells applied — not a replacement for it.
    """
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


class RegistryLocation(NamedTuple):
    """A resolved registry path plus why syncer is looking there.

    The provenance travels with the path because the path alone is not enough to act on: a
    machine that inherits a `repos_file` naming a directory it does not have reads as syncer
    hard-coding someone else's layout, and no message said which file chose the path.
    """

    path: Path
    # None is the built-in default; anything else is a phrase naming what pointed here.
    source: str | None = None


def registry_source_note(source: str | None) -> str:
    """Parenthetical naming where a registry path came from; empty for the built-in default."""
    return f' (from {source})' if source else ''


def registry_location(tool_config: ToolConfig, override: Path | None = None) -> RegistryLocation:
    """Resolve the registry path: --repos-file, then config.toml's repos_file, then the default.

    Takes an already-loaded ToolConfig so `config validate` can resolve the registry from the
    config it just parsed and diagnosed, instead of loading the same broken file a second time.
    """
    if override is not None:
        return RegistryLocation(override.expanduser(), 'the --repos-file flag')
    if tool_config.repos_file:
        return RegistryLocation(Path(tool_config.repos_file).expanduser(), f'repos_file in {TOOL_CONFIG_PATH}')
    return RegistryLocation(DEFAULT_REPOS_FILE)


def get_repos_file_path(override: Path | None = None) -> Path:
    return registry_location(load_tool_config(), override).path


def resolve_registry(repos_file: Path | None = None) -> tuple[SyncerConfig, Path]:
    """Load a repo registry and report the file it came from. Each registry is a self-contained
    set of repos: passing a different file swaps the whole working set, it does not merge with
    the default. The path comes back because it is the registry's identity — what the per-registry
    event stream is keyed on."""
    location = registry_location(load_tool_config(), repos_file)
    return _load_repos_file(location.path, location.source), location.path


def resolve_config(repos_file: Path | None = None) -> SyncerConfig:
    """The registry alone, for callers that do not record history."""
    return resolve_registry(repos_file)[0]
