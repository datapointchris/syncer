import json

import pytest
from typer.testing import CliRunner

from syncer.config import TEMPLATE_REGISTRY
from syncer.config import TEMPLATE_TOOL_CONFIG
from syncer.main import app
from syncer.policy import Action
from syncer.policy import PrimaryState

runner = CliRunner()


@pytest.fixture
def config_home(tmp_path, monkeypatch):
    """A syncer config dir nothing has written to yet, standing in for a fresh machine."""
    config_dir = tmp_path / 'config' / 'syncer'
    monkeypatch.setattr('syncer.main.notify', lambda *_: None)
    for module in ('syncer.config', 'syncer.commands.config_cmd'):
        monkeypatch.setattr(f'{module}.TOOL_CONFIG_PATH', config_dir / 'config.toml')
        monkeypatch.setattr(f'{module}.DEFAULT_REPOS_FILE', config_dir / 'repos.json')
        monkeypatch.setattr(f'{module}.STATE_DIR', tmp_path / 'state' / 'syncer')
    return config_dir


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


class TestTemplateRoundTrip:
    """The templates are the single source for `init` and `example`, so they have to stay
    parseable by the models they claim to describe. This is the test that keeps the annotated
    example honest when a config field changes."""

    def test_tool_config_template_parses_into_its_model(self, config_home):
        _write(config_home / 'config.toml', TEMPLATE_TOOL_CONFIG)
        _write(config_home / 'repos.json', TEMPLATE_REGISTRY)
        result = runner.invoke(app, ['config', 'validate'])
        assert result.exit_code == 0, result.output

    def test_registry_template_parses_into_its_model(self, config_home):
        _write(config_home / 'repos.json', TEMPLATE_REGISTRY)
        result = runner.invoke(app, ['config', 'validate'])
        assert result.exit_code == 0, result.output

    def test_template_documents_every_state_and_action(self):
        """A new PrimaryState or Action that never reaches the template is a rule the user has
        no way to discover. Iterate the enums rather than restating their members."""
        missing = [member.value for member in (*PrimaryState, *Action) if member.value not in TEMPLATE_TOOL_CONFIG]
        assert missing == []


class TestConfigInit:
    def test_writes_a_config_that_validates(self, config_home):
        assert runner.invoke(app, ['config', 'init']).exit_code == 0
        assert (config_home / 'config.toml').read_text() == TEMPLATE_TOOL_CONFIG

        _write(config_home / 'repos.json', TEMPLATE_REGISTRY)
        assert runner.invoke(app, ['config', 'validate']).exit_code == 0

    def test_refuses_when_the_file_exists(self, config_home):
        _write(config_home / 'config.toml', 'default_policy = "observe"\n')
        result = runner.invoke(app, ['config', 'init'])
        assert result.exit_code == 1
        assert (config_home / 'config.toml').read_text() == 'default_policy = "observe"\n'

    def test_never_writes_the_registry(self, config_home):
        """syncer never writes repos.json — it is shared with forge and indy. Scaffolding is
        `config example --registry`, so the invariant stays absolute."""
        runner.invoke(app, ['config', 'init'])
        assert not (config_home / 'repos.json').exists()


class TestConfigExample:
    def test_prints_the_tool_config_template(self, config_home):
        result = runner.invoke(app, ['config', 'example'])
        assert 'default_policy' in result.output

    def test_registry_flag_prints_the_registry_template(self, config_home):
        result = runner.invoke(app, ['config', 'example', '--registry'])
        assert json.loads(result.output)['repos']


class TestConfigPath:
    def test_naming_one_prints_it_bare_for_shell_substitution(self, config_home):
        result = runner.invoke(app, ['config', 'path', 'registry'])
        assert result.exit_code == 0
        assert result.output.strip() == str(config_home / 'repos.json')

    def test_unknown_name_is_a_usage_error(self, config_home):
        assert runner.invoke(app, ['config', 'path', 'nope']).exit_code == 2

    def test_all_three_paths_by_default(self, config_home):
        result = runner.invoke(app, ['config', 'path', '--json'])
        assert set(json.loads(result.output)) == {'config', 'registry', 'state'}


class TestConfigShow:
    def test_reports_the_resolved_policy_and_its_source_per_repo(self, config_home):
        _write(
            config_home / 'config.toml',
            'default_policy = "standard"\n\n[repo_overrides]\n"overridden" = "observe"\n',
        )
        _write(
            config_home / 'repos.json',
            json.dumps(
                {
                    'owner': 'me',
                    'repos': [
                        {'name': 'plain', 'path': '~/code/plain'},
                        {'name': 'hinted', 'path': '~/code/hinted', 'sync_policy': 'mirror'},
                        {'name': 'overridden', 'path': '~/code/overridden', 'sync_policy': 'mirror'},
                    ],
                }
            ),
        )
        repos = {repo['name']: repo for repo in json.loads(runner.invoke(app, ['config', 'show', '--json']).output)['repos']}
        assert repos['plain']['policy'] == 'standard'
        assert repos['hinted']['policy'] == 'mirror'
        # repo_overrides beats the registry's portable hint
        assert repos['overridden']['policy'] == 'observe'
        assert repos['overridden']['source'] == 'repo_overrides'

    def test_flags_a_policy_that_does_not_resolve(self, config_home):
        _write(config_home / 'config.toml', 'default_policy = "standard"\n')
        _write(
            config_home / 'repos.json',
            json.dumps({'owner': 'me', 'repos': [{'name': 'r', 'path': '~/code/r', 'sync_policy': 'nonexistent'}]}),
        )
        repos = json.loads(runner.invoke(app, ['config', 'show', '--json']).output)['repos']
        assert repos[0]['resolves'] is False


class TestConfigValidate:
    """Structure only — whether the repo paths exist on disk is `syncer issues`. One test per
    failure class, each asserting the message names the offending key."""

    def _run(self, config_home, toml='', registry=None):
        _write(config_home / 'config.toml', toml)
        _write(config_home / 'repos.json', registry if registry is not None else json.dumps({'owner': 'me', 'repos': []}))
        return runner.invoke(app, ['config', 'validate'])

    def test_valid_config_exits_zero(self, config_home):
        assert self._run(config_home, 'default_policy = "observe"\n').exit_code == 0

    def test_malformed_toml(self, config_home):
        result = self._run(config_home, 'default_policy = \n')
        assert result.exit_code == 1
        assert 'not valid TOML' in result.output

    def test_unknown_action_in_a_rule(self, config_home):
        result = self._run(config_home, '[policies.bad.rules]\n"*:ahead" = "yolo"\n')
        assert result.exit_code == 1
        assert 'yolo' in result.output

    def test_unknown_state_in_a_rule_key(self, config_home):
        result = self._run(config_home, '[policies.bad.rules]\n"*:sideways" = "report"\n')
        assert result.exit_code == 1
        assert 'sideways' in result.output

    def test_default_policy_must_resolve(self, config_home):
        result = self._run(config_home, 'default_policy = "nonexistent"\n')
        assert result.exit_code == 1
        assert 'default_policy' in result.output
        assert 'nonexistent' in result.output

    def test_repo_override_must_resolve(self, config_home):
        result = self._run(config_home, '[repo_overrides]\n"some-repo" = "nonexistent"\n')
        assert result.exit_code == 1
        assert 'repo_overrides.some-repo' in result.output

    def test_repos_file_must_exist(self, config_home, tmp_path):
        result = self._run(config_home, f'repos_file = "{tmp_path / "absent.json"}"\n')
        assert result.exit_code == 1
        assert 'repos_file' in result.output

    def test_malformed_registry_json(self, config_home):
        result = self._run(config_home, registry='{not json')
        assert result.exit_code == 1
        assert 'not valid JSON' in result.output

    def test_bad_url_template_is_surfaced_not_crashed(self, config_home):
        registry = json.dumps({'owner': 'me', 'url_template': '{host}/{oops}/{name}', 'repos': []})
        result = self._run(config_home, registry=registry)
        assert result.exit_code == 1
        assert 'url_template' in result.output

    def test_duplicate_repo_names(self, config_home):
        registry = json.dumps({'owner': 'me', 'repos': [{'name': 'dup', 'path': '~/a'}, {'name': 'dup', 'path': '~/b'}]})
        result = self._run(config_home, registry=registry)
        assert result.exit_code == 1
        assert 'duplicate name' in result.output

    def test_duplicate_repo_paths(self, config_home):
        registry = json.dumps({'owner': 'me', 'repos': [{'name': 'a', 'path': '~/same'}, {'name': 'b', 'path': '~/same'}]})
        result = self._run(config_home, registry=registry)
        assert result.exit_code == 1
        assert 'duplicate path' in result.output

    def test_sync_policy_hint_must_name_a_builtin(self, config_home):
        """config.toml is not synced between machines, so a hint naming a machine-local custom
        policy silently degrades to `unknown policy` on every other box."""
        toml = '[policies.laptop]\nscope = "all"\n'
        registry = json.dumps({'owner': 'me', 'repos': [{'name': 'r', 'path': '~/r', 'sync_policy': 'laptop'}]})
        result = self._run(config_home, toml, registry)
        assert result.exit_code == 1
        assert 'sync_policy' in result.output
        assert 'not a built-in' in result.output

    def test_missing_registry_points_at_the_scaffolding_command(self, config_home):
        _write(config_home / 'config.toml', '')
        result = runner.invoke(app, ['config', 'validate'])
        assert result.exit_code == 1
        assert 'config example --registry' in result.output

    def test_repo_paths_are_not_checked_against_the_disk(self, config_home):
        """validate checks structure, issues checks reality. Blurring them means neither gets
        trusted."""
        registry = json.dumps({'owner': 'me', 'repos': [{'name': 'r', 'path': '/nowhere/at/all'}]})
        assert self._run(config_home, registry=registry).exit_code == 0
