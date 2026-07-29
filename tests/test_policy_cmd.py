import json

import pytest
from typer.testing import CliRunner

from syncer.commands.policy_cmd import ROLES
from syncer.commands.policy_cmd import decision_matrix
from syncer.main import app
from syncer.policy import BUILTIN_POLICIES
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
