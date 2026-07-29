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
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import get_repos_file_path
from syncer.config import init_tool_config
from syncer.config import load_tool_config
from syncer.config import parse_registry
from syncer.config import parse_tool_config
from syncer.config import read_tool_config
from syncer.config import registry_path_for
from syncer.config import resolve_policies
from syncer.config import resolve_policy_name
from syncer.output import console
from syncer.output import emit_json
from syncer.output import error
from syncer.output import hint
from syncer.output import success
from syncer.policy import BUILTIN_POLICIES

config_app = typer.Typer(
    no_args_is_help=True,
    help='Inspect and edit the tool config and the repo registry it points at.',
)


@config_app.command('init')
def config_init() -> None:
    """Create an annotated tool config (fails if one already exists).

    Writes config.toml only, never the registry — syncer never writes repos.json, which is
    shared infrastructure that forge and indy also read. Scaffold a registry instead with
    `syncer config example --registry > <path>`.
    """
    if TOOL_CONFIG_PATH.exists():
        error(f'config already exists at {TOOL_CONFIG_PATH}')
        raise typer.Exit(1)
    path = init_tool_config()
    success(f'created config at {path}')
    hint(f'the registry defaults to {DEFAULT_REPOS_FILE} — scaffold one with:')
    hint(f'  syncer config example --registry > {DEFAULT_REPOS_FILE}')


@config_app.command('example')
def config_example(
    registry: Annotated[bool, typer.Option('--registry', help='Print the repo registry example instead of the tool config.')] = False,
) -> None:
    """Print a fully annotated example showing every available option.

    Meant for side-by-side reference: display it in one pane while editing the real file in
    another. Syntax-highlighted on a terminal, plain text when redirected, so
    `syncer config example --registry > repos.json` stays clean.
    """
    template, lexer = (TEMPLATE_REGISTRY, 'json') if registry else (TEMPLATE_TOOL_CONFIG, 'toml')
    if console.is_terminal:
        console.print(Syntax(template, lexer, background_color='default', word_wrap=True))
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
    registry_path = registry_path_for(tool_config, repos_file)
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
    console.print(f'[bold]registry[/bold]  {registry_path}{"" if registry_path.exists() else "  (absent)"}', soft_wrap=True)
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
def config_edit() -> None:
    """Open the tool config in your editor ($VISUAL, then $EDITOR).

    Seeds it from the template first if none exists. The editor runs in the foreground, so a
    terminal editor blocks until you close it; a GUI editor returns immediately unless you
    configured it to wait (e.g. EDITOR="code --wait").
    """
    if not TOOL_CONFIG_PATH.exists():
        path = init_tool_config()
        hint(f'no config found — created a starter config at {path}')

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

    subprocess.run([editor_bin, *parts[1:], str(TOOL_CONFIG_PATH)])


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
    _validate_registry(registry_path_for(tool_config, repos_file), tool_config, problems)

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
    if tool_config.repos_file and not Path(tool_config.repos_file).expanduser().exists():
        problems.append(f'repos_file: no such file: {Path(tool_config.repos_file).expanduser()}')
    return tool_config


def _validate_registry(registry_path: Path, tool_config: ToolConfig, problems: list[str]) -> None:
    if not registry_path.exists():
        problems.append(f'{registry_path}: no such file — scaffold one with `syncer config example --registry`')
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
