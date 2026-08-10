import json

import pytest
from typer.testing import CliRunner

from syncer.commands.policy_cmd import ROLES
from syncer.commands.policy_cmd import decision_matrix
from syncer.execute import Refusal
from syncer.main import app
from syncer.policy import BUILTIN_POLICIES
from syncer.policy import Action
from syncer.policy import BranchState
from syncer.policy import PrimaryState
from syncer.policy import decide

runner = CliRunner()


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    config_dir = tmp_path / 'config' / 'syncer'
    config_dir.mkdir(parents=True)
    monkeypatch.setattr('syncer.main.notify', lambda *_: None)
    monkeypatch.setattr('syncer.config.TOOL_CONFIG_PATH', config_dir / 'config.toml')
    return config_dir


class TestDecisionMatrix:
    """The matrix is the decision function, not a description of it. If these ever disagree,
    the rendered table is lying about what --apply will do."""

    @pytest.mark.parametrize('policy_name', sorted(BUILTIN_POLICIES))
    def test_agrees_cell_for_cell_with_decide(self, policy_name):
        policy = BUILTIN_POLICIES[policy_name]
        matrix = decision_matrix(policy, 'main')
        for state in PrimaryState:
            for role, is_default, is_current in ROLES:
                synthetic = BranchState(branch='main', primary=state, is_default=is_default, is_current=is_current)
                assert matrix[state.value][role] == decide(synthetic, policy).value

    def test_covers_every_primary_state(self):
        """Iterate the enum — a state added without a matrix row is one the user cannot see."""
        matrix = decision_matrix(BUILTIN_POLICIES['standard'], 'main')
        assert set(matrix) == {state.value for state in PrimaryState}

    def test_branch_name_resolves_exact_and_glob_selectors(self, config_home):
        """The reason --branch exists: confirming a name-specific rule beats `*` before
        trusting --apply across a set of repos."""
        (config_home / 'config.toml').write_text(
            '[policies.work]\nscope = "all"\n[policies.work.rules]\n"release/*:ahead" = "report"\n"*:ahead" = "push"\n'
        )
        generic = json.loads(runner.invoke(app, ['policy', 'show', 'work', '--branch', 'feature/x', '--json']).output)
        specific = json.loads(runner.invoke(app, ['policy', 'show', 'work', '--branch', 'release/2.0', '--json']).output)
        assert generic['decisions']['ahead']['other'] == 'push'
        assert specific['decisions']['ahead']['other'] == 'report'


class TestPolicyList:
    def test_lists_every_builtin(self, config_home):
        listed = json.loads(runner.invoke(app, ['policy', 'list', '--json']).output)
        assert {policy['name'] for policy in listed} >= set(BUILTIN_POLICIES)
        assert all(policy['builtin'] for policy in listed)

    def test_includes_custom_policies(self, config_home):
        (config_home / 'config.toml').write_text('[policies.laptop]\nscope = "all"\nmerge_target = "develop"\n')
        listed = {policy['name']: policy for policy in json.loads(runner.invoke(app, ['policy', 'list', '--json']).output)}
        assert listed['laptop']['builtin'] is False
        assert listed['laptop']['merge_target'] == 'develop'

    def test_a_custom_policy_replaces_the_builtin_it_names(self, config_home):
        (config_home / 'config.toml').write_text('[policies.standard]\nscope = "all"\nfallback = "skip"\n')
        listed = {policy['name']: policy for policy in json.loads(runner.invoke(app, ['policy', 'list', '--json']).output)}
        assert listed['standard']['fallback'] == 'skip'


class TestPolicyShow:
    def test_renders_the_rules_and_the_matrix(self, config_home):
        result = runner.invoke(app, ['policy', 'show', 'standard'])
        assert result.exit_code == 0
        assert 'default:synced' in result.output
        assert 'fast_forward' in result.output

    def test_unknown_policy_is_a_usage_error_naming_the_alternatives(self, config_home):
        result = runner.invoke(app, ['policy', 'show', 'nonexistent'])
        assert result.exit_code == 2
        assert 'mirror' in result.output

    def test_json_carries_the_rules_and_the_branch_it_evaluated(self, config_home):
        payload = json.loads(runner.invoke(app, ['policy', 'show', 'standard', '--json']).output)
        assert payload['branch'] == 'main'
        assert payload['rules']['default:ahead'] == 'push'
        assert payload['decisions']['ahead']['default'] == 'push'

    def test_observe_decides_nothing_but_report(self, config_home):
        """observe has no rules at all, so every cell is the fallback — worth asserting, since
        an empty rule table is the one shape a hand-written matrix would get wrong."""
        payload = json.loads(runner.invoke(app, ['policy', 'show', 'observe', '--json']).output)
        actions = {action for row in payload['decisions'].values() for action in row.values()}
        assert actions == {'report'}


class TestProtectedBranchesInShow:
    """protected is machine-local, set per policy in config.toml. `policy show` is where you
    confirm it covers what you think before trusting --apply."""

    def _work_policy(self, config_home):
        (config_home / 'config.toml').write_text(
            '[policies.work]\nscope = "all"\nprotected = ["develop", "release/*"]\n'
            '[policies.work.rules]\n"*:ahead" = "push"\n"*:gone" = "delete_local"\n'
        )

    def test_marks_the_actions_protection_would_refuse(self, config_home):
        self._work_policy(config_home)
        payload = json.loads(runner.invoke(app, ['policy', 'show', 'work', '--branch', 'develop', '--json']).output)
        assert payload['protected_by'] == 'develop'
        # decisions stays the pure decide() answer; blocked_actions is what execute() then refuses
        assert payload['decisions']['ahead']['other'] == 'push'
        assert 'push' in payload['blocked_actions']
        assert 'delete_local' in payload['blocked_actions']

    def test_fast_forward_is_never_blocked(self, config_home):
        """Advancing to what the upstream already contains publishes nothing and destroys
        nothing — blocking it would make the setting useless for long-lived branches."""
        self._work_policy(config_home)
        payload = json.loads(runner.invoke(app, ['policy', 'show', 'work', '--branch', 'develop', '--json']).output)
        assert 'fast_forward' not in payload['blocked_actions']

    def test_a_branch_outside_the_patterns_is_unblocked(self, config_home):
        self._work_policy(config_home)
        payload = json.loads(runner.invoke(app, ['policy', 'show', 'work', '--branch', 'feature/x', '--json']).output)
        assert payload['protected_by'] is None
        assert payload['blocked_actions'] == []

    def test_glob_patterns_resolve(self, config_home):
        self._work_policy(config_home)
        payload = json.loads(runner.invoke(app, ['policy', 'show', 'work', '--branch', 'release/2.0', '--json']).output)
        assert payload['protected_by'] == 'release/*'

    def test_list_reports_each_policys_protected_patterns(self, config_home):
        self._work_policy(config_home)
        listed = {policy['name']: policy for policy in json.loads(runner.invoke(app, ['policy', 'list', '--json']).output)}
        assert listed['work']['protected'] == ['develop', 'release/*']
        assert listed['standard']['protected'] == []


class TestPolicyRules:
    def test_lists_every_settable_key(self, config_home):
        result = runner.invoke(app, ['policy', 'rules', 'standard', '--json'])
        assert result.exit_code == 0
        payload = json.loads(result.stdout)
        assert {row['rule'] for row in payload['rules']} == {
            f'{selector}:{state.value}' for state in PrimaryState for selector in ('default', 'current', '*')
        }

    def test_the_action_column_agrees_with_decide(self, config_home):
        """The browse view is what someone edits their config from, so a row claiming an action
        apply would not take is worse than no row."""
        result = runner.invoke(app, ['policy', 'rules', 'standard', '--json'])
        policy = BUILTIN_POLICIES['standard']
        roles = {'default': (True, False), 'current': (False, True), '*': (False, False)}
        for row in json.loads(result.stdout)['rules']:
            selector, _, state = row['rule'].rpartition(':')
            is_default, is_current = roles[selector]
            expected = decide(BranchState(branch='main', primary=PrimaryState(state), is_default=is_default, is_current=is_current), policy)
            assert row['action'] == expected.value, row

    def test_offered_actions_come_from_the_guards_own_applies_to(self, config_home):
        payload = json.loads(runner.invoke(app, ['policy', 'rules', 'standard', '--json']).stdout)
        offered = {entry['state']: entry['actions'] for entry in payload['states']}
        assert 'delete_local' in offered['gone']
        assert 'delete_local' not in offered['synced']
        assert 'push' in offered['ahead']
        assert 'push' not in offered['behind']

    def test_unknown_policy_is_a_usage_error(self, config_home):
        assert runner.invoke(app, ['policy', 'rules', 'nope']).exit_code == 2


class TestPolicyActions:
    def test_list_covers_the_whole_menu(self, config_home):
        payload = json.loads(runner.invoke(app, ['policy', 'actions', 'list', '--json']).stdout)
        assert {entry['action'] for entry in payload} == {action.value for action in Action}

    def test_show_reports_what_the_guard_refuses_on(self, config_home):
        payload = json.loads(runner.invoke(app, ['policy', 'actions', 'show', 'delete_local', '--json']).stdout)
        assert payload['applies_to'] == ['gone']
        assert payload['mutates'] is True
        assert payload['protected_allows'] is False
        # Compared by key: the sentence lives once in REFUSAL_TEXT and is free to be rewritten.
        assert Refusal.NOT_INTEGRATED.value in {entry['reason'] for entry in payload['refuses']}

    def test_unknown_action_is_a_usage_error(self, config_home):
        assert runner.invoke(app, ['policy', 'actions', 'show', 'nope']).exit_code == 2

    def test_bare_group_shows_help_rather_than_acting(self, config_home):
        # Exit 2, as every other group in this tool does for no-args: click classifies it as a
        # usage error. Asserted so the behaviour is consistent, not accidental.
        result = runner.invoke(app, ['policy', 'actions'])
        assert result.exit_code == 2
        assert 'show' in result.output and 'list' in result.output
