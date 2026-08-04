"""`syncer doctor` — is this machine able to run syncer at all, and if not, which part is wrong.

The gap it fills: `config validate` checks **structure**, `issues` checks **reality** (do the
registry's paths exist), and neither could answer the question a fresh machine actually asks,
which is why nothing works. A first run that fails had no way to distinguish a missing
credential from a mis-pointed registry from a host that was never reachable.

Two rules shape every check below.

**Checks run in prerequisite order and the first FAIL is the actionable one.** There is no value
in reporting that a registry has no repos when git is not installed.

**Nothing here assumes GitHub.** Reachability is proved with `git ls-remote` against the URL the
registry actually resolves to, so a corporate Bitbucket, an internal GitLab, or a plain SSH host
are all first-class — `gh` is never invoked.

This module never writes anything, unlike `config edit`, which seeds a template.
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass
from dataclasses import field
from enum import StrEnum
from pathlib import Path

from rich.markup import escape

from syncer.config import DEFAULT_REPOS_FILE
from syncer.config import STATE_DIR
from syncer.config import TOOL_CONFIG_PATH
from syncer.config import ConfigError
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import parse_registry
from syncer.config import parse_tool_config
from syncer.config import read_tool_config
from syncer.config import registry_location
from syncer.config import registry_source_note
from syncer.config import resolve_clone_url
from syncer.config import resolve_policies
from syncer.config import resolve_policy_name
from syncer.diagnose import classify_failure
from syncer.diagnose import hint_lines
from syncer.diagnose import remote_host
from syncer.output import ICON_ERR
from syncer.output import ICON_OK
from syncer.output import ICON_WARN
from syncer.output import err_console
from syncer.output import error
from syncer.output import hint
from syncer.output import success
from syncer.repos import GitFailure
from syncer.repos import run_command

# Deliberately not git_timeout. That is sized for fetching a large monorepo over a VPN (120s by
# default); a diagnostic that hangs for two minutes per host is one nobody waits for.
PROBE_TIMEOUT_SECONDS = 20

# One probe per host, not per repo, and never more than this many — doctor answers "can this
# machine reach its remotes", which three hosts establish as well as thirty.
MAX_HOSTS_PROBED = 3

# Placeholder identities shipped in TEMPLATE_REGISTRY. Matched by value rather than by comparing
# the whole file, so a registry half-edited from the template is still caught.
_PLACEHOLDER_OWNER = 'your-github-username'
_PLACEHOLDER_REPOS = {'example-repo', 'example-clone', 'example-work-repo'}


class Status(StrEnum):
    OK = 'ok'
    WARN = 'warn'
    FAIL = 'fail'


@dataclass
class Check:
    """One diagnostic result. `detail` lines are printed under the summary, indented."""

    name: str
    status: Status
    summary: str
    detail: list[str] = field(default_factory=list)
    # What to do about it. Kept apart from detail so the renderer can mark it with an arrow.
    hints: list[str] = field(default_factory=list)


def _git_present() -> Check:
    binary = shutil.which('git')
    if binary is None:
        return Check(
            'git',
            Status.FAIL,
            'git is not on PATH',
            hints=['install git — every other check below depends on it'],
        )
    version = run_command(['git', '--version'], timeout=PROBE_TIMEOUT_SECONDS)
    return Check('git', Status.OK, version.stdout.strip() or 'git found', detail=[binary])


def _paths(tool_config: ToolConfig, override: Path | None) -> Check:
    """Always OK — this is context, not a verdict.

    It is also the single most valuable line on a broken machine: a `repos_file` inherited from
    a shared dotfiles bucket pointed the work box at a directory it could never have, and
    nothing in syncer's output named what had chosen the path, so the tool looked like it had
    someone else's layout hard-coded.
    """
    location = registry_location(tool_config, override)
    config_note = '' if TOOL_CONFIG_PATH.exists() else '  (absent — built-in defaults)'
    registry_note = '' if location.path.exists() else '  (absent)'
    return Check(
        'paths',
        Status.OK,
        'resolved paths',
        detail=[
            f'config    {TOOL_CONFIG_PATH}{config_note}',
            f'registry  {location.path}{registry_source_note(location.source)}{registry_note}',
            f'state     {STATE_DIR}',
        ],
    )


def _tool_config_parses() -> tuple[Check, ToolConfig]:
    """Parse config.toml the way a real run does, and hand back what it produced.

    Full parse, not just the TOML syntax: a file that is syntactically fine but names a bad
    policy rule would otherwise pass here and then sys.exit inside load_tool_config, turning a
    diagnostic into the crash it exists to explain.
    """
    if not TOOL_CONFIG_PATH.exists():
        return Check('config', Status.OK, 'no config.toml — using built-in defaults'), ToolConfig()
    try:
        tool_config = parse_tool_config(read_tool_config())
    except ConfigError as exc:
        return Check('config', Status.FAIL, f'{TOOL_CONFIG_PATH} is invalid', detail=list(exc.problems)), ToolConfig()
    return Check('config', Status.OK, f'{TOOL_CONFIG_PATH} parses'), tool_config


def _registry_loads(path: Path, source: str | None) -> tuple[Check, SyncerConfig | None]:
    if not path.exists():
        return (
            Check(
                'registry',
                Status.FAIL,
                f'no registry at {path}{registry_source_note(source)}',
                hints=[
                    'create one there: syncer config init registry',
                    *([f'or drop {source} to fall back to {DEFAULT_REPOS_FILE}'] if source else []),
                ],
            ),
            None,
        )
    try:
        config = parse_registry(json.loads(path.read_text()))
    except json.JSONDecodeError as exc:
        return Check('registry', Status.FAIL, f'{path} is not valid JSON', detail=[str(exc)]), None
    except ConfigError as exc:
        return Check('registry', Status.FAIL, f'{path} is invalid', detail=list(exc.problems)), None
    active = [repo for repo in config.repos if repo.status != 'retired']
    return Check('registry', Status.OK, f'{len(active)} active repos in {path}'), config


def _not_the_shipped_template(config: SyncerConfig) -> Check:
    """A registry of placeholders validates clean and then reports three bogus `would clone`
    lines on the very first run, which teaches a new user that this tool's output is noise."""
    found = [repo.name for repo in config.repos if repo.name in _PLACEHOLDER_REPOS]
    if config.owner == _PLACEHOLDER_OWNER:
        found.append(f'owner={_PLACEHOLDER_OWNER}')
    if not found:
        return Check('identity', Status.OK, 'registry has been edited from the template')
    # FAIL, not WARN: nobody keeps `your-github-username` on purpose, and a run against these
    # would try to clone repos that were never real.
    return Check(
        'identity',
        Status.FAIL,
        'registry still holds template placeholders',
        detail=found,
        hints=['replace them with your repos: syncer config edit registry'],
    )


def _identity_is_usable(config: SyncerConfig) -> Check:
    """An empty `owner` is only a problem for repos that would actually resolve through it.

    An all-third-party registry (every entry naming its own owner) and a freshly scaffolded
    empty one are both legitimate, so this cannot fire on `owner == ''` alone.
    """
    if config.owner or config.url_template:
        return Check('urls', Status.OK, 'clone URLs resolve')
    affected = [repo.name for repo in config.repos if not repo.clone_url and not repo.owner]
    if not affected:
        return Check('urls', Status.OK, 'clone URLs resolve')
    sample = resolve_clone_url(next(repo for repo in config.repos if repo.name == affected[0]), config)
    return Check(
        'urls',
        Status.FAIL,
        f'registry `owner` is empty, so {len(affected)} repos build a broken URL',
        detail=[f'e.g. {affected[0]} → {sample}', *affected[1:6]],
        hints=[
            'set `owner` in the registry, or give each repo its own `owner`/`clone_url`',
            'this also reports a false `origin mismatch` on every repo that is already cloned',
        ],
    )


def _hosts_to_probe(config: SyncerConfig) -> list[tuple[str, str]]:
    """One (host, url) per distinct host, in registry order, capped."""
    seen: dict[str, str] = {}
    for repo in config.repos:
        if repo.status == 'retired':
            continue
        url = resolve_clone_url(repo, config)
        seen.setdefault(remote_host(url), url)
    return list(seen.items())[:MAX_HOSTS_PROBED]


def _reachable(config: SyncerConfig) -> list[Check]:
    """Prove reachability the same way a clone would, against the URL the registry resolves to.

    `git ls-remote` rather than `gh auth status` or an https HEAD: it exercises the real
    transport, the real credential and the real host key, and it works identically for GitHub,
    Bitbucket Data Center, an internal GitLab and a bare SSH host.
    """
    hosts = _hosts_to_probe(config)
    if not hosts:
        return [Check('reach', Status.OK, 'no repos to reach')]

    checks = []
    for host, url in hosts:
        label = host or url
        result = run_command(['git', 'ls-remote', '--exit-code', '-h', url], timeout=PROBE_TIMEOUT_SECONDS)
        if result.returncode == 0:
            checks.append(Check('reach', Status.OK, f'{label} reachable'))
            continue
        failure = GitFailure(argv=('ls-remote', url), returncode=result.returncode, stderr=result.stderr.strip())
        cause = classify_failure(failure)
        what = cause.value.replace('_', ' ') if cause else 'failed'
        checks.append(
            Check(
                'reach',
                Status.FAIL,
                f'cannot reach {label}: {what}',
                detail=[line for line in failure.stderr.splitlines() if line.strip()],
                hints=hint_lines(cause, url),
            )
        )
    return checks


def _repos_on_disk(config: SyncerConfig, reachable: bool) -> Check:
    """Pure stat, no git. Reports the fresh-machine case as one fact rather than N warnings."""
    active = [repo for repo in config.repos if repo.status != 'retired']
    if not active:
        return Check(
            'clones',
            Status.WARN,
            'the registry lists no repos yet',
            hints=['add them by hand, or scan a directory you already have: syncer config scan ~/code'],
        )
    missing = [repo for repo in active if not Path(repo.path).expanduser().exists()]
    if not missing:
        return Check('clones', Status.OK, f'all {len(active)} repos present')
    summary = f'{len(active) - len(missing)} of {len(active)} repos present, {len(missing)} missing'
    hints = ['clone them: syncer --apply'] if reachable else ['fix the reachability failure above first — cloning will fail the same way']
    return Check('clones', Status.WARN, summary, detail=[repo.name for repo in missing[:6]], hints=hints)


def _policies_resolve(config: SyncerConfig, tool_config: ToolConfig) -> Check:
    policies = resolve_policies(tool_config)
    default = tool_config.default_policy or 'standard'
    if default not in policies:
        return Check(
            'policy',
            Status.FAIL,
            f'default_policy {default!r} does not exist',
            hints=[f'known policies: {", ".join(sorted(policies))}'],
        )
    unknown = {resolve_policy_name(repo, tool_config) for repo in config.repos if resolve_policy_name(repo, tool_config) not in policies}
    if unknown:
        return Check(
            'policy',
            Status.WARN,
            f'{len(unknown)} repos name a policy this machine does not have',
            detail=sorted(unknown),
            # config.toml is machine-local, so a registry sync_policy hint must name a built-in.
            hints=['a registry `sync_policy` must name a built-in, since config.toml is not shared'],
        )
    return Check('policy', Status.OK, f'default policy {default!r}, {len(policies)} available')


def run_doctor(override: Path | None = None) -> list[Check]:
    """Every check, in prerequisite order, stopping where continuing would be meaningless."""
    checks = [_git_present()]
    if checks[-1].status is Status.FAIL:
        return checks

    tool_config_check, tool_config = _tool_config_parses()
    # Paths are printed before the config verdict even though the config had to parse to resolve
    # them: on a broken machine "which files am I even reading" sits under every other question.
    location = registry_location(tool_config, override)
    checks.append(_paths(tool_config, override))
    checks.append(tool_config_check)
    if tool_config_check.status is Status.FAIL:
        return checks

    registry_check, config = _registry_loads(location.path, location.source)
    checks.append(registry_check)
    if config is None:
        return checks

    template_check = _not_the_shipped_template(config)
    identity_check = _identity_is_usable(config)
    checks.extend([template_check, identity_check])

    # Probing URLs built from placeholders or an empty owner reports a network problem for what
    # is really an unedited config — two failures where the answer is one, and the noisier one
    # is the wrong one to act on.
    if template_check.status is Status.FAIL or identity_check.status is Status.FAIL:
        # `clones` is skipped for the same reason: counting how many placeholder repos exist on
        # disk is a number about nothing.
        checks.append(Check('reach', Status.WARN, 'skipped — the registry does not name real repos yet'))
        checks.append(Check('clones', Status.WARN, 'skipped — nothing to clone until the registry is real'))
        checks.append(_policies_resolve(config, tool_config))
        return checks

    reach_checks = _reachable(config)
    checks.extend(reach_checks)
    reachable = all(check.status is Status.OK for check in reach_checks)
    checks.append(_repos_on_disk(config, reachable))
    checks.append(_policies_resolve(config, tool_config))
    return checks


_MARK = {Status.OK: (ICON_OK, 'green'), Status.WARN: (ICON_WARN, 'yellow'), Status.FAIL: (ICON_ERR, 'red')}


def render_doctor(checks: list[Check]) -> None:
    """Print the report. Goes to stderr — a diagnostic is not data anyone pipes into jq."""
    for check in checks:
        icon, color = _MARK[check.status]
        err_console.print(f'[{color}]{icon}  {check.name:9}[/{color}] {escape(check.summary)}', soft_wrap=True)
        for line in check.detail:
            hint(f'              {escape(line)}')
        for line in check.hints:
            hint(f'            → {escape(line)}')
    err_console.print()

    failed = [check for check in checks if check.status is Status.FAIL]
    if failed:
        error(f'{len(failed)} check(s) failed — this machine is not ready to sync')
        return
    if any(check.status is Status.WARN for check in checks):
        hint('ready, with warnings above')
        return
    success('everything checks out')


def doctor_exit_code(checks: list[Check]) -> int:
    """1 on any FAIL, 0 otherwise.

    WARN stays 0 deliberately: an un-cloned repo set and a registry you have not filled in yet
    are states you can be in on purpose, while a registry that will not load or a host that
    cannot be reached means the next command is going to fail. `syncer doctor && syncer --apply`
    has to stop for the second and not the first.
    """
    return 1 if any(check.status is Status.FAIL for check in checks) else 0
