"""Read-only per-branch sync report. This is the first-slice surface: it classifies
every in-scope branch and shows the action each policy *would* take, but performs no
mutation — the execute() half is a later slice.
"""

from __future__ import annotations

from pathlib import Path

from syncer.classify import classify_repo
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import resolve_policies
from syncer.config import resolve_policy_name
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import PrimaryState
from syncer.policy import decide
from syncer.repos import ICON_ERR
from syncer.repos import ICON_MOVE
from syncer.repos import ICON_OK
from syncer.repos import ICON_PULL
from syncer.repos import ICON_PUSH
from syncer.repos import ICON_WARN
from syncer.repos import Repo
from syncer.repos import console

_STATE_STYLE = {
    PrimaryState.SYNCED: (ICON_OK, 'green'),
    PrimaryState.AHEAD: (ICON_PUSH, 'yellow'),
    PrimaryState.BEHIND: (ICON_PULL, 'yellow'),
    PrimaryState.DIVERGED: (ICON_MOVE, 'yellow'),
    PrimaryState.NO_UPSTREAM: (ICON_WARN, 'yellow'),
    PrimaryState.GONE: (ICON_ERR, 'red'),
    PrimaryState.DETACHED: (ICON_WARN, 'red'),
}


_STATE_LABEL = {
    PrimaryState.SYNCED: 'synced',
    PrimaryState.NO_UPSTREAM: 'no upstream',
    PrimaryState.GONE: 'gone (remote deleted)',
    PrimaryState.DETACHED: 'detached HEAD',
}


def _branch_line(state: BranchState, action: Action) -> str:
    icon, color = _STATE_STYLE.get(state.primary, (ICON_WARN, 'blue'))

    # For ahead/behind/diverged the commit counts carry the meaning; elsewhere use a label.
    counts = []
    if state.ahead:
        counts.append(f'{state.ahead} ahead')
    if state.behind:
        counts.append(f'{state.behind} behind')
    detail_parts = counts or [_STATE_LABEL.get(state.primary, state.primary.value)]
    if state.dirty:
        detail_parts.append('dirty')
    if state.stashed:
        detail_parts.append('stashed')
    detail = ', '.join(detail_parts)

    # Parens, not brackets: Rich console markup treats [..] as style tags and would
    # swallow the text.
    flags = []
    if state.is_default:
        flags.append('default')
    if state.is_current:
        flags.append('current')
    flag_str = f' ({", ".join(flags)})' if flags else ''

    return f'  [{color}]{icon}  {state.branch}{flag_str} — {detail}[/{color}] [blue]→ {action.value}[/blue]'


def report_branches(config: SyncerConfig, tool_config: ToolConfig, cli_policy: str | None = None) -> None:
    policies = resolve_policies(tool_config)
    active_repos = [repo for repo in config.repos if repo.status != 'retired']

    console.print()
    for repo_config in active_repos:
        path = Path(repo_config.path).expanduser()
        label = repo_config.path if repo_config.path.startswith('~') else repo_config.name
        owner = repo_config.owner or config.owner
        repo = Repo(name=repo_config.name, path=path, owner=owner, host=config.host)

        if not repo.exists or not repo.is_git_repo or not repo.has_remote:
            continue

        policy_name = resolve_policy_name(repo_config, tool_config, cli_policy)
        policy = policies.get(policy_name)
        if policy is None:
            console.print(f'[red]{label}: unknown policy {policy_name!r}[/red]')
            console.print()
            continue

        states = classify_repo(repo, policy)
        console.print(f'[bold]{label}[/bold] [blue](policy: {policy_name})[/blue]')
        for state in states:
            console.print(_branch_line(state, decide(state, policy)))
        console.print()
