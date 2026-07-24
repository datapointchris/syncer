import json

import pytest

from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import ToolConfig
from syncer.config import _load_repos_file
from syncer.config import get_repos_file_path
from syncer.config import load_tool_config
from syncer.config import resolve_config
from syncer.config import resolve_policies
from syncer.config import resolve_policy_name
from syncer.policy import Action
from syncer.policy import Scope


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

    def test_invalid_policy_action_fails_loudly(self, tool_config):
        tool_config.write_text('[policies.bad]\n[policies.bad.rules]\n"default:behind" = "nuke"\n')
        with pytest.raises(ValueError, match='unknown action'):
            load_tool_config()

    def test_parses_repo_overrides(self, tool_config):
        tool_config.write_text('[repo_overrides]\n"shared-repo" = "observe"\n')
        loaded = load_tool_config()
        assert loaded.repo_overrides == {'shared-repo': 'observe'}


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

    def test_falls_back_to_legacy(self, tool_config, tmp_path, monkeypatch):
        legacy_dir = tmp_path / 'legacy'
        legacy_dir.mkdir()
        (legacy_dir / 'test.json').write_text('{}')
        monkeypatch.setattr('syncer.config._LEGACY_CONFIG_DIR', legacy_dir)
        # tool_config doesn't exist (not written), so falls back
        assert get_repos_file_path() == legacy_dir / 'test.json'

    def test_exits_when_no_config(self, tool_config, tmp_path, monkeypatch):
        monkeypatch.setattr('syncer.config._LEGACY_CONFIG_DIR', tmp_path / 'nonexistent')
        with pytest.raises(SystemExit):
            get_repos_file_path()


class TestResolveConfig:
    def test_resolve_via_tool_config(self, tool_config, repos_file, sample_config):
        repos_file.write_text(json.dumps(sample_config))
        tool_config.write_text(f'repos_file = "{repos_file}"\n')
        config = resolve_config()
        assert config.owner == 'testuser'
