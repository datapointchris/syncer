import json

import pytest

from syncer.config import SyncerConfig
from syncer.config import load_config
from syncer.config import resolve_config


@pytest.fixture
def config_dir(tmp_path, monkeypatch):
    config_dir = tmp_path / '.config' / 'syncer'
    config_dir.mkdir(parents=True)
    monkeypatch.setattr('syncer.config.CONFIG_DIR', config_dir)
    return config_dir


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


class TestLoadConfig:
    def test_load_existing_config(self, config_dir, sample_config):
        config_file = config_dir / 'test.json'
        config_file.write_text(json.dumps(sample_config))

        config = load_config('test')
        assert config.owner == 'testuser'
        assert len(config.repos) == 2

    def test_load_missing_config(self, config_dir):
        with pytest.raises(SystemExit):
            load_config('nonexistent')


class TestResolveConfig:
    def test_resolve_with_name(self, config_dir, sample_config):
        (config_dir / 'myconfig.json').write_text(json.dumps(sample_config))
        config = resolve_config('myconfig')
        assert config.owner == 'testuser'

    def test_resolve_single_file(self, config_dir, sample_config):
        (config_dir / 'only.json').write_text(json.dumps(sample_config))
        config = resolve_config()
        assert config.owner == 'testuser'

    def test_resolve_multiple_files_exits(self, config_dir, sample_config):
        (config_dir / 'one.json').write_text(json.dumps(sample_config))
        (config_dir / 'two.json').write_text(json.dumps(sample_config))
        with pytest.raises(SystemExit):
            resolve_config()

    def test_resolve_no_files_exits(self, config_dir):
        with pytest.raises(SystemExit):
            resolve_config()

    def test_resolve_no_dir_exits(self, tmp_path, monkeypatch):
        monkeypatch.setattr('syncer.config.CONFIG_DIR', tmp_path / 'nonexistent')
        with pytest.raises(SystemExit):
            resolve_config()
