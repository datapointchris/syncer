"""`syncer config` — the tool config and the registry it points at.

Both files, because config.toml's whole job is naming the registry: a command that showed one
and not the other would answer half of every question about which repos are in play.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import subprocess
from collections import Counter
from pathlib import Path
from typing import Annotated
from typing import Any

import typer
from rich.syntax import Syntax

from syncer.config import DEFAULT_REPOS_FILE
from syncer.config import STATE_DIR
from syncer.config import TEMPLATE_REGISTRY
from syncer.config import TEMPLATE_TOOL_CONFIG
from syncer.config import TOOL_CONFIG_PATH
from syncer.config import ConfigError
from syncer.config import RegistryLocation
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import get_repos_file_path
from syncer.config import init_registry
from syncer.config import init_tool_config
from syncer.config import load_tool_config
from syncer.config import parse_registry
from syncer.config import parse_tool_config
from syncer.config import read_tool_config
from syncer.config import registry_location
from syncer.config import registry_source_note
from syncer.config import resolve_policies
from syncer.config import resolve_policy_name
from syncer.config import write_registry
from syncer.output import console
from syncer.output import emit_json
from syncer.output import error
from syncer.output import hint
from syncer.output import success
from syncer.policy import BUILTIN_POLICIES
from syncer.repos import Repo
from syncer.repos import find_untracked_repos
from syncer.repos import normalize_remote_url

config_app = typer.Typer(
    no_args_is_help=True,
    help='Inspect and edit the tool config and the repo registry it points at.',
)


FILES = ('config', 'registry')


def _requested_files(which: str | None) -> tuple[str, ...]:
    """The files a command was asked about. Naming none means both, matching `config path`."""
    if which is None:
        return FILES
    if which not in FILES:
        raise typer.BadParameter(f'unknown file {which!r}; expected one of: {", ".join(FILES)}')
    return (which,)


@config_app.command('init')
def config_init(
    which: Annotated[str | None, typer.Argument(help='One of: config, registry. Omit to create both.')] = None,
    repos_file: Annotated[
        Path | None,
        typer.Option('--repos-file', '-c', help='Create the registry at this path instead of the resolved one'),
    ] = None,
) -> None:
    """Create a minimal tool config and an empty repo registry, at the paths syncer reads.

    Minimal on purpose — nothing in either file needs deleting. The fully annotated versions are
    reference material: `syncer config example config` and `syncer config example registry`.

    Idempotent: a file that already exists is reported and left exactly as it was, never
    rewritten — the registry is shared infrastructure other tools may also read, and syncer
    creates one that is absent but modifies no existing one.
    """
    files = _requested_files(which)

    if 'config' in files:
        if TOOL_CONFIG_PATH.exists():
            hint(f'config already exists at {TOOL_CONFIG_PATH} — left as is')
        else:
            success(f'created config at {init_tool_config()}')

    if 'registry' in files:
        # Resolved after the write above, so a config.toml created moments ago is what names the
        # registry path — not the absence it was resolved against before.
        location = registry_location(load_tool_config(), repos_file)
        note = registry_source_note(location.source)
        if location.path.exists():
            hint(f'registry already exists at {location.path}{note} — left as is')
        else:
            success(f'created registry at {init_registry(location.path)}{note}')
            hint('it is empty — add your repos with `syncer config edit registry`, then: syncer doctor')
            hint('every option, annotated: syncer config example registry')


def _registry_has_repos(path: Path) -> bool:
    """Whether a registry holds anything worth protecting.

    The no-clobber rule guards *content* — a registry other tools may also read — not the empty
    scaffold `config init` writes. Refusing to fill that one made the documented flow
    (`config init` then `config scan --write`) contradict itself. Unreadable counts as content:
    a file syncer cannot parse is the last one to overwrite silently.
    """
    try:
        return bool(json.loads(path.read_text()).get('repos'))
    except (OSError, ValueError):
        return True


def _entry_from(path: Path) -> tuple[dict[str, str], str, str]:
    """One registry entry for a repo on disk, plus the (host, owner) its origin implies.

    Derived from the clone's own origin rather than assumed, so a scan across a directory
    holding both personal and third-party clones produces entries that are individually right.
    """
    repo = Repo(name=path.name, path=path, owner='', host='')
    origin = repo.origin_url
    home = str(Path.home())
    entry = {'name': path.name, 'path': str(path).replace(home, '~', 1), 'status': 'active'}
    if not origin:
        return entry, '', ''
    host, _, tail = normalize_remote_url(origin).partition('/')
    owner = tail.rpartition('/')[0]
    if not host or not owner:
        # A URL the default {host}/{owner}/{name} shape cannot express. The real one is right
        # here, so record it rather than emitting an entry whose URL cannot be rebuilt.
        entry['clone_url'] = origin
        return entry, '', ''
    return entry, host, owner


@config_app.command('scan')
def config_scan(
    paths: Annotated[list[Path], typer.Argument(help='Directories to scan for git repos.')],
    write: Annotated[bool, typer.Option('--write', help='Write the registry instead of printing it')] = False,
) -> None:
    """Build registry entries from the repos already on disk, and print them.

    The fresh-machine shortcut: naming thirty repos by hand is the step that makes setting this
    up feel like work, and the answer is already sitting in the filesystem. Each entry's owner
    and host come from that clone's real origin, so a directory holding both your repos and
    third-party clones scans correctly.

    Prints to stdout by default so you can review it — `syncer config scan ~/code > repos.json`
    works, and so does piping it through jq. `--write` puts it at the path syncer reads, and
    only when no registry is there: this file may be shared with other tools, so syncer creates
    one that is absent and never rewrites one that exists.
    """
    found: list[Path] = []
    for path in paths:
        expanded = path.expanduser()
        if not expanded.is_dir():
            error(f'not a directory: {expanded}')
            raise typer.Exit(2)
        found.extend(find_untracked_repos(expanded, known_paths=set()))

    # One pass — each _entry_from shells out to git, so re-deriving would double the cost of a
    # scan over a directory holding seventy repos.
    scanned = [_entry_from(repo_path) for repo_path in sorted(found)]
    hosts = Counter(host for _, host, _ in scanned if host)
    owners = Counter(owner for _, _, owner in scanned if owner)

    # The commonest host/owner become the registry defaults and every entry that disagrees keeps
    # its own — which is what the per-repo `owner` override is already for.
    host = f'https://{hosts.most_common(1)[0][0]}' if hosts else 'https://github.com'
    owner = owners.most_common(1)[0][0] if owners else ''
    entries = []
    for entry, _, entry_owner in scanned:
        if entry_owner and entry_owner != owner:
            entry['owner'] = entry_owner
        entries.append(entry)

    registry = {
        'owner': owner,
        'host': host,
        'search_paths': [str(p.expanduser()).replace(str(Path.home()), '~', 1) for p in paths],
        'repos': entries,
    }
    rendered = json.dumps(registry, indent=2) + '\n'

    if not write:
        print(rendered, end='')
        hint(f'found {len(entries)} repos — review, then redirect to {get_repos_file_path()} or re-run with --write')
        return

    location = registry_location(load_tool_config())
    if location.path.exists() and _registry_has_repos(location.path):
        error(f'registry already lists repos at {location.path}{registry_source_note(location.source)} — not overwriting')
        hint('review the scan and merge it by hand: syncer config scan <paths>')
        raise typer.Exit(1)
    write_registry(location.path, rendered)
    success(f'wrote {len(entries)} repos to {location.path}')


@config_app.command('example')
def config_example(
    which: Annotated[str, typer.Argument(help='Which file to print: config or registry.')] = 'config',
) -> None:
    """Print a fully annotated example of one file, showing every available option.

    Reference material, not a scaffold — every option appears here, exercised, so this is the
    thing to read while editing the real file. `syncer config init` writes a *minimal* starter
    instead, because a scaffold you have to delete most of is worse than an empty one.
    Syntax-highlighted on a terminal, plain text when redirected, so
    `syncer config example registry > repos.json` stays clean.
    """
    _requested_files(which)
    template, lexer = (TEMPLATE_REGISTRY, 'json') if which == 'registry' else (TEMPLATE_TOOL_CONFIG, 'toml')
    if console.is_terminal:
        console.print(Syntax(template, lexer, background_color='default', word_wrap=True))
        # A template on screen with no path is the half-answer: it says what to write and never
        # where. On stderr, so a redirect still captures only the template.
        target = TOOL_CONFIG_PATH if which == 'config' else get_repos_file_path()
        hint(f'reference only — the file syncer reads is {target}; `syncer config edit {which}` opens it')
    else:
        print(template, end='')


@config_app.command('path')
def config_path(
    which: Annotated[str | None, typer.Argument(help='One of: config, registry, state. Omit to print all three.')] = None,
    as_json: Annotated[bool, typer.Option('--json', help='Output as JSON to stdout.')] = False,
) -> None:
    """Print the resolved config, registry, and state paths, whether or not they exist.

    Naming one prints it bare, for shell substitution:
    `cd "$(dirname "$(syncer config path registry)")"`.
    """
    paths = {'config': TOOL_CONFIG_PATH, 'registry': get_repos_file_path(), 'state': STATE_DIR}

    if which is not None:
        if which not in paths:
            error(f'unknown path {which!r}; expected one of: {", ".join(paths)}')
            raise typer.Exit(2)
        # Bare print, no markup or ANSI, so command substitution captures the path and nothing else.
        print(paths[which])
        return

    if as_json:
        emit_json({name: str(path) for name, path in paths.items()})
        return
    for name, path in paths.items():
        print(f'{name:<9} {path}')


@config_app.command('show')
def config_show(
    repos_file: Annotated[
        Path | None,
        typer.Option('--repos-file', '-c', help='Use a different repo registry; replaces the default set entirely'),
    ] = None,
    as_json: Annotated[bool, typer.Option('--json', help='Output as JSON to stdout.')] = False,
) -> None:
    """Show the effective settings, including the policy that resolves for every repo.

    The per-repo policy is the point: resolution runs --policy → repo_overrides → the registry's
    sync_policy hint → default_policy → built-in standard, and this is the only place that
    answers which one actually won.
    """
    tool_config = load_tool_config()
    registry_path, registry_source = registry_location(tool_config, repos_file)
    known_policies = resolve_policies(tool_config)

    registry: SyncerConfig | None = None
    if registry_path.exists():
        try:
            registry = parse_registry(json.loads(registry_path.read_text()))
        except (json.JSONDecodeError, ConfigError) as exc:
            # Show what it can: the paths and machine settings are still worth answering when
            # it is the registry that is broken.
            error(f'{registry_path} could not be read: {exc}')

    repos: list[dict[str, Any]] = []
    for repo_config in registry.repos if registry else []:
        policy_name = resolve_policy_name(repo_config, tool_config)
        repos.append(
            {
                'name': repo_config.name,
                'path': repo_config.path,
                'status': repo_config.status,
                'policy': policy_name,
                'source': _policy_source(repo_config, tool_config),
                'resolves': policy_name in known_policies,
            }
        )

    if as_json:
        emit_json(
            {
                'config_file': str(TOOL_CONFIG_PATH),
                'config_exists': TOOL_CONFIG_PATH.exists(),
                'registry_file': str(registry_path),
                'registry_exists': registry_path.exists(),
                'registry_source': registry_source,
                'state_dir': str(STATE_DIR),
                'default_policy': tool_config.default_policy,
                'git_timeout': tool_config.git_timeout,
                'policies': sorted(known_policies),
                'repos': repos,
            }
        )
        return

    # soft_wrap so a long path stays on one line and survives a copy-paste.
    console.print(
        f'[bold]config[/bold]    {TOOL_CONFIG_PATH}{"" if TOOL_CONFIG_PATH.exists() else "  (absent — built-in defaults)"}',
        soft_wrap=True,
    )
    console.print(
        f'[bold]registry[/bold]  {registry_path}{registry_source_note(registry_source)}{"" if registry_path.exists() else "  (absent)"}',
        soft_wrap=True,
    )
    console.print(f'[bold]state[/bold]     {STATE_DIR}', soft_wrap=True)
    console.print()
    console.print(f'[bold]default_policy[/bold]  {tool_config.default_policy}')
    console.print(f'[bold]git_timeout[/bold]     {tool_config.git_timeout}s')
    console.print(f'[bold]policies[/bold]        {", ".join(sorted(known_policies))}')

    if not repos:
        return
    console.print()
    console.print(f'[bold]{len(repos)} repos[/bold]')
    width = max(len(repo['name']) for repo in repos)
    for repo in repos:
        marker = '' if repo['resolves'] else '  [red](unknown policy)[/red]'
        console.print(f'  {repo["name"]:<{width}}  {repo["policy"]} [cyan]({repo["source"]})[/cyan]{marker}')


def _policy_source(repo_config, tool_config: ToolConfig) -> str:
    """Which precedence tier supplied this repo's policy."""
    if repo_config.name in tool_config.repo_overrides:
        return 'repo_overrides'
    if repo_config.sync_policy:
        return 'registry hint'
    return 'default_policy'


@config_app.command('edit')
def config_edit(
    which: Annotated[str, typer.Argument(help='Which file to open: config or registry.')] = 'config',
    repos_file: Annotated[
        Path | None,
        typer.Option('--repos-file', '-c', help='Edit a different registry'),
    ] = None,
) -> None:
    """Open the tool config or the repo registry in your editor ($VISUAL, then $EDITOR).

    Takes the same `config`/`registry` positional as `init` and `example`. It used to open only
    config.toml, while the file a new machine actually needs edited is the registry — naming your
    repos is the one step nothing else can do for you.

    Seeds the file from its starter first if none exists. The editor runs in the foreground, so a
    terminal editor blocks until you close it; a GUI editor returns immediately unless you
    configured it to wait (e.g. EDITOR="code --wait").
    """
    _requested_files(which)
    if which == 'registry':
        # Resolved through registry_location so this opens what syncer actually reads, not an
        # assumed default — the two differ on every machine that sets repos_registry.
        location = registry_location(load_tool_config(), repos_file)
        target = location.path
        if not target.exists():
            hint(f'no registry found — created an empty one at {init_registry(target)}{registry_source_note(location.source)}')
    else:
        target = TOOL_CONFIG_PATH
        if not target.exists():
            hint(f'no config found — created a starter config at {init_tool_config()}')

    editor = os.environ.get('VISUAL') or os.environ.get('EDITOR')
    if not editor:
        error('no editor configured — set $VISUAL or $EDITOR')
        raise typer.Exit(1)

    # $EDITOR may carry arguments ("code --wait", "emacsclient -nw"), so split it and resolve the
    # binary to a full path — this keeps the user's full intent and satisfies bandit's
    # partial-path check (B607) without a nosec.
    parts = shlex.split(editor)
    editor_bin = shutil.which(parts[0])
    if editor_bin is None:
        error(f'editor not found on PATH: {parts[0]}')
        raise typer.Exit(1)

    subprocess.run([editor_bin, *parts[1:], str(target)])


@config_app.command('validate')
def config_validate(
    repos_file: Annotated[
        Path | None,
        typer.Option('--repos-file', '-c', help='Use a different repo registry; replaces the default set entirely'),
    ] = None,
) -> None:
    """Check that both files parse, match their schemas, and agree with each other.

    Structure only. Whether the repo paths inside the registry exist on disk is `syncer issues`
    — validate checks the files, issues checks reality.
    """
    problems: list[str] = []
    tool_config = _validate_tool_config(problems)
    # Resolved from the config just parsed, not re-read: load_tool_config exits on a broken file,
    # which would abort the very command whose job is to explain what is broken about it.
    _validate_registry(registry_location(tool_config, repos_file), tool_config, problems)

    if problems:
        error(f'{len(problems)} problem(s) found:')
        for problem in problems:
            hint(f'  {problem}')
        raise typer.Exit(1)
    success('config is valid')


def _validate_tool_config(problems: list[str]) -> ToolConfig:
    """Parse config.toml and check every policy name it uses resolves. Returns what it could
    parse, so registry checks still run against a partially broken tool config."""
    if not TOOL_CONFIG_PATH.exists():
        hint(f'no tool config at {TOOL_CONFIG_PATH} — using built-in defaults')
        return ToolConfig()

    try:
        tool_config = parse_tool_config(read_tool_config())
    except ConfigError as exc:
        problems.extend(f'{TOOL_CONFIG_PATH}: {problem}' for problem in exc.problems)
        return ToolConfig()

    known = resolve_policies(tool_config)
    if tool_config.default_policy not in known:
        problems.append(f'default_policy: unknown policy {tool_config.default_policy!r}')
    for repo_name, policy_name in tool_config.repo_overrides.items():
        if policy_name not in known:
            problems.append(f'repo_overrides.{repo_name}: unknown policy {policy_name!r}')
    if tool_config.repos_registry and not Path(tool_config.repos_registry).expanduser().exists():
        # The likely cause is a config copied from a machine that has the shared registry onto one
        # that does not, so the message names the way out rather than only the missing path.
        problems.append(
            f'repos_registry: no such file: {Path(tool_config.repos_registry).expanduser()}'
            f" — comment it out to use this machine's own registry at {DEFAULT_REPOS_FILE}"
        )
    return tool_config


def _validate_registry(location: RegistryLocation, tool_config: ToolConfig, problems: list[str]) -> None:
    registry_path = location.path
    if not registry_path.exists():
        problems.append(
            f'{registry_path}{registry_source_note(location.source)}: no such file — create one with `syncer config init registry`'
        )
        return

    try:
        raw = json.loads(registry_path.read_text())
    except json.JSONDecodeError as exc:
        problems.append(f'{registry_path}: not valid JSON: {exc}')
        return

    try:
        registry = parse_registry(raw)
    except ConfigError as exc:
        problems.extend(f'{registry_path}: {problem}' for problem in exc.problems)
        return

    # status is required of a repo registry. It is not modeled as required because
    # this same parser reads the exemplar registry, where entries have no lifecycle —
    # so the rule is asserted here, where the file being read is the repo registry.
    undeclared = [repo.name for repo in registry.repos if repo.status is None]
    if undeclared:
        problems.append(f'repos: status is required and is missing on: {", ".join(undeclared)}')

    known = resolve_policies(tool_config)
    seen_names: dict[str, str] = {}
    seen_paths: dict[str, str] = {}
    for repo_config in registry.repos:
        if repo_config.name in seen_names:
            problems.append(f'repos: duplicate name {repo_config.name!r}')
        seen_names[repo_config.name] = repo_config.path

        resolved = str(Path(repo_config.path).expanduser())
        if resolved in seen_paths:
            problems.append(f'repos: duplicate path {repo_config.path!r} ({seen_paths[resolved]} and {repo_config.name})')
        seen_paths[resolved] = repo_config.name

        # A registry travels between machines but config.toml does not, so a hint naming a
        # machine-local custom policy silently degrades to `unknown policy` everywhere else.
        if repo_config.sync_policy and repo_config.sync_policy not in BUILTIN_POLICIES:
            detail = 'not a built-in' if repo_config.sync_policy in known else 'unknown policy'
            problems.append(
                f'repos.{repo_config.name}.sync_policy: {repo_config.sync_policy!r} is {detail}; '
                f'the registry is portable, so a hint must name one of: {", ".join(sorted(BUILTIN_POLICIES))}'
            )
