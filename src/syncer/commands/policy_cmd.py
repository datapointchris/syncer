"""`syncer policy` — what a policy actually decides.

The decision matrix here is computed by calling decide() over the full state taxonomy rather
than described in prose, so it cannot drift from what --apply will do. That is only possible
because decide() is pure: a BranchState and a Policy in, an Action out, no git and no disk.
"""

from __future__ import annotations

from typing import Annotated

import typer
from rich.table import Table

from syncer.config import load_tool_config
from syncer.config import resolve_policies
from syncer.output import console
from syncer.output import emit_json
from syncer.output import error
from syncer.output import hint
from syncer.policy import BUILTIN_POLICIES
from syncer.policy import PROTECTED_ALLOWED
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import Policy
from syncer.policy import PrimaryState
from syncer.policy import decide
from syncer.policy import matching_protection

policy_app = typer.Typer(
    no_args_is_help=True,
    help='Inspect the sync policies this machine can resolve, and what each one decides.',
)

# The branch name the matrix evaluates when none is given. Exact-name and glob selectors are
# resolved against it, so `--branch` is how you check that `release/*:ahead` beats `*:ahead`
# before trusting --apply across thirty repos.
DEFAULT_MATRIX_BRANCH = 'main'

# The three roles a branch can hold — (label, is_default, is_current) — which is the other half of
# what decide() reads. Modifiers (dirty, stashed) are deliberately absent: they are execute-time
# gates, never decision inputs, and decide() is invariant to them.
ROLES: tuple[tuple[str, bool, bool], ...] = (
    ('default', True, False),
    ('current', False, True),
    ('other', False, False),
)


def decision_matrix(policy: Policy, branch: str) -> dict[str, dict[str, str]]:
    """Every primary state × every branch role, as decide() answers it.

    Iterates PrimaryState rather than listing its members, so a new state appears here the day
    it is added instead of the day someone remembers to document it.
    """
    return {
        state.value: {
            role: decide(BranchState(branch=branch, primary=state, is_default=is_default, is_current=is_current), policy).value
            for role, is_default, is_current in ROLES
        }
        for state in PrimaryState
    }


def _summary(policy: Policy) -> dict:
    return {
        'name': policy.name,
        'builtin': policy.name in BUILTIN_POLICIES,
        'scope': policy.scope.value,
        'prune': policy.prune,
        'fallback': policy.fallback.value,
        'merge_target': policy.merge_target,
        'protected': policy.protected,
        'rules': len(policy.rules),
    }


@policy_app.command('list')
def policy_list(
    as_json: Annotated[bool, typer.Option('--json', help='Output as JSON to stdout.')] = False,
) -> None:
    """List every policy this machine can resolve, built-in and custom.

    A custom policy defined in config.toml under the name of a built-in replaces it.
    """
    tool_config = load_tool_config()
    policies = resolve_policies(tool_config)

    if as_json:
        emit_json([_summary(policies[name]) for name in sorted(policies)])
        return

    table = Table(box=None, pad_edge=False)
    table.add_column('policy', style='bold')
    table.add_column('source')
    table.add_column('scope')
    table.add_column('fallback')
    table.add_column('merge target')
    table.add_column('protected')
    table.add_column('rules', justify='right')
    for name in sorted(policies):
        policy = policies[name]
        if name not in tool_config.policies:
            source = 'built-in'
        else:
            source = 'config.toml (overrides built-in)' if name in BUILTIN_POLICIES else 'config.toml'
        table.add_row(
            name,
            source,
            policy.scope.value,
            policy.fallback.value,
            policy.merge_target or 'default branch',
            ', '.join(policy.protected) or 'none',
            str(len(policy.rules)),
        )
    console.print(table)


@policy_app.command('show')
def policy_show(
    name: Annotated[str, typer.Argument(help='Policy name, as it appears in `syncer policy list`.')],
    branch: Annotated[
        str,
        typer.Option('--branch', '-b', help='Evaluate the matrix for this branch name, so exact-name and glob selectors resolve.'),
    ] = DEFAULT_MATRIX_BRANCH,
    as_json: Annotated[bool, typer.Option('--json', help='Output as JSON to stdout.')] = False,
) -> None:
    """Show a policy's rules and the decision they produce for every state.

    The matrix is computed by calling the rules engine, not written down, so it is the policy
    rather than a description of it. Pass --branch to check a name-specific or glob selector:
    `--branch develop` is how you confirm `develop:ahead` beats `*:ahead` before --apply runs.
    """
    policies = resolve_policies(load_tool_config())
    policy = policies.get(name)
    if policy is None:
        error(f'unknown policy {name!r}')
        hint(f'known policies: {", ".join(sorted(policies))}')
        raise typer.Exit(2)

    matrix = decision_matrix(policy, branch)
    # Protection matches on the branch name alone, so it applies to all three role columns
    # equally — one lookup answers the whole matrix.
    protected_by = matching_protection(branch, policy)
    blocked_actions = sorted(action.value for action in Action if action not in PROTECTED_ALLOWED) if protected_by else []

    if as_json:
        emit_json(
            _summary(policy)
            | {
                'rules': policy.rules,
                'branch': branch,
                'decisions': matrix,
                'protected_by': protected_by,
                # decisions stays the pure decide() answer; this is what execute() would then
                # refuse, so a caller asking "will syncer push to develop" gets both halves.
                'blocked_actions': blocked_actions,
            }
        )
        return

    merge_target = policy.merge_target or "the repo's default branch"
    console.print(
        f'[bold]{policy.name}[/bold]  (scope: {policy.scope.value}, fallback: {policy.fallback.value}, merge target: {merge_target})',
        soft_wrap=True,
    )
    if policy.protected:
        console.print(f'protected: {", ".join(policy.protected)}', soft_wrap=True)

    console.print()
    if policy.rules:
        rules = Table(box=None, pad_edge=False, show_header=False)
        rules.add_column('rule')
        rules.add_column('action')
        for key in sorted(policy.rules):
            rules.add_row(f'  {key}', policy.rules[key])
        console.print('[bold]rules[/bold]')
        console.print(rules)
    else:
        console.print(f'[bold]rules[/bold]  none — every state falls back to {policy.fallback.value}')

    console.print()
    console.print(f'[bold]decisions[/bold] for a branch named [cyan]{branch}[/cyan]')
    if protected_by:
        console.print(
            f'[yellow]{branch} is protected by {protected_by!r} — marked actions are refused at execute time[/yellow]', soft_wrap=True
        )
    decisions = Table(box=None, pad_edge=False)
    decisions.add_column('')
    decisions.add_column(f'default({branch})')
    decisions.add_column('current')
    decisions.add_column('other')
    for state, by_role in matrix.items():
        cells = [
            f'[yellow]{action} (refused)[/yellow]' if action in blocked_actions else action
            for action in (by_role[role] for role, _, _ in ROLES)
        ]
        decisions.add_row(f'  {state}', *cells)
    console.print(decisions)
