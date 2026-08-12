import json
import subprocess

import pytest
from typer.testing import CliRunner

from syncer.config import STARTER_REGISTRY
from syncer.config import STARTER_TOOL_CONFIG
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


def _git_repo(path, origin):
    path.mkdir(parents=True, exist_ok=True)
    subprocess.run(['git', 'init', '-q', str(path)], capture_output=True)
    subprocess.run(['git', 'remote', 'add', 'origin', origin], cwd=path, capture_output=True)
    return path


class TestTemplateRoundTrip:
    """Four templates now — the STARTER_* pair `init` writes and the annotated TEMPLATE_* pair
    `example` prints — and every one has to stay parseable by the model it claims to describe.

    Splitting them is what lets `init` scaffold a file with nothing to delete while `example`
    stays exhaustive, and this test is the whole reason the split is safe: it is what actually
    caught drift, not the fact that the two shared a string.
    """

    def test_tool_config_template_parses_into_its_model(self, config_home):
        _write(config_home / 'config.toml', TEMPLATE_TOOL_CONFIG)
        _write(config_home / 'repos.json', TEMPLATE_REGISTRY)
        result = runner.invoke(app, ['config', 'validate'])
        assert result.exit_code == 0, result.output

    def test_registry_template_parses_into_its_model(self, config_home):
        _write(config_home / 'repos.json', TEMPLATE_REGISTRY)
        result = runner.invoke(app, ['config', 'validate'])
        assert result.exit_code == 0, result.output

    def test_starter_config_parses_into_its_model(self, config_home):
        _write(config_home / 'config.toml', STARTER_TOOL_CONFIG)
        _write(config_home / 'repos.json', STARTER_REGISTRY)
        result = runner.invoke(app, ['config', 'validate'])
        assert result.exit_code == 0, result.output

    def test_starter_registry_parses_into_its_model(self, config_home):
        _write(config_home / 'repos.json', STARTER_REGISTRY)
        result = runner.invoke(app, ['config', 'validate'])
        assert result.exit_code == 0, result.output

    def test_the_starters_carry_nothing_to_delete(self):
        """The point of the split. `init` used to ship a fake `laptop` policy that `policy list`
        renders like a built-in, an override for a repo nobody has, and three fake repos that
        made the first-ever run print three bogus `would clone` lines."""
        assert 'policies' not in STARTER_TOOL_CONFIG
        assert 'repo_overrides' not in STARTER_TOOL_CONFIG
        # Not even commented out: repos_file is the one setting whose wrong value fails every run
        # outright, and a shared config.toml naming a path one machine lacks is a real incident.
        assert 'repos_file' not in STARTER_TOOL_CONFIG
        assert json.loads(STARTER_REGISTRY)['repos'] == []

    def test_template_documents_every_state_and_action(self):
        """A new PrimaryState or Action that never reaches the annotated template is a rule the
        user has no way to discover. Deliberately still TEMPLATE_, not STARTER_: discoverability
        belongs in the reference, which is what resolves its tension with a minimal scaffold."""
        missing = [member.value for member in (*PrimaryState, *Action) if member.value not in TEMPLATE_TOOL_CONFIG]
        assert missing == []


class TestConfigInit:
    def test_creates_both_files_and_they_validate(self, config_home):
        """One command has to leave a fresh machine in a runnable state: a template printed to
        the terminal is the half-answer that sent the user looking for where to put it."""
        assert runner.invoke(app, ['config', 'init']).exit_code == 0
        assert (config_home / 'config.toml').read_text() == STARTER_TOOL_CONFIG
        assert (config_home / 'repos.json').read_text() == STARTER_REGISTRY
        assert runner.invoke(app, ['config', 'validate']).exit_code == 0

    def test_naming_one_file_creates_only_that_one(self, config_home):
        assert runner.invoke(app, ['config', 'init', 'registry']).exit_code == 0
        assert (config_home / 'repos.json').exists()
        assert not (config_home / 'config.toml').exists()

    def test_unknown_file_is_a_usage_error(self, config_home):
        assert runner.invoke(app, ['config', 'init', 'nope']).exit_code == 2

    def test_never_overwrites_an_existing_file(self, config_home):
        """Idempotent by design: the registry may be shared with other tools, so syncer creates one
        that is absent and modifies no existing one."""
        _write(config_home / 'config.toml', 'default_policy = "observe"\n')
        _write(config_home / 'repos.json', '{"owner": "me", "repos": []}')
        result = runner.invoke(app, ['config', 'init'])
        assert result.exit_code == 0
        assert (config_home / 'config.toml').read_text() == 'default_policy = "observe"\n'
        assert (config_home / 'repos.json').read_text() == '{"owner": "me", "repos": []}'

    def test_creates_the_registry_where_repos_file_points(self, config_home, tmp_path):
        """The scaffold lands at the path syncer will actually read, not at the default — a
        registry created anywhere else is a file nothing loads."""
        elsewhere = tmp_path / 'shared' / 'registry.json'
        _write(config_home / 'config.toml', f'repos_file = "{elsewhere}"\n')
        result = runner.invoke(app, ['config', 'init', 'registry'])
        assert result.exit_code == 0
        assert elsewhere.read_text() == STARTER_REGISTRY
        assert not (config_home / 'repos.json').exists()
        # The provenance is the answer to "why is it looking there", so it rides on the message.
        assert 'repos_file' in result.output

    def test_the_config_it_just_wrote_names_the_registry_path(self, config_home, tmp_path):
        """Resolution order matters within one invocation: reading the tool config before writing
        it would scaffold the registry against a config that did not exist yet."""
        result = runner.invoke(app, ['config', 'init'])
        assert result.exit_code == 0
        assert (config_home / 'repos.json').exists()


class TestConfigExample:
    def test_prints_the_tool_config_template_by_default(self, config_home):
        result = runner.invoke(app, ['config', 'example'])
        assert 'default_policy' in result.output

    def test_naming_the_registry_prints_the_registry_template(self, config_home):
        result = runner.invoke(app, ['config', 'example', 'registry'])
        assert json.loads(result.output)['repos']

    def test_unknown_file_is_a_usage_error(self, config_home):
        assert runner.invoke(app, ['config', 'example', 'nope']).exit_code == 2

    def test_redirected_output_is_only_the_template(self, config_home):
        """The where-to-put-it hint goes to stderr, so `config example registry > repos.json`
        still produces a parseable file."""
        result = runner.invoke(app, ['config', 'example', 'registry'])
        assert result.stdout == TEMPLATE_REGISTRY


class TestConfigScan:
    """Naming thirty repos by hand is the step that makes setting this up feel like work, and
    the answer is already sitting in the filesystem."""

    def test_it_finds_repos_and_derives_the_registry_identity(self, config_home, tmp_path):
        code = tmp_path / 'code'
        _git_repo(code / 'api', 'https://github.com/me/api')
        _git_repo(code / 'web', 'https://github.com/me/web')
        result = runner.invoke(app, ['config', 'scan', str(code)])
        registry = json.loads(result.stdout)
        assert registry['owner'] == 'me'
        assert registry['host'] == 'https://github.com'
        assert {repo['name'] for repo in registry['repos']} == {'api', 'web'}

    def test_a_repo_with_a_different_owner_keeps_its_own(self, config_home, tmp_path):
        """A directory holding both your repos and third-party clones has to scan correctly —
        that is exactly the shape of the exemplar registry."""
        code = tmp_path / 'code'
        _git_repo(code / 'mine', 'https://github.com/me/mine')
        _git_repo(code / 'also-mine', 'https://github.com/me/also-mine')
        _git_repo(code / 'vuetify', 'https://github.com/vuetifyjs/vuetify')
        registry = json.loads(runner.invoke(app, ['config', 'scan', str(code)]).stdout)
        by_name = {repo['name']: repo for repo in registry['repos']}
        assert registry['owner'] == 'me'
        assert by_name['vuetify']['owner'] == 'vuetifyjs'
        assert 'owner' not in by_name['mine']

    def test_the_scan_it_prints_is_a_valid_registry(self, config_home, tmp_path):
        code = tmp_path / 'code'
        _git_repo(code / 'api', 'git@github.com:me/api.git')
        _write(config_home / 'repos.json', runner.invoke(app, ['config', 'scan', str(code)]).stdout)
        assert runner.invoke(app, ['config', 'validate']).exit_code == 0

    def test_it_prints_rather_than_writes_by_default(self, config_home, tmp_path):
        """A review step, because the registry may be shared with other tools."""
        code = tmp_path / 'code'
        _git_repo(code / 'api', 'https://github.com/me/api')
        runner.invoke(app, ['config', 'scan', str(code)])
        assert not (config_home / 'repos.json').exists()

    def test_write_refuses_to_clobber_a_registry_with_repos_in_it(self, config_home, tmp_path):
        code = tmp_path / 'code'
        _git_repo(code / 'api', 'https://github.com/me/api')
        existing = '{"owner": "me", "repos": [{"name": "kept", "path": "~/kept"}]}'
        _write(config_home / 'repos.json', existing)
        result = runner.invoke(app, ['config', 'scan', str(code), '--write'])
        assert result.exit_code == 1
        assert (config_home / 'repos.json').read_text() == existing

    def test_a_url_the_default_shape_cannot_express_is_recorded_verbatim(self, config_home, tmp_path):
        """Better an entry carrying its real origin than one whose URL cannot be rebuilt."""
        code = tmp_path / 'code'
        _git_repo(code / 'odd', 'ssh://git@host:7999/odd.git')
        registry = json.loads(runner.invoke(app, ['config', 'scan', str(code)]).stdout)
        assert registry['repos'][0]['clone_url'] == 'ssh://git@host:7999/odd.git'

    def test_write_creates_one_that_is_absent(self, config_home, tmp_path):
        code = tmp_path / 'code'
        _git_repo(code / 'api', 'https://github.com/me/api')
        assert runner.invoke(app, ['config', 'scan', str(code), '--write']).exit_code == 0
        assert json.loads((config_home / 'repos.json').read_text())['repos'][0]['name'] == 'api'

    def test_write_fills_the_empty_registry_init_just_wrote(self, config_home, tmp_path):
        """The documented flow is `config init` then `config scan --write`. Refusing here made
        those two steps contradict each other — the no-clobber rule guards content, not the
        empty scaffold syncer wrote seconds earlier."""
        code = tmp_path / 'code'
        _git_repo(code / 'api', 'https://github.com/me/api')
        assert runner.invoke(app, ['config', 'init']).exit_code == 0
        assert runner.invoke(app, ['config', 'scan', str(code), '--write']).exit_code == 0
        assert json.loads((config_home / 'repos.json').read_text())['repos'][0]['name'] == 'api'

    def test_an_unreadable_registry_is_never_overwritten(self, config_home, tmp_path):
        """A file syncer cannot parse is the last one to clobber silently."""
        code = tmp_path / 'code'
        _git_repo(code / 'api', 'https://github.com/me/api')
        _write(config_home / 'repos.json', 'not json at all')
        assert runner.invoke(app, ['config', 'scan', str(code), '--write']).exit_code == 1
        assert (config_home / 'repos.json').read_text() == 'not json at all'

    def test_a_path_that_is_not_a_directory_is_a_usage_error(self, config_home, tmp_path):
        assert runner.invoke(app, ['config', 'scan', str(tmp_path / 'nope')]).exit_code == 2


class TestARegistryThatIsASymlink:
    """A shared registry is commonly a symlink to wherever it is really kept, and every write has
    to follow it rather than land on top of it.

    Replacing the link with a regular file forks the registry silently. Both files are then valid
    JSON, readers that resolve the link get the copy, and the original stops receiving writes
    while still looking authoritative — measured 2026-08-12, when one clobbered registry carried
    an entry no other copy had. Nothing errors at any point.

    A temp-file-and-rename write does exactly that, so these tests assert the invariant and not
    the implementation: after any write the path is still a symlink, and the content is in the
    file it points at. Hardening these writes to be atomic is allowed to change how they get
    there and is not allowed to change either of those.
    """

    def test_scan_writes_through_the_symlink_rather_than_over_it(self, config_home, tmp_path):
        real = tmp_path / 'shared' / 'repos.json'
        _write(real, STARTER_REGISTRY)
        link = config_home / 'repos.json'
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)
        code = tmp_path / 'code'
        _git_repo(code / 'api', 'https://github.com/me/api')

        assert runner.invoke(app, ['config', 'scan', str(code), '--write']).exit_code == 0

        assert link.is_symlink()
        assert json.loads(real.read_text())['repos'][0]['name'] == 'api'

    def test_init_writes_through_a_symlink_whose_target_is_not_there_yet(self, config_home, tmp_path):
        """The fresh-machine order: the link is made when the registry is deployed, and the file
        it points at arrives when something first writes one."""
        real = tmp_path / 'shared' / 'repos.json'
        real.parent.mkdir(parents=True, exist_ok=True)
        link = config_home / 'repos.json'
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)

        assert runner.invoke(app, ['config', 'init', 'registry']).exit_code == 0

        assert link.is_symlink()
        assert real.read_text() == STARTER_REGISTRY

    def test_edit_seeds_through_the_symlink(self, config_home, tmp_path, monkeypatch):
        """`edit` seeds an absent registry before opening it, which is a third writer of the same
        file and gets the same guarantee."""
        monkeypatch.setenv('VISUAL', 'true')
        monkeypatch.delenv('EDITOR', raising=False)
        real = tmp_path / 'shared' / 'repos.json'
        real.parent.mkdir(parents=True, exist_ok=True)
        link = config_home / 'repos.json'
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to(real)

        assert runner.invoke(app, ['config', 'edit', 'registry']).exit_code == 0

        assert link.is_symlink()
        assert real.read_text() == STARTER_REGISTRY


class TestConfigEdit:
    """It opened only config.toml, while the file a new machine actually needs edited is the
    registry — naming your repos is the one step nothing else can do for you."""

    @pytest.fixture
    def fake_editor(self, tmp_path, monkeypatch):
        """A stand-in $EDITOR that records the path it was handed instead of blocking."""
        opened = tmp_path / 'opened.txt'
        script = tmp_path / 'fake-editor'
        script.write_text(f'#!/bin/sh\necho "$1" > {opened}\n')
        script.chmod(0o755)
        monkeypatch.setenv('VISUAL', str(script))
        monkeypatch.delenv('EDITOR', raising=False)
        return opened

    def test_it_opens_the_config_by_default(self, config_home, fake_editor):
        assert runner.invoke(app, ['config', 'edit']).exit_code == 0
        assert fake_editor.read_text().strip() == str(config_home / 'config.toml')

    def test_naming_the_registry_opens_the_registry(self, config_home, fake_editor):
        assert runner.invoke(app, ['config', 'edit', 'registry']).exit_code == 0
        assert fake_editor.read_text().strip() == str(config_home / 'repos.json')

    def test_it_opens_the_registry_syncer_actually_reads(self, config_home, fake_editor, tmp_path):
        """Resolved through registry_location, not an assumed default — the two differ on every
        machine that sets repos_file, and editing the wrong file changes nothing."""
        elsewhere = tmp_path / 'shared' / 'registry.json'
        _write(config_home / 'config.toml', f'repos_file = "{elsewhere}"\n')
        assert runner.invoke(app, ['config', 'edit', 'registry']).exit_code == 0
        assert fake_editor.read_text().strip() == str(elsewhere)

    def test_it_seeds_an_absent_file_before_opening(self, config_home, fake_editor):
        runner.invoke(app, ['config', 'edit', 'registry'])
        assert json.loads((config_home / 'repos.json').read_text())['repos'] == []

    def test_unknown_file_is_a_usage_error(self, config_home, fake_editor):
        assert runner.invoke(app, ['config', 'edit', 'nope']).exit_code == 2


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
        assert 'config init registry' in result.output

    def test_unreachable_repos_file_says_how_to_fall_back(self, config_home, tmp_path):
        """The work-box case: a config naming a registry this machine does not have. Reporting
        only the missing path leaves the reader with no way out of it."""
        result = self._run(config_home, f'repos_file = "{tmp_path / "elsewhere.json"}"\n')
        assert result.exit_code == 1
        assert 'comment it out' in result.output

    def test_repo_paths_are_not_checked_against_the_disk(self, config_home):
        """validate checks structure, issues checks reality. Blurring them means neither gets
        trusted."""
        registry = json.dumps({'owner': 'me', 'repos': [{'name': 'r', 'path': '/nowhere/at/all'}]})
        assert self._run(config_home, registry=registry).exit_code == 0
