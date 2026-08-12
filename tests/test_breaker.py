from syncer.breaker import FLAKY_THRESHOLD
from syncer.breaker import HostBreaker
from syncer.diagnose import Cause
from syncer.repos import TIMEOUT_RETURNCODE
from syncer.repos import GitFailure

GITHUB_HTTPS = 'https://github.com/datapointchris/syncer'
GITHUB_SSH = 'git@github.com:datapointchris/syncer.git'


def _failure(stderr: str, returncode: int = 128) -> GitFailure:
    return GitFailure(argv=('fetch',), returncode=returncode, stderr=stderr)


AUTH = _failure('fatal: could not read Username for https://github.com: terminal prompts disabled')
HOST_KEY = _failure('Host key verification failed.\nfatal: Could not read from remote repository.')
DNS = _failure('fatal: unable to access: Could not resolve host: github.com')
NETWORK = _failure('fatal: unable to access: Failed to connect to github.com port 443: Connection refused')
NOT_FOUND = _failure('ERROR: Repository not found.')
TIMEOUT = _failure('timed out after 120s', returncode=TIMEOUT_RETURNCODE)
UNKNOWN = _failure('fatal: something nobody has written a pattern for')


class TestAHostWideCauseTripsOnTheFirstFailure:
    """A rejected credential, an unverified host key and a name that does not resolve are facts
    about the host. The next repo cannot get a different answer, so asking again only costs the
    credential-helper storm the breaker exists to stop."""

    def test_auth_trips_immediately(self):
        breaker = HostBreaker()
        breaker.record_failure(GITHUB_HTTPS, AUTH)
        trip = breaker.trip_for(GITHUB_HTTPS)
        assert trip is not None
        assert trip.host == 'github.com'
        assert trip.cause is Cause.AUTH

    def test_host_key_trips_immediately(self):
        breaker = HostBreaker()
        breaker.record_failure(GITHUB_SSH, HOST_KEY)
        assert breaker.trip_for(GITHUB_SSH) is not None

    def test_dns_trips_immediately(self):
        breaker = HostBreaker()
        breaker.record_failure(GITHUB_HTTPS, DNS)
        assert breaker.trip_for(GITHUB_HTTPS) is not None

    def test_a_clean_breaker_trips_on_nothing(self):
        assert HostBreaker().trip_for(GITHUB_HTTPS) is None


class TestAFlakyCauseHasToRepeat:
    """A refused connection can be one machine briefly out, and a timeout is routinely one
    legitimately enormous repo. Tripping on the first would let a single slow clone cancel a run
    that was otherwise working."""

    def test_one_network_failure_does_not_trip(self):
        breaker = HostBreaker()
        breaker.record_failure(GITHUB_HTTPS, NETWORK)
        assert breaker.trip_for(GITHUB_HTTPS) is None

    def test_the_threshold_trips(self):
        breaker = HostBreaker()
        for _ in range(FLAKY_THRESHOLD):
            breaker.record_failure(GITHUB_HTTPS, NETWORK)
        trip = breaker.trip_for(GITHUB_HTTPS)
        assert trip is not None
        assert trip.cause is Cause.NETWORK

    def test_timeouts_accumulate_the_same_way(self):
        breaker = HostBreaker(flaky_threshold=2)
        breaker.record_failure(GITHUB_HTTPS, TIMEOUT)
        assert breaker.trip_for(GITHUB_HTTPS) is None
        breaker.record_failure(GITHUB_HTTPS, TIMEOUT)
        assert breaker.trip_for(GITHUB_HTTPS) is not None

    def test_different_flaky_causes_do_not_pool(self):
        """Two network failures and a timeout is not three of anything. Pooling them would trip on
        a host that had one bad moment and one big repo."""
        breaker = HostBreaker(flaky_threshold=3)
        breaker.record_failure(GITHUB_HTTPS, NETWORK)
        breaker.record_failure(GITHUB_HTTPS, NETWORK)
        breaker.record_failure(GITHUB_HTTPS, TIMEOUT)
        assert breaker.trip_for(GITHUB_HTTPS) is None


class TestWhatNeverTrips:
    def test_a_missing_repo_says_nothing_about_the_host(self):
        """`repository not found` is what a private repo you have no access to reports, and it is
        the one failure that is genuinely about the repo rather than the machine."""
        breaker = HostBreaker()
        for _ in range(FLAKY_THRESHOLD + 2):
            breaker.record_failure(GITHUB_HTTPS, NOT_FOUND)
        assert breaker.trip_for(GITHUB_HTTPS) is None

    def test_an_undiagnosed_failure_is_not_recorded(self):
        """diagnose refuses to guess a cause, so a breaker that tripped on 'something went wrong'
        would skip repos over a message no one has read."""
        breaker = HostBreaker(flaky_threshold=1)
        for _ in range(5):
            breaker.record_failure(GITHUB_HTTPS, UNKNOWN)
        assert breaker.trip_for(GITHUB_HTTPS) is None

    def test_a_local_path_has_no_host_to_close(self):
        breaker = HostBreaker()
        breaker.record_failure('/srv/git/thing.git', AUTH)
        assert breaker.trip_for('/srv/git/thing.git') is None
        assert breaker.trip_for('~/mirrors/thing.git') is None


class TestASuccessImmunisesTheHost:
    """Whatever failed after a host answered, it was not the host being unreachable."""

    def test_a_success_before_the_failure_prevents_the_trip(self):
        breaker = HostBreaker()
        breaker.record_success(GITHUB_HTTPS)
        breaker.record_failure(GITHUB_HTTPS, AUTH)
        assert breaker.trip_for(GITHUB_HTTPS) is None

    def test_a_success_after_the_failure_reopens_it(self):
        breaker = HostBreaker()
        breaker.record_failure(GITHUB_HTTPS, AUTH)
        assert breaker.trip_for(GITHUB_HTTPS) is not None
        breaker.record_success(GITHUB_HTTPS)
        assert breaker.trip_for(GITHUB_HTTPS) is None


class TestSshAndHttpsAreSeparateCredentials:
    """A loaded ssh key and an expired https token live on one host every day. Closing github.com
    because one of them failed would skip every repo reaching it by the other."""

    def test_an_https_failure_does_not_close_ssh(self):
        breaker = HostBreaker()
        breaker.record_failure(GITHUB_HTTPS, AUTH)
        assert breaker.trip_for(GITHUB_SSH) is None

    def test_an_ssh_failure_does_not_close_https(self):
        breaker = HostBreaker()
        breaker.record_failure(GITHUB_SSH, HOST_KEY)
        assert breaker.trip_for(GITHUB_HTTPS) is None

    def test_the_same_transport_spelled_differently_is_the_same_key(self):
        """scp-style and ssh:// reach the same host through the same key, so one closing must
        close the other — normalize_remote_url already folds them for the failure summary."""
        breaker = HostBreaker()
        breaker.record_failure('git@github.com:datapointchris/a.git', HOST_KEY)
        assert breaker.trip_for('ssh://git@github.com/datapointchris/b.git') is not None

    def test_a_different_host_is_untouched(self):
        breaker = HostBreaker()
        breaker.record_failure(GITHUB_HTTPS, AUTH)
        assert breaker.trip_for('https://bitbucket.example.com/scm/team/thing.git') is None


class TestTripSummary:
    def test_names_the_host_and_the_cause(self):
        breaker = HostBreaker()
        breaker.record_failure(GITHUB_SSH, HOST_KEY)
        trip = breaker.trip_for(GITHUB_SSH)
        assert trip is not None
        assert trip.summary == 'github.com (host key)'
