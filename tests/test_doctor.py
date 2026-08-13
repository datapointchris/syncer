import json
import subprocess
from pathlib import Path

import pytest

from syncer.doctor import Status
from syncer.doctor import doctor_exit_code
from syncer.doctor import render_doctor
from syncer.doctor import run_doctor


@pytest.fixture
def isolated(tmp_path, monkeypatch):
    """Point every resolved path at tmp_path, so doctor never reads the real machine."""
    config_dir = tmp_path / 'config' / 'syncer'
    config_dir.mkdir(parents=True)
    monkeypatch.setattr('syncer.doctor.TOOL_CONFIG_PATH', config_dir / 'config.toml')
    monkeypatch.setattr('syncer.config.TOOL_CONFIG_PATH', config_dir / 'config.toml')
    monkeypatch.setattr('syncer.doctor.STATE_DIR', tmp_path / 'state')
    monkeypatch.setattr('syncer.config.DEFAULT_REPOS_FILE', config_dir / 'repos.json')
    monkeypatch.setattr('syncer.doctor.DEFAULT_REPOS_FILE', config_dir / 'repos.json')
    return config_dir


def _bare_repo(tmp_path: Path, name: str) -> Path:
    """A real local remote, so the reachability probe can succeed with no network."""
    bare = tmp_path / f'{name}.git'
    subprocess.run(['git', 'init', '--bare', '-b', 'main', str(bare)], capture_output=True)
    seed = tmp_path / f'{name}-seed'
    subprocess.run(['git', 'clone', str(bare), str(seed)], capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 't@t.com'], cwd=seed, capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'T'], cwd=seed, capture_output=True)
    (seed / 'README.md').write_text('# t\n')
    subprocess.run(['git', 'add', 'README.md'], cwd=seed, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'init'], cwd=seed, capture_output=True)
    subprocess.run(['git', 'push', '-u', 'origin', 'main'], cwd=seed, capture_output=True)
    return bare


def _write_registry(config_dir: Path, **overrides) -> Path:
    registry = config_dir / 'repos.json'
    registry.write_text(json.dumps({'owner': 'me', 'host': 'https://github.com', 'search_paths': [], 'repos': [], **overrides}))
    return registry


def _named(checks, name):
    return [check for check in checks if check.name == name]


class TestPrerequisiteOrder:
    def test_a_missing_registry_stops_before_the_network(self, isolated):
        """Nothing below a registry that will not load can say anything useful."""
        checks = run_doctor()
        assert _named(checks, 'registry')[0].status is Status.FAIL
        assert _named(checks, 'reach') == []

    def test_paths_are_reported_even_when_everything_else_fails(self, isolated):
        """The line that would have made the work-box incident diagnosable: which files syncer
        is actually reading, and what chose them."""
        checks = run_doctor()
        [paths] = _named(checks, 'paths')
        assert paths.status is Status.OK
        assert any('registry' in line for line in paths.detail)

    def test_the_registry_path_carries_its_provenance(self, isolated):
        (isolated / 'config.toml').write_text('repos_registry = "/nowhere/repos.json"\n')
        checks = run_doctor()
        [paths] = _named(checks, 'paths')
        assert any('from repos_registry' in line for line in paths.detail)

    def test_a_broken_config_is_named_not_raised(self, isolated):
        """load_tool_config sys.exits on a bad file; a diagnostic must not become the crash it
        exists to explain."""
        (isolated / 'config.toml').write_text('[policies.bad.rules]\n"*:ahead" = "not-an-action"\n')
        checks = run_doctor()
        [config_check] = _named(checks, 'config')
        assert config_check.status is Status.FAIL
        assert any('not-an-action' in line for line in config_check.detail)


class TestIdentity:
    def test_the_shipped_placeholders_are_caught(self, isolated):
        _write_registry(
            isolated,
            owner='your-github-username',
            repos=[{'name': 'example-repo', 'path': '~/code/example-repo'}],
        )
        checks = run_doctor()
        [identity] = _named(checks, 'identity')
        assert identity.status is Status.FAIL
        assert 'example-repo' in identity.detail

    def test_placeholders_skip_the_network_probe(self, isolated):
        """Probing fake URLs reports a network problem for what is really an unedited config."""
        _write_registry(isolated, owner='your-github-username', repos=[{'name': 'example-repo', 'path': '~/x'}])
        checks = run_doctor()
        assert _named(checks, 'reach')[0].status is Status.WARN
        assert 'skipped' in _named(checks, 'reach')[0].summary

    def test_an_empty_owner_is_only_a_problem_for_repos_that_use_it(self, isolated):
        _write_registry(isolated, owner='', repos=[{'name': 'api', 'path': '~/code/api'}])
        checks = run_doctor()
        [urls] = _named(checks, 'urls')
        assert urls.status is Status.FAIL
        assert any('//' in line for line in urls.detail)

    def test_an_all_third_party_registry_needs_no_owner(self, isolated):
        """The exemplar registry names an owner per entry, which is legitimate."""
        _write_registry(isolated, owner='', repos=[{'name': 'vuetify', 'path': '~/refs/vuetify', 'owner': 'vuetifyjs'}])
        assert _named(run_doctor(), 'urls')[0].status is Status.OK

    def test_an_empty_registry_is_not_an_identity_failure(self, isolated):
        """A freshly scaffolded registry has no owner and no repos, and that is a legitimate
        state to be in — it must not report as broken."""
        _write_registry(isolated, owner='')
        assert _named(run_doctor(), 'urls')[0].status is Status.OK


class TestReachability:
    def test_a_reachable_remote_passes(self, isolated, tmp_path):
        bare = _bare_repo(tmp_path, 'api')
        _write_registry(isolated, repos=[{'name': 'api', 'path': str(tmp_path / 'api'), 'clone_url': str(bare)}])
        assert _named(run_doctor(), 'reach')[0].status is Status.OK

    def test_an_unreachable_remote_fails_with_a_cause_and_a_hint(self, isolated, tmp_path):
        _write_registry(isolated, repos=[{'name': 'api', 'path': str(tmp_path / 'api'), 'clone_url': str(tmp_path / 'gone.git')}])
        [reach] = _named(run_doctor(), 'reach')
        assert reach.status is Status.FAIL
        assert reach.detail  # git's own words
        assert reach.hints  # and something to do about it

    def test_one_probe_per_host_not_per_repo(self, isolated, tmp_path):
        """Thirty repos on one host is one question, and thirty probes of it is thirty times the
        wait for the same answer."""
        bare = _bare_repo(tmp_path, 'shared')
        _write_registry(
            isolated,
            repos=[{'name': f'r{i}', 'path': str(tmp_path / f'r{i}'), 'clone_url': str(bare)} for i in range(5)],
        )
        assert len(_named(run_doctor(), 'reach')) == 1

    def test_gh_is_never_invoked(self, isolated, tmp_path, monkeypatch):
        """A remote may be Bitbucket or internal; doctor must not assume GitHub."""
        calls = []
        real = subprocess.run

        def spy(args, **kwargs):
            calls.append(args)
            return real(args, **kwargs)

        monkeypatch.setattr('syncer.repos.subprocess.run', spy)
        _write_registry(isolated, repos=[{'name': 'api', 'path': str(tmp_path / 'api'), 'clone_url': str(tmp_path / 'gone.git')}])
        run_doctor()
        assert not any(args and args[0] == 'gh' for args in calls)


class TestClonesAndPolicy:
    def test_missing_clones_are_one_fact_not_n_warnings(self, isolated, tmp_path):
        bare = _bare_repo(tmp_path, 'api')
        _write_registry(
            isolated,
            repos=[{'name': f'r{i}', 'path': str(tmp_path / f'nope{i}'), 'clone_url': str(bare)} for i in range(3)],
        )
        [clones] = _named(run_doctor(), 'clones')
        assert clones.status is Status.WARN
        assert '0 of 3' in clones.summary
        assert 'syncer --apply' in ' '.join(clones.hints)

    def test_it_does_not_suggest_cloning_when_the_host_is_unreachable(self, isolated, tmp_path):
        _write_registry(isolated, repos=[{'name': 'api', 'path': str(tmp_path / 'api'), 'clone_url': str(tmp_path / 'gone.git')}])
        [clones] = _named(run_doctor(), 'clones')
        assert 'syncer --apply' not in ' '.join(clones.hints)

    def test_an_unresolvable_default_policy_fails(self, isolated):
        (isolated / 'config.toml').write_text('default_policy = "nonexistent"\n')
        _write_registry(isolated)
        [policy] = _named(run_doctor(), 'policy')
        assert policy.status is Status.FAIL


class TestExitCode:
    def test_fail_exits_one(self, isolated):
        assert doctor_exit_code(run_doctor()) == 1  # no registry

    def test_warn_alone_exits_zero(self, isolated, tmp_path):
        """An un-cloned repo set is a state you can be in on purpose; a registry that will not
        load means the next command is going to fail."""
        bare = _bare_repo(tmp_path, 'api')
        _write_registry(isolated, repos=[{'name': 'api', 'path': str(tmp_path / 'nope'), 'clone_url': str(bare)}])
        checks = run_doctor()
        assert any(check.status is Status.WARN for check in checks)
        assert doctor_exit_code(checks) == 0


class TestRendering:
    def test_everything_goes_to_stderr(self, isolated, capsys):
        """A diagnostic is not data anyone pipes into jq."""
        render_doctor(run_doctor())
        captured = capsys.readouterr()
        assert captured.out == ''
        assert 'registry' in captured.err
