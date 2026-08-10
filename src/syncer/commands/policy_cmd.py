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
from syncer.execute import ACTION_DOCS
from syncer.execute import MUTATING_ACTIONS
from syncer.execute import describe_refusal
from syncer.output import console
from syncer.output import emit_json
from syncer.output import error
from syncer.output import hint
from syncer.policy import BUILTIN_POLICIES
from syncer.policy import PROTECTED_ALLOWED
from syncer.policy import STATE_DOCS
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import Policy
from syncer.policy import PrimaryState
from syncer.policy import decide
from syncer.policy import deciding_selector
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


actions_app = typer.Typer(
    no_args_is_help=True,
    help='The action menu a rule can map to, and what each one runs and refuses.',
)
policy_app.add_typer(actions_app, name='actions')


def _legal_actions(state: PrimaryState) -> list[str]:
    """The actions worth putting on a rule for `state`, from each action's own applies_to.

    Read off ACTION_DOCS rather than listed here, so an action added to the menu appears against
    the states it handles without this file being touched.
    """
    return [action.value for action in Action if state in ACTION_DOCS[action].applies_to]


def _rule_rows(policy: Policy, branch: str) -> list[dict]:
    """Every settable rule key for `branch`'s three possible roles, with the decision it produces.

    The action comes from decide() and the origin from deciding_selector(), so a row cannot claim
    something apply would not do. Rows are the role selectors rather than an invented branch name
    per glob: a policy's own exact-name and glob selectors surface through `--branch`, which is
    the same seam `policy show` uses.
    """
    rows = []
    for state in PrimaryState:
        for role, is_default, is_current in ROLES:
            selector = {'default': 'default', 'current': 'current', 'other': '*'}[role]
            branch_state = BranchState(branch=branch, primary=state, is_default=is_default, is_current=is_current)
            winner = deciding_selector(branch_state, policy)
            rows.append(
                {
                    'rule': f'{selector}:{state.value}',
                    'state': state.value,
                    'matches': {'default': 'the default branch', 'current': 'the checked-out branch', 'other': 'any other branch'}[role],
                    'action': decide(branch_state, policy).value,
                    'from': 'this rule' if winner == selector else (f'{winner}:{state.value}' if winner else 'fallback'),
                }
            )
    return rows


@policy_app.command('rules')
def policy_rules(
    name: Annotated[str | None, typer.Argument(help='Policy to resolve the rules for. Omitted, the machine default_policy.')] = None,
    branch: Annotated[
        str,
        typer.Option('--branch', '-b', help="Resolve for this branch name, so the policy's own exact-name and glob selectors apply."),
    ] = DEFAULT_MATRIX_BRANCH,
    as_json: Annotated[bool, typer.Option('--json', help='Output as JSON to stdout.')] = False,
) -> None:
    """List every rule you can set, grouped by state, with what this policy decides for each.

    This is the browse-and-pick view: the rule column is the complete set of keys, the actions
    line on each group is what that state will accept, and the action column is what you have
    now. Grep it — `syncer policy rules | rg gone` is how you find the one to change.
    """
    tool_config = load_tool_config()
    policies = resolve_policies(tool_config)
    resolved_name = name or tool_config.default_policy or 'standard'
    policy = policies.get(resolved_name)
    if policy is None:
        error(f'unknown policy {resolved_name!r}')
        hint(f'known policies: {", ".join(sorted(policies))}')
        raise typer.Exit(2)

    rows = _rule_rows(policy, branch)
    if as_json:
        emit_json(
            {
                'policy': policy.name,
                'branch': branch,
                'states': [{'state': state.value, 'means': STATE_DOCS[state], 'actions': _legal_actions(state)} for state in PrimaryState],
                'rules': rows,
            }
        )
        return

    source = 'default_policy' if name is None else 'named'
    console.print(f'[bold]{policy.name}[/bold]  ({source}, resolved for a branch named [cyan]{branch}[/cyan])', soft_wrap=True)
    for state in PrimaryState:
        console.print()
        console.print(f'[bold]{state.value}[/bold] — {STATE_DOCS[state]}', soft_wrap=True)
        console.print(f'  actions: {" · ".join(_legal_actions(state))}', soft_wrap=True)
        table = Table(box=None, pad_edge=False, show_header=False)
        table.add_column('rule')
        table.add_column('matches')
        table.add_column('action', style='cyan')
        table.add_column('from')
        for row in (r for r in rows if r['state'] == state.value):
            table.add_row(f'  {row["rule"]}', row['matches'], row['action'], row['from'])
        console.print(table)


def _action_record(action: Action, policies: dict[str, Policy]) -> dict:
    doc = ACTION_DOCS[action]
    return {
        'action': action.value,
        'summary': doc.summary,
        'mutates': action in MUTATING_ACTIONS,
        'protected_allows': action in PROTECTED_ALLOWED,
        'applies_to': [state.value for state in PrimaryState if state in doc.applies_to],
        'runs': doc.runs,
        'refuses': [{'reason': reason.value, 'text': describe_refusal(reason)} for reason in doc.refuses],
        'never': doc.never,
        # Computed rather than declared, so a policy that stops deciding an action stops listing it.
        'decided_by': sorted(
            policy_name
            for policy_name, policy in policies.items()
            if any(action.value in by_role.values() for by_role in decision_matrix(policy, DEFAULT_MATRIX_BRANCH).values())
        ),
    }


@actions_app.command('list')
def actions_list(
    as_json: Annotated[bool, typer.Option('--json', help='Output as JSON to stdout.')] = False,
) -> None:
    """List every action a rule can map to, and whether it changes anything.

    The menu is fixed — a policy chooses among these, and can never add to them.
    """
    policies = resolve_policies(load_tool_config())
    if as_json:
        emit_json([_action_record(action, policies) for action in Action])
        return

    table = Table(box=None, pad_edge=False)
    table.add_column('action', style='bold')
    table.add_column('mutates')
    table.add_column('on protected')
    table.add_column('applies to')
    table.add_column('what it does')
    for action in Action:
        doc = ACTION_DOCS[action]
        applies = [state.value for state in PrimaryState if state in doc.applies_to]
        table.add_row(
            action.value,
            'yes' if action in MUTATING_ACTIONS else 'no',
            'allowed' if action in PROTECTED_ALLOWED else 'refused',
            'any state' if len(applies) == len(PrimaryState) else ', '.join(applies),
            doc.summary,
        )
    console.print(table)
    console.print()
    console.print('syncer policy actions show <action>    what one of them runs, and what refuses it', soft_wrap=True)


@actions_app.command('show')
def actions_show(
    action_name: Annotated[str, typer.Argument(help='Action name, as it appears in `syncer policy actions list`.')],
    as_json: Annotated[bool, typer.Option('--json', help='Output as JSON to stdout.')] = False,
) -> None:
    """Show what one action runs, every precondition that refuses it, and what it will never do.

    The refusal list is the set of reasons the guard actually returns, held next to the guard in
    execute.py and checked against it by test, so this cannot promise a safety the code does not
    enforce.
    """
    try:
        action = Action(action_name)
    except ValueError:
        error(f'unknown action {action_name!r}')
        hint(f'known actions: {", ".join(a.value for a in Action)}')
        raise typer.Exit(2) from None

    policies = resolve_policies(load_tool_config())
    record = _action_record(action, policies)
    if as_json:
        emit_json(record)
        return

    doc = ACTION_DOCS[action]
    console.print(f'[bold]{action.value}[/bold] — {doc.summary}', soft_wrap=True)
    console.print()
    if doc.runs:
        console.print(f'  [bold]Runs[/bold]       {doc.runs}', soft_wrap=True)
    console.print(f'  [bold]Applies to[/bold] {", ".join(record["applies_to"])}', soft_wrap=True)
    # One refusal per line: this is the list you read before trusting the action, and running six
    # of them together into a paragraph is how it stops being read.
    for index, reason in enumerate(doc.refuses):
        label = '  [bold]Refuses[/bold]   ' if index == 0 else ' ' * 12
        console.print(f'{label} {describe_refusal(reason)}', soft_wrap=True)
    if doc.never:
        console.print(f'  [bold]Never[/bold]      {doc.never}', soft_wrap=True)
    console.print(f'  [bold]Protected[/bold]  {"allowed" if action in PROTECTED_ALLOWED else "refused"}', soft_wrap=True)
    console.print(f'  [bold]Decided by[/bold] {", ".join(record["decided_by"]) or "no policy on this machine"}', soft_wrap=True)


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
    tool_config = load_tool_config()
    policies = resolve_policies(tool_config)
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
        # A patched policy is the base plus this file's cells, and after the merge the result is
        # indistinguishable from a policy that declared all of them — which is the one thing
        # someone editing config.toml needs to see. Compared against the recorded base rather
        # than tracked through the merge, so it stays a property of what is on screen.
        base = BUILTIN_POLICIES.get(tool_config.policy_bases.get(name, ''))
        rules = Table(box=None, pad_edge=False, show_header=False)
        rules.add_column('rule')
        rules.add_column('action')
        rules.add_column('origin')
        for key in sorted(policy.rules):
            if base is None:
                origin = ''
            elif key not in base.rules:
                origin = f'added by config.toml (base {base.name})'
            elif base.rules[key] != policy.rules[key]:
                origin = f'overrides {base.name} ({base.rules[key]})'
            else:
                origin = f'from {base.name}'
            rules.add_row(f'  {key}', policy.rules[key], origin)
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
