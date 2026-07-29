import json
from pathlib import Path

import pytest

from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import _load_repos_file
from syncer.config import get_repos_file_path
from syncer.config import load_tool_config
from syncer.config import resolve_clone_url
from syncer.config import resolve_config
from syncer.config import resolve_policies
from syncer.config import resolve_policy_name
from syncer.config import xdg_config_home
from syncer.config import xdg_state_home
from syncer.policy import Action
from syncer.policy import Scope
from syncer.repos import GIT_TIMEOUT_SECONDS


@pytest.fixture
def repos_file(tmp_path):
    return tmp_path / 'repos.json'


@pytest.fixture
def tool_config(tmp_path, monkeypatch):
    config_path = tmp_path / 'config.toml'
    monkeypatch.setattr('syncer.config.TOOL_CONFIG_PATH', config_path)
    return config_path


@pytest.fixture
def sample_config():
    return {
        'owner': 'testuser',
        'host': 'https://github.com',
        'search_paths': ['~/code'],
        'repos': [
            {'name': 'repo1', 'path': '~/code/repo1'},
            {'name': 'repo2', 'path': '~/tools/repo2'},
        ],
    }


@pytest.fixture
def sample_config_with_status():
    return {
        'owner': 'testuser',
        'host': 'https://github.com',
        'search_paths': ['~/code'],
        'repos': [
            {'name': 'active-repo', 'path': '~/code/active', 'status': 'active'},
            {'name': 'dormant-repo', 'path': '~/code/dormant', 'status': 'dormant'},
            {'name': 'retired-repo', 'path': '~/code/retired', 'status': 'retired'},
        ],
    }


class TestRepoConfig:
    def test_status_defaults_to_active(self):
        repo = RepoConfig(name='test', path='~/code/test')
        assert repo.status == 'active'

    def test_explicit_status(self):
        repo = RepoConfig(name='test', path='~/code/test', status='dormant')
        assert repo.status == 'dormant'

    def test_invalid_status_rejected(self):
        with pytest.raises(ValueError):
            RepoConfig(name='test', path='~/code/test', status='invalid')

    def test_sync_policy_defaults_to_none(self):
        repo = RepoConfig(name='test', path='~/code/test')
        assert repo.sync_policy is None

    def test_sync_policy_hint(self):
        repo = RepoConfig(name='test', path='~/code/test', sync_policy='observe')
        assert repo.sync_policy == 'observe'


class TestLoadToolConfig:
    def test_missing_config_returns_defaults(self, tool_config):
        # tool_config fixture monkeypatches the path but doesn't write it
        loaded = load_tool_config()
        assert loaded.repos_file is None
        assert loaded.default_policy == 'standard'
        assert loaded.policies == {}

    def test_parses_repos_file_and_default_policy(self, tool_config):
        tool_config.write_text('repos_file = "~/dev/repos.json"\ndefault_policy = "observe"\n')
        loaded = load_tool_config()
        assert loaded.repos_file == '~/dev/repos.json'
        assert loaded.default_policy == 'observe'

    def test_parses_custom_policy_with_injected_name(self, tool_config):
        tool_config.write_text(
            'default_policy = "laptop"\n'
            '[policies.laptop]\n'
            'scope = "all"\n'
            'prune = true\n'
            'fallback = "report"\n'
            '[policies.laptop.rules]\n'
            '"default:behind" = "pull_ff"\n'
        )
        loaded = load_tool_config()
        policy = loaded.policies['laptop']
        assert policy.name == 'laptop'
        assert policy.scope == Scope.ALL
        assert policy.rules['default:behind'] == 'pull_ff'

    def test_invalid_policy_action_fails_loudly(self, tool_config, capsys):
        tool_config.write_text('[policies.bad]\n[policies.bad.rules]\n"default:behind" = "nuke"\n')
        with pytest.raises(SystemExit):
            load_tool_config()
        assert 'unknown action' in capsys.readouterr().err

    def test_git_timeout_defaults_and_overrides(self, tool_config):
        assert load_tool_config().git_timeout == GIT_TIMEOUT_SECONDS
        tool_config.write_text('git_timeout = 300\n')
        assert load_tool_config().git_timeout == 300

    def test_parses_repo_overrides(self, tool_config):
        tool_config.write_text('[repo_overrides]\n"shared-repo" = "observe"\n')
        loaded = load_tool_config()
        assert loaded.repo_overrides == {'shared-repo': 'observe'}


class TestResolveCloneUrl:
    """The default '{host}/{owner}/{name}' cannot express every host: scp-style SSH has no
    slash after the host, and some servers want the .git suffix."""

    def _config(self, **kwargs):
        defaults = {'owner': 'datapointchris', 'host': 'https://github.com', 'repos': []}
        return SyncerConfig(**{**defaults, **kwargs})

    def test_default_three_part_path(self):
        repo = RepoConfig(name='syncer', path='~/tools/syncer')
        assert resolve_clone_url(repo, self._config()) == 'https://github.com/datapointchris/syncer'

    def test_repo_owner_overrides_registry_owner(self):
        repo = RepoConfig(name='vuetify', path='~/code/refs/vuetify', owner='vuetifyjs')
        assert resolve_clone_url(repo, self._config()) == 'https://github.com/vuetifyjs/vuetify'

    def test_template_renders_scp_style_ssh(self):
        config = self._config(owner='myworkspace', host='bitbucket.org', url_template='git@{host}:{owner}/{name}.git')
        repo = RepoConfig(name='payments', path='~/code/1904labs/payments')
        assert resolve_clone_url(repo, config) == 'git@bitbucket.org:myworkspace/payments.git'

    def test_template_renders_bitbucket_data_center(self):
        config = self._config(owner='PROJ', host='https://bitbucket.corp.com', url_template='{host}/scm/{owner}/{name}.git')
        repo = RepoConfig(name='payments', path='~/code/1904labs/payments')
        assert resolve_clone_url(repo, config) == 'https://bitbucket.corp.com/scm/PROJ/payments.git'

    def test_per_repo_clone_url_beats_template(self):
        config = self._config(url_template='git@{host}:{owner}/{name}.git')
        repo = RepoConfig(name='odd-one', path='~/code/odd', clone_url='ssh://git@other.host:7999/x/odd.git')
        assert resolve_clone_url(repo, config) == 'ssh://git@other.host:7999/x/odd.git'

    def test_template_without_name_rejected(self):
        with pytest.raises(ValueError, match='must include'):
            self._config(url_template='git@{host}:{owner}.git')

    def test_template_with_unknown_placeholder_rejected(self):
        with pytest.raises(ValueError, match='unknown placeholder'):
            self._config(url_template='git@{host}:{project}/{name}.git')


class TestResolvePolicies:
    def test_builtins_available_with_no_user_policies(self):
        merged = resolve_policies(ToolConfig())
        assert set(merged) >= {'standard', 'observe', 'mirror'}

    def test_user_policy_overrides_builtin_by_name(self, tool_config):
        tool_config.write_text('[policies.standard]\nscope = "all"\nfallback = "skip"\n')
        merged = resolve_policies(load_tool_config())
        assert merged['standard'].scope == Scope.ALL
        assert merged['standard'].fallback == Action.SKIP


class TestResolvePolicyName:
    def _repo(self, **kwargs):
        return RepoConfig(name='myrepo', path='~/code/myrepo', **kwargs)

    def test_cli_flag_wins(self):
        tc = ToolConfig(default_policy='standard', repo_overrides={'myrepo': 'observe'})
        repo = self._repo(sync_policy='mirror')
        assert resolve_policy_name(repo, tc, cli_policy='standard') == 'standard'

    def test_repo_override_beats_hint_and_default(self):
        tc = ToolConfig(default_policy='standard', repo_overrides={'myrepo': 'observe'})
        repo = self._repo(sync_policy='mirror')
        assert resolve_policy_name(repo, tc) == 'observe'

    def test_repos_json_hint_beats_default(self):
        tc = ToolConfig(default_policy='standard')
        repo = self._repo(sync_policy='mirror')
        assert resolve_policy_name(repo, tc) == 'mirror'

    def test_machine_default_used_when_no_hint(self):
        tc = ToolConfig(default_policy='observe')
        assert resolve_policy_name(self._repo(), tc) == 'observe'

    def test_falls_back_to_standard(self):
        tc = ToolConfig(default_policy='')
        assert resolve_policy_name(self._repo(), tc) == 'standard'


class TestSyncerConfig:
    def test_valid_config(self, sample_config):
        config = SyncerConfig(**sample_config)
        assert config.owner == 'testuser'
        assert config.host == 'https://github.com'
        assert len(config.repos) == 2
        assert config.repos[0].name == 'repo1'

    def test_config_without_search_paths(self):
        config = SyncerConfig(
            owner='testuser',
            host='https://github.com',
            repos=[{'name': 'repo1', 'path': '~/code/repo1'}],
        )
        assert config.search_paths == []

    def test_config_preserves_status(self, sample_config_with_status):
        config = SyncerConfig(**sample_config_with_status)
        assert config.repos[0].status == 'active'
        assert config.repos[1].status == 'dormant'
        assert config.repos[2].status == 'retired'

    def test_status_survives_round_trip(self, sample_config_with_status):
        config = SyncerConfig(**sample_config_with_status)
        dumped = json.loads(json.dumps(config.model_dump()))
        restored = SyncerConfig(**dumped)
        assert restored.repos[2].status == 'retired'


class TestLoadReposFile:
    def test_load_existing_file(self, repos_file, sample_config):
        repos_file.write_text(json.dumps(sample_config))
        config = _load_repos_file(repos_file)
        assert config.owner == 'testuser'
        assert len(config.repos) == 2

    def test_load_missing_file(self, repos_file):
        with pytest.raises(SystemExit):
            _load_repos_file(repos_file)

    def test_repos_sorted_by_path(self, repos_file, sample_config):
        repos_file.write_text(json.dumps(sample_config))
        config = _load_repos_file(repos_file)
        paths = [r.path for r in config.repos]
        assert paths == sorted(paths)


class TestGetReposFilePath:
    def test_reads_from_tool_config(self, tool_config, repos_file):
        tool_config.write_text(f'repos_file = "{repos_file}"\n')
        assert get_repos_file_path() == repos_file

    def test_explicit_override_wins(self, tool_config, repos_file, tmp_path):
        tool_config.write_text(f'repos_file = "{repos_file}"\n')
        other = tmp_path / 'work-repos.json'
        assert get_repos_file_path(other) == other

    def test_defaults_into_the_xdg_config_dir(self, tool_config, tmp_path, monkeypatch):
        """A fresh machine gets a working default rather than an error naming one fleet's
        directory layout. The old behaviour globbed ~/.config/syncer/*.json — picking up any
        stray JSON — and otherwise hard-exited."""
        monkeypatch.setattr('syncer.config.DEFAULT_REPOS_FILE', tmp_path / 'syncer' / 'repos.json')
        # tool_config is not written, so nothing names a registry
        assert get_repos_file_path() == tmp_path / 'syncer' / 'repos.json'


class TestResolveConfig:
    def test_resolve_via_tool_config(self, tool_config, repos_file, sample_config):
        repos_file.write_text(json.dumps(sample_config))
        tool_config.write_text(f'repos_file = "{repos_file}"\n')
        config = resolve_config()
        assert config.owner == 'testuser'


class TestRegistryIndependence:
    """A registry is a self-contained set: a different file swaps the whole working
    set rather than merging with the default."""

    def test_owner_and_host_are_optional(self):
        """An all-third-party registry has no single owner — every repo names its own."""
        config = SyncerConfig.model_validate({'repos': [{'name': 'bubbletea', 'path': '~/code/refs/bubbletea', 'owner': 'charmbracelet'}]})
        assert config.owner == ''
        assert config.host == 'https://github.com'
        assert config.repos[0].owner == 'charmbracelet'

    def test_search_and_exclude_paths_default_empty(self):
        """A registry that claims no directory scans nothing and excludes nothing."""
        config = SyncerConfig.model_validate({'owner': 'me', 'host': 'https://github.com', 'repos': []})
        assert config.search_paths == []
        assert config.exclude_paths == []

    def test_exclude_paths_round_trip(self):
        config = SyncerConfig.model_validate({'owner': 'me', 'host': 'https://github.com', 'exclude_paths': ['~/code/refs'], 'repos': []})
        assert config.exclude_paths == ['~/code/refs']

    def test_unknown_fields_are_ignored(self):
        """The exemplar registry carries fields syncer has no use for."""
        config = SyncerConfig.model_validate(
            {
                'purpose': 'read, not worked in',
                'clone_root': '~/code/refs',
                'repos': [
                    {
                        'name': 'chi',
                        'path': '~/code/refs/chi',
                        'owner': 'go-chi',
                        'exemplary_for': 'middleware composition',
                        'index_exclude': ['testdata/**'],
                    }
                ],
            }
        )
        assert config.repos[0].name == 'chi'


class TestXDGPaths:
    """Every path syncer writes is an XDG base directory, resolved through the environment
    variable rather than a hardcoded ~/.config — a machine that relocates its config or state
    home is otherwise silently ignored."""

    def test_config_home_honours_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv('XDG_CONFIG_HOME', str(tmp_path / 'cfg'))
        assert xdg_config_home() == tmp_path / 'cfg'

    def test_state_home_honours_the_environment(self, tmp_path, monkeypatch):
        monkeypatch.setenv('XDG_STATE_HOME', str(tmp_path / 'st'))
        assert xdg_state_home() == tmp_path / 'st'

    def test_documented_fallbacks_when_unset(self, monkeypatch):
        monkeypatch.delenv('XDG_CONFIG_HOME', raising=False)
        monkeypatch.delenv('XDG_STATE_HOME', raising=False)
        assert xdg_config_home() == Path.home() / '.config'
        assert xdg_state_home() == Path.home() / '.local' / 'state'

    def test_tilde_in_the_override_is_expanded(self, monkeypatch):
        monkeypatch.setenv('XDG_STATE_HOME', '~/somewhere/state')
        assert xdg_state_home() == Path.home() / 'somewhere' / 'state'


class TestBrokenConfigIsExplained:
    """A malformed config used to surface as a raw pydantic traceback from whichever command
    happened to load it first. Every load path now prints what is actually wrong — the key that
    failed and why — rather than a traceback or a referral to another command."""

    def test_malformed_toml_names_the_syntax_error(self, tool_config, capsys):
        tool_config.write_text('default_policy = \n')
        with pytest.raises(SystemExit):
            load_tool_config()
        assert 'not valid TOML' in capsys.readouterr().err

    def test_a_bad_rule_names_the_policy_it_is_in(self, tool_config, capsys):
        """pydantic's own error says only `rules`, which is no help in a file holding several
        policies — the location has to carry the policy name."""
        tool_config.write_text('[policies.laptop.rules]\n"*:ahead" = "yolo"\n')
        with pytest.raises(SystemExit):
            load_tool_config()
        stderr = capsys.readouterr().err
        assert 'policies.laptop.rules' in stderr
        assert 'yolo' in stderr

    def test_a_bad_registry_names_the_offending_key(self, repos_file, capsys):
        repos_file.write_text(json.dumps({'owner': 'me', 'url_template': '{host}/{oops}/{name}', 'repos': []}))
        with pytest.raises(SystemExit):
            _load_repos_file(repos_file)
        stderr = capsys.readouterr().err
        assert 'url_template' in stderr
        assert 'unknown placeholder' in stderr

    def test_malformed_registry_json_names_the_syntax_error(self, repos_file, capsys):
        repos_file.write_text('{not json')
        with pytest.raises(SystemExit):
            _load_repos_file(repos_file)
        assert 'not valid JSON' in capsys.readouterr().err
