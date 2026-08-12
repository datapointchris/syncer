import subprocess
from pathlib import Path
from unittest.mock import patch

import pytest

from syncer.output import ALL_ICONS
from syncer.output import ICON_ERR
from syncer.output import ICON_OK
from syncer.output import _display_width
from syncer.output import _status_line
from syncer.repos import TIMEOUT_RETURNCODE
from syncer.repos import Repo
from syncer.repos import _noninteractive_env
from syncer.repos import find_repo_in_search_paths
from syncer.repos import find_untracked_repos
from syncer.repos import normalize_remote_url
from syncer.repos import origin_mismatch
from syncer.repos import run_command


def _git(path: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)


def _rev(path: Path, ref: str) -> str:
    """Resolve `ref`, or '' when it does not exist — so a setup that never built it compares
    unequal to a real object rather than blowing up somewhere less legible."""
    result = subprocess.run(['git', 'rev-parse', ref], cwd=path, capture_output=True, text=True)
    return result.stdout.strip() if result.returncode == 0 else ''


@pytest.fixture
def git_repo(tmp_path):
    """Create a real git repo for testing."""
    repo_path = tmp_path / 'test-repo'
    repo_path.mkdir()
    subprocess.run(['git', 'init'], cwd=repo_path, capture_output=True)
    subprocess.run(['git', 'config', 'user.email', 'test@test.com'], cwd=repo_path, capture_output=True)
    subprocess.run(['git', 'config', 'user.name', 'Test'], cwd=repo_path, capture_output=True)
    # Create initial commit so HEAD exists
    (repo_path / 'README.md').write_text('# Test\n')
    subprocess.run(['git', 'add', '.'], cwd=repo_path, capture_output=True)
    subprocess.run(['git', 'commit', '-m', 'init'], cwd=repo_path, capture_output=True)
    return repo_path


@pytest.fixture
def git_repo_with_remote(tmp_path):
    """Create a git repo cloned from a bare remote."""
    bare = tmp_path / 'remote' / 'test-repo.git'
    bare.mkdir(parents=True)
    subprocess.run(['git', 'init', '--bare', str(bare)], capture_output=True)

    repo_path = tmp_path / 'test-repo'
    subprocess.run(['git', 'clone', str(bare), str(repo_path)], capture_output=True)
    _git(repo_path, 'config', 'user.email', 'test@test.com')
    _git(repo_path, 'config', 'user.name', 'Test')
    (repo_path / 'README.md').write_text('# Test\n')
    _git(repo_path, 'add', '.')
    _git(repo_path, 'commit', '-m', 'init')
    _git(repo_path, 'push')
    return repo_path


def _make_repo(path: Path, **kwargs) -> Repo:
    return Repo(name='test-repo', path=path, owner='user', host='https://github.com', **kwargs)


class TestDisplayWidth:
    def test_plain_text(self):
        assert _display_width('hello') == 5

    def test_icon_counts_as_two(self):
        assert _display_width(ICON_OK) == 2

    def test_mixed_text_and_icon(self):
        text = f'{ICON_OK}  syncer '
        assert _display_width(text) == 2 + len('  syncer ')

    def test_all_icons_are_double_width(self):
        for icon in ALL_ICONS:
            assert _display_width(icon) == 2


class TestStatusLine:
    def test_without_branch(self):
        line = _status_line(ICON_OK, 'myrepo', 'synced', 'green')
        assert 'myrepo' in line
        assert 'synced' in line
        assert '[green]' in line
        assert '_' in line

    def test_with_branch(self):
        line = _status_line(ICON_OK, 'myrepo', 'synced', 'green', branch='main')
        assert 'myrepo' in line
        assert 'synced' in line
        assert '(main)' in line
        assert '[blue]' in line

    def test_padding_minimum_one(self):
        line = _status_line(ICON_OK, 'a' * 200, 'synced', 'green')
        assert '_' in line

    def test_no_branch_vs_branch_alignment(self):
        # Both lines should target the same LINE_WIDTH
        no_branch = _status_line(ICON_ERR, 'repo', 'no remote', 'red')
        with_branch = _status_line(ICON_OK, 'repo', 'synced', 'green', branch='main')
        # Both should contain padding
        assert '_' in no_branch
        assert '_' in with_branch


class TestRepo:
    def test_init(self):
        repo = Repo(name='myrepo', path=Path('/tmp/myrepo'), owner='user', host='https://github.com')
        assert repo.name == 'myrepo'
        assert repo.url == 'https://github.com/user/myrepo'

    def test_exists(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert repo.exists is True

    def test_not_exists(self, tmp_path):
        repo = _make_repo(tmp_path / 'nope')
        assert repo.exists is False

    def test_is_git_repo(self, git_repo):
        repo = _make_repo(git_repo)
        assert repo.is_git_repo is True

    def test_is_not_git_repo(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert repo.is_git_repo is False

    def test_has_no_remote(self, git_repo):
        repo = _make_repo(git_repo)
        assert repo.has_remote is False

    def test_has_remote(self, git_repo_with_remote):
        repo = _make_repo(git_repo_with_remote)
        assert repo.has_remote is True

    def test_current_branch(self, git_repo):
        repo = _make_repo(git_repo)
        branch = repo.current_branch
        assert branch in ('main', 'master')

    def test_uncommitted_changes_empty(self, git_repo):
        repo = _make_repo(git_repo)
        assert repo.uncommitted_changes == []

    def test_uncommitted_changes_detected(self, git_repo):
        (git_repo / 'newfile.txt').write_text('hello')
        repo = _make_repo(git_repo)
        assert len(repo.uncommitted_changes) > 0

    def test_stash_count_zero(self, git_repo):
        repo = _make_repo(git_repo)
        assert repo.stash_count == 0

    def test_stash_count_nonzero(self, git_repo):
        (git_repo / 'wip.txt').write_text('work\n')
        _git(git_repo, 'add', '.')
        _git(git_repo, 'stash', 'push', '-m', 'save')
        repo = _make_repo(git_repo)
        assert repo.stash_count == 1

    def test_default_branch_detection(self, git_repo):
        repo = _make_repo(git_repo)
        branch = repo.default_branch
        assert branch in ('main', 'master')

    def test_unpushed_commits_with_remote(self, git_repo_with_remote):
        (git_repo_with_remote / 'new.txt').write_text('new\n')
        _git(git_repo_with_remote, 'add', '.')
        _git(git_repo_with_remote, 'commit', '-m', 'new commit')
        repo = _make_repo(git_repo_with_remote)
        assert repo.unpushed_commits == 1

    def test_behind_remote(self, tmp_path):
        """Create a repo that is behind its remote."""
        bare = tmp_path / 'remote' / 'repo.git'
        bare.mkdir(parents=True)
        subprocess.run(['git', 'init', '--bare', str(bare)], capture_output=True)

        repo_path = tmp_path / 'repo'
        subprocess.run(['git', 'clone', str(bare), str(repo_path)], capture_output=True)
        _git(repo_path, 'config', 'user.email', 'test@test.com')
        _git(repo_path, 'config', 'user.name', 'Test')
        (repo_path / 'README.md').write_text('# Test\n')
        _git(repo_path, 'add', '.')
        _git(repo_path, 'commit', '-m', 'init')
        _git(repo_path, 'push')

        # Push from a second clone to get the repo behind
        second = tmp_path / 'second'
        subprocess.run(['git', 'clone', str(bare), str(second)], capture_output=True)
        _git(second, 'config', 'user.email', 'test@test.com')
        _git(second, 'config', 'user.name', 'Test')
        (second / 'update.txt').write_text('update\n')
        _git(second, 'add', '.')
        _git(second, 'commit', '-m', 'remote update')
        _git(second, 'push')

        _git(repo_path, 'fetch')
        repo = _make_repo(repo_path)
        assert repo.behind_remote == 1


class TestGitStats:
    def test_total_commits(self, git_repo):
        repo = _make_repo(git_repo)
        assert repo.total_commits == 1

    def test_total_commits_multiple(self, git_repo):
        (git_repo / 'file2.txt').write_text('content\n')
        _git(git_repo, 'add', '.')
        _git(git_repo, 'commit', '-m', 'second')
        repo = _make_repo(git_repo)
        assert repo.total_commits == 2

    def test_last_commit_date(self, git_repo):
        repo = _make_repo(git_repo)
        date = repo.last_commit_date
        assert date is not None
        # ISO 8601 format includes timezone offset
        assert 'T' in date

    def test_first_commit_date(self, git_repo):
        repo = _make_repo(git_repo)
        date = repo.first_commit_date
        assert date is not None
        assert 'T' in date

    def test_first_and_last_differ_with_multiple_commits(self, git_repo):
        import time

        time.sleep(1)  # ensure different timestamps
        (git_repo / 'file2.txt').write_text('content\n')
        _git(git_repo, 'add', '.')
        _git(git_repo, 'commit', '-m', 'second')
        repo = _make_repo(git_repo)
        assert repo.first_commit_date != repo.last_commit_date

    def test_stats_on_non_git_dir(self, tmp_path):
        repo = _make_repo(tmp_path)
        assert repo.total_commits == 0
        assert repo.last_commit_date is None
        assert repo.first_commit_date is None


class TestDefaultBranchStaleRef:
    def test_stale_origin_head_falls_back_to_local(self, git_repo_with_remote):
        """If origin/HEAD points to a deleted branch, fall back to local branch detection."""
        # Point origin/HEAD to a non-existent branch
        _git(git_repo_with_remote, 'symbolic-ref', 'refs/remotes/origin/HEAD', 'refs/remotes/origin/nonexistent')
        repo = _make_repo(git_repo_with_remote)
        branch = repo.default_branch
        # Should not return 'nonexistent', should fall back
        assert branch != 'nonexistent'
        assert branch in ('main', 'master')

    def test_valid_origin_head_is_trusted(self, git_repo_with_remote):
        """If origin/HEAD points to a valid branch, use it."""
        repo = _make_repo(git_repo_with_remote)
        branch = repo.default_branch
        assert branch in ('main', 'master')

    def test_no_remote_falls_back_to_local(self, git_repo):
        """Repo with no remote should still detect default branch from local refs."""
        repo = _make_repo(git_repo)
        assert repo.default_branch in ('main', 'master')


class TestIsFork:
    def test_is_fork_true(self, git_repo):
        repo = _make_repo(git_repo)
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout='true\n', stderr='')
        with patch('subprocess.run', return_value=result):
            assert repo.is_fork is True

    def test_is_fork_false(self, git_repo):
        repo = _make_repo(git_repo)
        result = subprocess.CompletedProcess(args=[], returncode=0, stdout='false\n', stderr='')
        with patch('subprocess.run', return_value=result):
            assert repo.is_fork is False

    def test_is_fork_gh_fails(self, git_repo):
        repo = _make_repo(git_repo)
        result = subprocess.CompletedProcess(args=[], returncode=1, stdout='', stderr='error')
        with patch('subprocess.run', return_value=result):
            assert repo.is_fork is False

    def test_non_github_host_never_invokes_gh(self, git_repo):
        """gh only speaks to GitHub, so asking it about a Bitbucket repo is a subprocess per
        repo that always answers 'no'."""
        repo = _make_repo(git_repo, url='git@bitbucket.org:myworkspace/payments.git')
        assert repo.is_github is False
        with patch('syncer.repos.subprocess.run') as run:
            assert repo.is_fork is False
        run.assert_not_called()

    def test_github_ssh_url_is_recognised(self, git_repo):
        assert _make_repo(git_repo, url='git@github.com:datapointchris/syncer.git').is_github is True


class TestNonInteractiveExecution:
    """Git prompts on /dev/tty, which capture_output does not redirect, so a credential or
    host-key prompt would block a worker thread indefinitely with nothing on screen."""

    def test_disables_git_terminal_prompting(self, monkeypatch):
        monkeypatch.delenv('GIT_TERMINAL_PROMPT', raising=False)
        assert _noninteractive_env()['GIT_TERMINAL_PROMPT'] == '0'

    def test_adds_ssh_batch_mode(self, monkeypatch):
        monkeypatch.delenv('GIT_SSH_COMMAND', raising=False)
        assert _noninteractive_env()['GIT_SSH_COMMAND'] == 'ssh -o BatchMode=yes'

    def test_preserves_a_configured_ssh_command(self, monkeypatch):
        monkeypatch.setenv('GIT_SSH_COMMAND', 'ssh -i /keys/work')
        assert _noninteractive_env()['GIT_SSH_COMMAND'] == 'ssh -i /keys/work -o BatchMode=yes'

    def test_environment_reaches_the_subprocess(self):
        result = run_command(['sh', '-c', 'echo "$GIT_TERMINAL_PROMPT"'], timeout=10)
        assert result.stdout.strip() == '0'

    def test_timeout_becomes_a_non_zero_result_not_an_exception(self):
        result = run_command(['sleep', '5'], timeout=1)
        assert result.returncode == TIMEOUT_RETURNCODE
        assert 'timed out' in result.stderr

    def test_a_timed_out_git_call_reads_as_failure(self, git_repo):
        repo = _make_repo(git_repo, timeout=1)
        with patch('syncer.repos.subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='git', timeout=1)):
            failure = repo.fetch_prune()
            assert failure is not None
            assert failure.timed_out
            assert repo.local_branches() == []
            assert repo.default_branch is None


class TestFailureRecording:
    """Every accessor used to turn a non-zero exit into a benign value, so a broken git looked
    like a healthy repo. Recording is the default; probe=True is the argued-for exception."""

    def test_a_failed_call_is_recorded(self, git_repo):
        repo = _make_repo(git_repo)
        repo._git('rev-parse', '--verify', 'refs/heads/nope')
        assert len(repo.failures) == 1
        assert repo.failures[0].returncode != 0
        assert repo.failures[0].command.startswith('git rev-parse')

    def test_a_probe_is_not_recorded(self, git_repo):
        repo = _make_repo(git_repo)
        repo._git('rev-parse', '--verify', 'refs/heads/nope', probe=True)
        assert repo.failures == []

    def test_walking_the_default_branch_fallbacks_is_not_a_failure(self, git_repo):
        """default_branch probes several refs by design; recording those would bury the real
        failures under noise on every repo whose origin/HEAD was never set."""
        repo = _make_repo(git_repo)
        # Not asserting the name: `git init` honours init.defaultBranch, so this is 'main' on a
        # configured machine and 'master' on a stock CI runner.
        assert repo.default_branch is not None
        assert repo.failures == []

    def test_a_successful_call_records_nothing(self, git_repo):
        repo = _make_repo(git_repo)
        repo.local_branches()
        assert repo.failures == []

    def test_a_rejected_tag_fetch_carries_its_reason(self, tmp_path):
        """A recorded failure with empty stderr is the undiagnosable state GitFailure exists to
        prevent, and `--quiet` produced exactly that: a tag-clobber fetch exits 1 with zero bytes
        on stderr, so the repo reported `fetch failed` with no detail and nothing to act on.

        Built with real git rather than a mock, because the whole finding is about which stream
        git writes to under which flags — a mocked CompletedProcess would assert our own guess.
        """
        # --initial-branch, because the bare's HEAD is what a later clone checks out: left to
        # init.defaultBranch it said `master` on CI while the branch pushed below was `main`, so
        # the clone checked out nothing and had no HEAD for its tag to point at.
        bare = tmp_path / 'remote.git'
        subprocess.run(['git', 'init', '--bare', '--initial-branch=main', str(bare)], capture_output=True)

        # Every push names its branch and every checkout creates it. A bare `git push` onto an
        # empty remote only works where push.autoSetupRemote is configured, so it pushed nothing
        # on CI and the whole divergence this test is about was silently never built.
        upstream = tmp_path / 'upstream'
        subprocess.run(['git', 'clone', str(bare), str(upstream)], capture_output=True)
        _git(upstream, 'config', 'user.email', 'test@test.com')
        _git(upstream, 'config', 'user.name', 'Test')
        _git(upstream, 'checkout', '-b', 'main')
        (upstream / 'README.md').write_text('one\n')
        _git(upstream, 'add', '.')
        _git(upstream, 'commit', '-m', 'one')
        _git(upstream, 'push', 'origin', 'main')

        local = tmp_path / 'local'
        subprocess.run(['git', 'clone', str(bare), str(local)], capture_output=True)
        # Set here rather than inherited, so the test states the condition it is about. These are
        # what make a tag a pruned refspec instead of an auto-follow, and only an explicit refspec
        # rejects a changed tag — auto-follow skips one that already exists locally and exits 0.
        # They are on for every repo on the machine this was found on (~/.config/git/common.gitconfig)
        # and absent on CI, which is why the run there fetched clean.
        _git(local, 'config', 'fetch.prune', 'true')
        _git(local, 'config', 'fetch.pruneTags', 'true')

        # The tag now means a different commit on each side, which is what git refuses to resolve.
        (upstream / 'README.md').write_text('two\n')
        _git(upstream, 'add', 'README.md')
        _git(upstream, 'commit', '-m', 'two')
        _git(upstream, 'push', 'origin', 'main')
        _git(upstream, 'tag', 'v1.0.0')
        _git(upstream, 'push', 'origin', 'v1.0.0')
        _git(local, 'tag', 'v1.0.0')

        # _git swallows a non-zero exit, so an unbuilt divergence would otherwise reach the
        # assertions below as a clean fetch and read as the bug being fixed. Both sides must
        # resolve: one missing tag is also a broken setup, and it also produces a clean fetch.
        local_tag, upstream_tag = _rev(local, 'v1.0.0'), _rev(upstream, 'v1.0.0')
        assert local_tag and upstream_tag
        assert local_tag != upstream_tag

        failure = _make_repo(local).fetch_prune()
        assert failure is not None
        assert 'would clobber existing tag' in failure.stderr
        assert 'v1.0.0' in failure.stderr


class TestUnknownIsNotClean:
    """Invariant 2 gates on this. A failing `git status` returning [] read as a clean tree —
    i.e. as permission to mutate — which is the wrong direction for a safety check."""

    def test_a_dirty_tree_is_dirty(self, git_repo):
        (git_repo / 'new.txt').write_text('x\n')
        assert _make_repo(git_repo).is_dirty is True

    def test_a_clean_tree_is_clean(self, git_repo):
        assert _make_repo(git_repo).is_dirty is False

    def test_an_unreadable_tree_counts_as_dirty(self, git_repo):
        repo = _make_repo(git_repo, timeout=1)
        with patch('syncer.repos.subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='git', timeout=1)):
            assert repo.is_dirty is True

    def test_no_remotes_is_distinct_from_cannot_ask(self, git_repo):
        repo = _make_repo(git_repo)
        assert repo.remotes() == []  # a repo you never pushed anywhere
        with patch('syncer.repos.subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='git', timeout=1)):
            assert repo.remotes() is None  # says nothing about remotes at all

    def test_unreadable_counts_are_none_not_zero(self, git_repo):
        """(0, 0) is read as SYNCED by _primary_from_counts, so it can never be the fallback."""
        repo = _make_repo(git_repo, timeout=1)
        with patch('syncer.repos.subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='git', timeout=1)):
            assert repo.ahead_behind('main', 'origin/main') is None


class TestClone:
    """A failed clone must carry git's own words. Auth, an unknown host key, DNS, a bad
    url_template and a timeout are otherwise one indistinguishable 'clone failed' line, and
    capture_output means git's message never reaches the terminal by itself."""

    def test_a_successful_clone_reports_no_error(self, tmp_path, git_repo):
        repo = _make_repo(tmp_path / 'dest', url=str(git_repo))
        ok, err = repo.clone()
        assert ok is True
        assert err == ''
        assert (tmp_path / 'dest' / '.git').is_dir()

    def test_a_failed_clone_returns_gits_stderr(self, tmp_path):
        repo = _make_repo(tmp_path / 'dest', url=str(tmp_path / 'no-such-repo.git'))
        ok, err = repo.clone()
        assert ok is False
        assert err  # the actual reason, not a bare False

    def test_a_clone_timeout_returns_its_reason(self, tmp_path):
        repo = _make_repo(tmp_path / 'dest', url='https://example.invalid/x.git', timeout=1)
        with patch('syncer.repos.subprocess.run', side_effect=subprocess.TimeoutExpired(cmd='git', timeout=5)):
            ok, err = repo.clone()
        assert ok is False
        assert 'timed out' in err

    def test_clone_scales_its_timeout_from_the_configured_one(self, tmp_path):
        """config.toml and the README both promise clones get 5x git_timeout; before this the
        clone used a hard-coded 600s and ignored the setting entirely."""
        repo = _make_repo(tmp_path / 'dest', url='https://example.invalid/x.git', timeout=30)
        with patch('syncer.repos.run_command') as run:
            run.return_value = subprocess.CompletedProcess([], returncode=0, stdout='', stderr='')
            repo.clone()
        assert run.call_args.kwargs['timeout'] == 150


class TestContainsBranch:
    """Integration has two proofs: ancestry, and patch equivalence for squash/cherry-pick
    integration, which rewrites commits so ancestry can never see them."""

    def _branch_with_commit(self, repo_path: Path, branch: str, filename: str) -> None:
        _git(repo_path, 'checkout', '-b', branch)
        (repo_path / filename).write_text(f'{filename}\n')
        _git(repo_path, 'add', '.')
        _git(repo_path, 'commit', '-m', f'work on {branch}')
        _git(repo_path, 'checkout', '-')

    def test_ancestry_proves_a_merged_branch(self, git_repo):
        self._branch_with_commit(git_repo, 'feature', 'a.txt')
        _git(git_repo, 'merge', '--no-ff', '-m', 'merge feature', 'feature')
        repo = _make_repo(git_repo)
        assert repo.is_merged_into('feature', 'master' if repo.default_branch == 'master' else 'main') is True
        assert repo.contains_branch('feature', repo.default_branch) is True

    def test_patch_equivalence_proves_a_squash_merged_branch(self, git_repo):
        self._branch_with_commit(git_repo, 'feature', 'a.txt')
        _git(git_repo, 'merge', '--squash', 'feature')
        _git(git_repo, 'commit', '-m', 'squash feature')
        repo = _make_repo(git_repo)
        default = repo.default_branch
        assert repo.is_merged_into('feature', default) is False  # a squash rewrites the commit
        assert repo.is_patch_applied_in('feature', default) is True
        assert repo.contains_branch('feature', default) is True

    def test_unintegrated_branch_is_not_contained(self, git_repo):
        self._branch_with_commit(git_repo, 'feature', 'a.txt')
        repo = _make_repo(git_repo)
        assert repo.contains_branch('feature', repo.default_branch) is False

    def test_missing_target_is_not_contained(self, git_repo):
        self._branch_with_commit(git_repo, 'feature', 'a.txt')
        repo = _make_repo(git_repo)
        assert repo.contains_branch('feature', 'no-such-branch') is False


class TestFindRepoInSearchPaths:
    def test_find_direct(self, tmp_path):
        repo_dir = tmp_path / 'code' / 'myrepo'
        repo_dir.mkdir(parents=True)
        (repo_dir / '.git').mkdir()

        result = find_repo_in_search_paths('myrepo', [tmp_path / 'code'])
        assert result == repo_dir

    def test_find_nested(self, tmp_path):
        repo_dir = tmp_path / 'code' / 'subdir' / 'myrepo'
        repo_dir.mkdir(parents=True)
        (repo_dir / '.git').mkdir()

        result = find_repo_in_search_paths('myrepo', [tmp_path / 'code'])
        assert result == repo_dir

    def test_not_found(self, tmp_path):
        search = tmp_path / 'code'
        search.mkdir()

        result = find_repo_in_search_paths('myrepo', [search])
        assert result is None

    def test_skip_nonexistent_search_path(self, tmp_path):
        result = find_repo_in_search_paths('myrepo', [tmp_path / 'nonexistent'])
        assert result is None

    def test_skip_claimed_path_direct(self, tmp_path):
        repo_dir = tmp_path / 'code' / 'myrepo'
        repo_dir.mkdir(parents=True)
        (repo_dir / '.git').mkdir()

        result = find_repo_in_search_paths('myrepo', [tmp_path / 'code'], claimed_paths={repo_dir})
        assert result is None

    def test_skip_claimed_path_nested(self, tmp_path):
        repo_dir = tmp_path / 'code' / 'subdir' / 'myrepo'
        repo_dir.mkdir(parents=True)
        (repo_dir / '.git').mkdir()

        result = find_repo_in_search_paths('myrepo', [tmp_path / 'code'], claimed_paths={repo_dir})
        assert result is None


class TestNormalizeRemoteUrl:
    """The same repo is reachable over https, scp-style SSH and ssh:// with a port. Comparing
    raw strings would flag every SSH clone against an https registry — noise that gets the
    check ignored, and an ignored check is how a wrong origin survives for months."""

    def test_equivalent_forms_compare_equal(self):
        forms = [
            'https://github.com/khuedoan/homelab',
            'https://github.com/khuedoan/homelab.git',
            'https://github.com/khuedoan/homelab/',
            'git@github.com:khuedoan/homelab.git',
            'ssh://git@github.com/khuedoan/homelab.git',
        ]
        assert len({normalize_remote_url(form) for form in forms}) == 1

    def test_bitbucket_data_center_port_is_not_part_of_the_path(self):
        assert normalize_remote_url('ssh://git@bitbucket.corp:7999/proj/payments.git') == 'bitbucket.corp/proj/payments'

    def test_scp_style_colon_is_a_path_separator_not_a_port(self):
        assert normalize_remote_url('git@bitbucket.org:myworkspace/payments.git') == 'bitbucket.org/myworkspace/payments'

    def test_case_is_ignored(self):
        assert normalize_remote_url('https://GitHub.com/Owner/Repo') == normalize_remote_url('https://github.com/owner/repo')

    def test_different_owners_do_not_compare_equal(self):
        """The real incident: the same repo name under a different owner."""
        assert normalize_remote_url('https://github.com/datapointchris/homelab') != normalize_remote_url(
            'https://github.com/khuedoan/homelab'
        )


class TestOriginMismatch:
    """~/code/refs/homelab pointed at datapointchris/homelab while the exemplar registry
    declared khuedoan/homelab — undetected for 3.5 months, because nothing compared them.
    `gh repo clone <bare-name>` resolves to the authenticated user, so any reference repo that
    also exists under your own account silently gets your fork as origin."""

    def _repo_with_origin(self, tmp_path, origin, expected):
        path = tmp_path / 'clone'
        subprocess.run(['git', 'init', str(path)], capture_output=True)
        subprocess.run(['git', 'remote', 'add', 'origin', origin], cwd=path, capture_output=True)
        return Repo(name='homelab', path=path, owner='khuedoan', host='https://github.com', url=expected)

    def test_the_real_incident_is_flagged(self, tmp_path):
        repo = self._repo_with_origin(
            tmp_path,
            origin='https://github.com/datapointchris/homelab',
            expected='https://github.com/khuedoan/homelab',
        )
        assert origin_mismatch(repo) == 'https://github.com/datapointchris/homelab'

    def test_a_matching_origin_is_silent(self, tmp_path):
        repo = self._repo_with_origin(
            tmp_path,
            origin='https://github.com/khuedoan/homelab',
            expected='https://github.com/khuedoan/homelab',
        )
        assert origin_mismatch(repo) is None

    def test_an_ssh_clone_of_an_https_registry_entry_is_not_a_mismatch(self, tmp_path):
        repo = self._repo_with_origin(
            tmp_path,
            origin='git@github.com:khuedoan/homelab.git',
            expected='https://github.com/khuedoan/homelab',
        )
        assert origin_mismatch(repo) is None

    def test_a_repo_with_no_remote_is_not_a_mismatch(self, tmp_path):
        """`no remote` is already its own lifecycle status; reporting it twice is noise."""
        path = tmp_path / 'bare-init'
        subprocess.run(['git', 'init', str(path)], capture_output=True)
        repo = Repo(name='x', path=path, owner='me', host='https://github.com')
        assert origin_mismatch(repo) is None


class TestFindUntrackedRepos:
    """The untracked scan is the safety net that catches repos falling out of a
    registry. Scanning only direct children silently missed twenty of them."""

    def _repo(self, path):
        path.mkdir(parents=True, exist_ok=True)
        (path / '.git').mkdir(exist_ok=True)
        return path

    def test_finds_repos_nested_below_the_search_path(self, tmp_path):
        # ~/code/refs/<name> and ~/code/python-projects/<name> are the real shapes.
        nested = self._repo(tmp_path / 'refs' / 'bubbletea')
        found = find_untracked_repos(tmp_path, known_paths=set())
        assert nested in found

    def test_registered_repos_are_not_reported(self, tmp_path):
        known = self._repo(tmp_path / 'tools' / 'forge')
        found = find_untracked_repos(tmp_path, known_paths={known.resolve()})
        assert found == []

    def test_excluded_subtree_is_skipped_entirely(self, tmp_path):
        self._repo(tmp_path / 'refs' / 'fastapi')
        kept = self._repo(tmp_path / 'tools' / 'syncer')
        found = find_untracked_repos(tmp_path, known_paths=set(), excluded={(tmp_path / 'refs').resolve()})
        assert found == [kept]

    def test_does_not_descend_into_a_repo(self, tmp_path):
        outer = self._repo(tmp_path / 'outer')
        self._repo(outer / 'vendored')
        found = find_untracked_repos(tmp_path, known_paths=set())
        assert found == [outer]

    def test_hidden_directories_are_skipped(self, tmp_path):
        self._repo(tmp_path / '.cache' / 'thing')
        assert find_untracked_repos(tmp_path, known_paths=set()) == []
