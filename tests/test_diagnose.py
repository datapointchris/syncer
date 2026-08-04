import pytest

from syncer.diagnose import Cause
from syncer.diagnose import classify_failure
from syncer.diagnose import group_failures
from syncer.diagnose import hint_lines
from syncer.diagnose import remote_host
from syncer.repos import TIMEOUT_RETURNCODE
from syncer.repos import GitFailure


def _failure(stderr: str, returncode: int = 128) -> GitFailure:
    return GitFailure(argv=('fetch', '--quiet'), returncode=returncode, stderr=stderr)


# Real output, as git/ssh/curl actually emit it.
REAL_STDERR = [
    (
        Cause.HOST_KEY,
        'Host key verification failed.\nfatal: Could not read from remote repository.',
    ),
    (
        Cause.HOST_KEY,
        '@@@ WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED! @@@',
    ),
    (
        Cause.HOST_KEY,
        'No ED25519 host key is known for bitbucket.corp and you have requested strict checking.',
    ),
    (
        Cause.AUTH,
        'git@github.com: Permission denied (publickey).\nfatal: Could not read from remote repository.',
    ),
    (
        Cause.AUTH,
        "fatal: could not read Username for 'https://github.com': terminal prompts disabled",
    ),
    (
        Cause.AUTH,
        "remote: Invalid username or password.\nfatal: Authentication failed for 'https://git.corp/x.git'",
    ),
    (
        Cause.DNS,
        "fatal: unable to access 'https://git.example.invalid/x.git/': Could not resolve host: git.example.invalid",
    ),
    (
        Cause.DNS,
        'ssh: Could not resolve hostname bitbucket.corp: Name or service not known',
    ),
    (
        Cause.NETWORK,
        "fatal: unable to access 'https://git.corp/x.git/': Failed to connect to git.corp port 443: Connection refused",
    ),
    (
        Cause.NETWORK,
        'ssh: connect to host git.corp port 22: Network is unreachable',
    ),
    (
        Cause.NOT_FOUND,
        "remote: Repository not found.\nfatal: repository 'https://github.com/o/r/' not found",
    ),
    (
        Cause.NOT_FOUND,
        "fatal: '/srv/git/x.git' does not appear to be a git repository",
    ),
]


class TestClassifyFailure:
    @pytest.mark.parametrize(('expected', 'stderr'), REAL_STDERR)
    def test_real_stderr_is_classified(self, expected, stderr):
        assert classify_failure(_failure(stderr)) is expected

    def test_a_host_key_rejection_is_not_read_as_an_auth_problem(self):
        """Both lines appear together; matching AUTH first would send you to re-check a
        credential that was never the problem."""
        both = 'git@host: Permission denied (publickey).\nHost key verification failed.'
        assert classify_failure(_failure(both)) is Cause.HOST_KEY

    def test_a_timeout_is_recognised_by_its_returncode_not_its_text(self):
        failure = _failure('timed out after 120s', returncode=TIMEOUT_RETURNCODE)
        assert classify_failure(failure) is Cause.TIMEOUT

    def test_unrecognised_output_gets_no_cause(self):
        """Honesty rule 1: no fallback bucket. A confident wrong explanation sends someone to
        fix the wrong thing, which is worse than admitting we do not know."""
        assert classify_failure(_failure('error: something nobody has seen before')) is None

    def test_the_generic_line_alone_is_not_enough(self):
        """'Could not read from remote repository' accompanies every one of these causes, so
        matching on it would classify all of them as whichever pattern happened to list it."""
        assert classify_failure(_failure('fatal: Could not read from remote repository.')) is None


class TestHints:
    def test_gh_is_only_suggested_for_github(self):
        """The work box may be Bitbucket or an internal GitLab; telling it to run `gh auth
        login` is noise that teaches you to skip the hint."""
        github = ' '.join(hint_lines(Cause.AUTH, 'https://github.com/o/r'))
        corporate = ' '.join(hint_lines(Cause.AUTH, 'https://bitbucket.corp/scm/p/r.git'))
        assert 'gh auth login' in github
        assert 'gh auth login' not in corporate
        assert 'bitbucket.corp' in corporate

    def test_ssh_and_https_get_different_remedies(self):
        ssh = ' '.join(hint_lines(Cause.AUTH, 'git@git.corp:p/r.git'))
        https = ' '.join(hint_lines(Cause.AUTH, 'https://git.corp/p/r.git'))
        assert 'ssh-add' in ssh
        assert 'credential' in https

    def test_auth_and_host_key_always_explain_batch_mode(self):
        """Nothing in git's own output hints at it, so a credential that works by hand failing
        here is otherwise inexplicable."""
        for cause in (Cause.AUTH, Cause.HOST_KEY):
            assert any('non-interactively' in line for line in hint_lines(cause, 'git@h.corp:p/r'))

    def test_not_found_names_the_empty_owner_trap(self):
        text = ' '.join(hint_lines(Cause.NOT_FOUND, 'https://github.com//r'))
        assert 'owner' in text

    def test_an_unknown_cause_offers_nothing(self):
        assert hint_lines(None, 'https://github.com/o/r') == []


class TestRemoteHost:
    @pytest.mark.parametrize(
        ('url', 'expected'),
        [
            ('https://github.com/o/r', 'github.com'),
            ('git@github.com:o/r.git', 'github.com'),
            ('ssh://git@bitbucket.corp:7999/p/r.git', 'bitbucket.corp'),
            ('/srv/git/r.git', ''),
            ('./relative/r.git', ''),
            ('~/mirrors/r.git', ''),
        ],
    )
    def test_host_is_extracted_however_the_url_is_spelled(self, url, expected):
        assert remote_host(url) == expected


class TestGroupFailures:
    def test_one_cause_across_many_repos_is_one_group(self):
        """N repos behind one dead VPN produce N identical blobs; a wall of those is its own
        kind of unreadable."""
        stderr = 'ssh: connect to host git.corp port 22: Network is unreachable'
        groups = group_failures([(f'repo{i}', 'git@git.corp:p/r.git', _failure(stderr)) for i in range(5)])
        assert len(groups) == 1
        assert len(groups[0].repos) == 5
        assert groups[0].cause is Cause.NETWORK

    def test_different_causes_stay_separate(self):
        groups = group_failures(
            [
                ('a', 'git@git.corp:p/a.git', _failure('Host key verification failed.')),
                ('b', 'git@git.corp:p/b.git', _failure('git@git.corp: Permission denied (publickey).')),
            ]
        )
        assert {group.cause for group in groups} == {Cause.HOST_KEY, Cause.AUTH}

    def test_the_same_cause_on_different_hosts_stays_separate(self):
        """Two hosts down is two things to fix, and one hint cannot name both."""
        stderr = 'Could not resolve host: x'
        groups = group_failures(
            [
                ('a', 'https://one.corp/p/a.git', _failure(stderr)),
                ('b', 'https://two.corp/p/b.git', _failure(stderr)),
            ]
        )
        assert len(groups) == 2

    def test_raw_stderr_survives_grouping(self):
        """Honesty rule 3: the cause is a summary, and summaries lose things."""
        stderr = 'ssh: connect to host git.corp port 22: Network is unreachable'
        [group] = group_failures([('a', 'git@git.corp:p/a.git', _failure(stderr))])
        assert group.stderr == stderr

    def test_an_unclassified_failure_still_groups_and_shows_its_output(self):
        [group] = group_failures([('a', 'https://git.corp/p/a.git', _failure('something novel'))])
        assert group.cause is None
        assert group.hints == ()
        assert group.stderr == 'something novel'
