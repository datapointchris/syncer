import json

import pytest

from syncer.config import RepoConfig
from syncer.config import SyncerConfig
from syncer.config import _load_repos_file
from syncer.config import get_repos_file_path
from syncer.config import resolve_config


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
