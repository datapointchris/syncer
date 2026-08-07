import json
import shutil
import subprocess

import pytest
from typer.testing import CliRunner

from syncer.main import app

runner = CliRunner()


class TestBareInvocation:
    """The run used to hang off the root callback, so `syncer` scanned every repo and
    `syncer --apply` mutated them — a write one flag away from the bare invocation. It is
    `syncer check` / `syncer apply` now, and bare shows help. See cli-design.md, 'No args
    shows help'."""

    def test_bare_invocation_shows_help(self, monkeypatch):
        monkeypatch.setattr('syncer.main.notify', lambda *_: None)
        result = runner.invoke(app, [])
        assert 'Usage:' in result.output
        assert 'check' in result.output

    @pytest.mark.parametrize('flag', ['--policy=standard', '--jobs=2', '--repos-file=x.json', '--json', '--per-branch'])
    def test_action_flags_are_rejected_at_the_root(self, flag, monkeypatch):
        """Flags on the root callback are the tell that a default is really a command."""
        monkeypatch.setattr('syncer.main.notify', lambda *_: None)
        assert runner.invoke(app, [flag]).exit_code != 0

    @pytest.mark.parametrize('verb', ['check', 'apply'])
    @pytest.mark.parametrize('flag', ['--policy=standard', '--jobs=2', '--json', '--per-branch'])
    def test_both_verbs_accept_them_instead(self, verb, flag, monkeypatch):
        monkeypatch.setattr('syncer.main.notify', lambda *_: None)
        assert 'No such option' not in runner.invoke(app, [verb, flag, '--help']).output


class TestWritingIsAVerbNotAFlag:
    """`check --apply` put the write inside the read, and `--dry-run` existed only to cancel
    it — a flag whose whole job is undoing another flag. `check` is now the dry run."""

    @pytest.mark.parametrize('verb', ['check', 'apply'])
    @pytest.mark.parametrize('flag', ['--apply', '--dry-run'])
    def test_the_old_flags_are_gone(self, verb, flag, monkeypatch):
        monkeypatch.setattr('syncer.main.notify', lambda *_: None)
        assert runner.invoke(app, [verb, flag]).exit_code != 0

    def test_branches_is_no_longer_a_command(self, monkeypatch):
        monkeypatch.setattr('syncer.main.notify', lambda *_: None)
        assert runner.invoke(app, ['branches']).exit_code != 0


class TestIssuesMasterCheck:
    """A company's default-branch naming is not ours to change, so flagging master across
    thirty work repos is noise that can never be actioned."""

    def _registry(self, tmp_path, **extra):
        repo = tmp_path / 'work-repo'
        subprocess.run(['git', 'init', '-b', 'master', str(repo)], capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=repo, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo, capture_output=True)
        (repo / 'README.md').write_text('# work\n')
        subprocess.run(['git', 'add', '.'], cwd=repo, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'init'], cwd=repo, capture_output=True)
        registry_data = {
            'owner': 'myworkspace',
            'host': 'https://bitbucket.org',
            'search_paths': [],
            'repos': [{'name': 'work-repo', 'path': str(repo)}],
        }
        registry = tmp_path / 'work-repos.json'
        registry.write_text(json.dumps(registry_data | extra))
        return registry

    def _run_issues(self, registry, monkeypatch, tmp_path):
        monkeypatch.setattr('syncer.main.notify', lambda *_: None)
        monkeypatch.setattr('syncer.config.TOOL_CONFIG_PATH', tmp_path / 'absent.toml')
        return runner.invoke(app, ['issues', '-c', str(registry)])

    def test_flagged_when_branch_naming_is_ours(self, tmp_path, monkeypatch):
        result = self._run_issues(self._registry(tmp_path), monkeypatch, tmp_path)
        # Non-zero deliberately — this asserted 0 while issues were being reported, which is the
        # exact shape of a check nothing can be scripted against.
        assert result.exit_code == 1
        assert 'issue(s) found' in result.stdout

    def test_silent_when_branch_naming_is_not_ours(self, tmp_path, monkeypatch):
        registry = self._registry(tmp_path, owns_branch_naming=False)
        result = self._run_issues(registry, monkeypatch, tmp_path)
        assert result.exit_code == 0
        # The all-clear names the one thing this command measures. It used to read 'All repos
        # healthy', a verdict on sync state it never looks at — and printed it for a fleet that
        # `check` was simultaneously calling untidy.
        assert 'Registry matches the filesystem' in result.stdout
        assert 'healthy' not in result.stdout


class TestExitCodesAndJson:
    """Every run used to exit 0 however many repos failed, so nothing could be scripted around
    syncer without scraping its text."""

    def _registry(self, tmp_path, repos):
        registry = tmp_path / 'repos.json'
        registry.write_text(json.dumps({'owner': 'me', 'host': 'https://github.com', 'search_paths': [], 'repos': repos}))
        return registry

    def _healthy_repo(self, tmp_path, name='api'):
        bare = tmp_path / f'{name}.git'
        subprocess.run(['git', 'init', '--bare', '-b', 'main', str(bare)], capture_output=True)
        repo = tmp_path / name
        subprocess.run(['git', 'clone', str(bare), str(repo)], capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 't@t.com'], cwd=repo, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'T'], cwd=repo, capture_output=True)
        (repo / 'README.md').write_text('# t\n')
        subprocess.run(['git', 'add', 'README.md'], cwd=repo, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'init'], cwd=repo, capture_output=True)
        subprocess.run(['git', 'push', '-u', 'origin', 'main'], cwd=repo, capture_output=True)
        return repo

    def _run(self, registry, monkeypatch, tmp_path, *args):
        monkeypatch.setattr('syncer.main.notify', lambda *_: None)
        monkeypatch.setattr('syncer.config.TOOL_CONFIG_PATH', tmp_path / 'absent.toml')
        monkeypatch.setattr('syncer.tracking.STATE_DIR', tmp_path / 'state')
        return runner.invoke(app, [*args, '-c', str(registry)])

    def test_a_healthy_run_exits_zero(self, tmp_path, monkeypatch):
        repo = self._healthy_repo(tmp_path)
        registry = self._registry(tmp_path, [{'name': 'api', 'path': str(repo)}])
        assert self._run(registry, monkeypatch, tmp_path, 'check').exit_code == 0

    def test_an_unreadable_repo_exits_one(self, tmp_path, monkeypatch):
        repo = self._healthy_repo(tmp_path)
        shutil.rmtree(tmp_path / 'api.git')  # origin gone, so nothing can be verified
        registry = self._registry(tmp_path, [{'name': 'api', 'path': str(repo)}])
        assert self._run(registry, monkeypatch, tmp_path, 'check').exit_code == 1

    def test_a_repo_merely_needing_a_push_still_exits_zero(self, tmp_path, monkeypatch):
        """WARNING stays 0: 'ahead' is the normal state of a machine somebody works on, and an
        exit code that is non-zero every day is one nobody can automate against."""
        repo = self._healthy_repo(tmp_path)
        subprocess.run(['git', 'commit', '--allow-empty', '-m', 'local'], cwd=repo, capture_output=True)
        registry = self._registry(tmp_path, [{'name': 'api', 'path': str(repo)}])
        assert self._run(registry, monkeypatch, tmp_path, 'check').exit_code == 0

    def test_json_is_parseable_and_carries_the_failure_reason(self, tmp_path, monkeypatch):
        repo = self._healthy_repo(tmp_path)
        shutil.rmtree(tmp_path / 'api.git')
        registry = self._registry(tmp_path, [{'name': 'api', 'path': str(repo)}])
        result = self._run(registry, monkeypatch, tmp_path, 'check', '--json')
        payload = json.loads(result.stdout)  # nothing else may be on stdout
        assert payload['summary']['failed'] == 1
        assert payload['repos'][0]['status'] == 'unverified'
        assert payload['failures'][0]['stderr']

    def test_branches_json_carries_per_branch_state(self, tmp_path, monkeypatch):
        repo = self._healthy_repo(tmp_path)
        registry = self._registry(tmp_path, [{'name': 'api', 'path': str(repo)}])
        result = self._run(registry, monkeypatch, tmp_path, 'check', '--per-branch', '--json')
        payload = json.loads(result.stdout)
        assert payload['repos'][0]['branches'][0]['branch'] == 'main'
        assert payload['repos'][0]['branches'][0]['state'] == 'synced'


class TestCloneFailureReachesTheScreen:
    """End to end through the real CLI, because every layer already held the reason and the
    user still saw a bare 'clone failed': git's stderr was dropped in Repo.clone, the detail
    slot was hard-coded to None at the call site, and nothing asserted the rendered output.
    A unit test on any single layer would have passed throughout.

    Deliberately a nonexistent local path, not an unreachable host — no DNS, no network, and
    the same code path.
    """

    def _run(self, tmp_path, monkeypatch, clone_url):
        registry = tmp_path / 'work-repos.json'
        registry.write_text(
            json.dumps(
                {
                    'owner': 'someone',
                    'host': 'https://github.com',
                    'search_paths': [],
                    'repos': [{'name': 'ghost', 'path': str(tmp_path / 'ghost'), 'clone_url': clone_url}],
                }
            )
        )
        monkeypatch.setattr('syncer.main.notify', lambda *_: None)
        monkeypatch.setattr('syncer.config.TOOL_CONFIG_PATH', tmp_path / 'absent.toml')
        monkeypatch.setattr('syncer.tracking.STATE_DIR', tmp_path / 'state')
        return runner.invoke(app, ['apply', '-c', str(registry)])

    def test_the_url_and_gits_reason_are_both_printed(self, tmp_path, monkeypatch):
        bad_url = str(tmp_path / 'no-such-repo.git')
        result = self._run(tmp_path, monkeypatch, bad_url)
        assert 'clone failed' in result.stdout
        # The URL, because a wrong url_template or an empty registry owner is a likely cause
        # and git's message alone never names the setting that produced it. Contiguous in the
        # output is part of the assertion: Rich hard-wraps at 80 columns without soft_wrap,
        # which would break the URL mid-path and defeat a copy-paste.
        assert bad_url in result.stdout
        # git's own words, not our summary of them.
        assert 'fatal:' in result.stdout

    def test_a_successful_clone_says_where_it_landed(self, tmp_path, monkeypatch):
        source = tmp_path / 'source'
        subprocess.run(['git', 'init', str(source)], capture_output=True)
        subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=source, capture_output=True)
        subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=source, capture_output=True)
        (source / 'README.md').write_text('# src\n')
        subprocess.run(['git', 'add', '.'], cwd=source, capture_output=True)
        subprocess.run(['git', 'commit', '-m', 'init'], cwd=source, capture_output=True)

        result = self._run(tmp_path, monkeypatch, str(source))
        assert 'cloned' in result.stdout
        assert 'clone failed' not in result.stdout
        assert (tmp_path / 'ghost' / '.git').is_dir()
