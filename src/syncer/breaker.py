"""Stop asking a host that has already said no. Pure logic: no git, no filesystem, no console.

A registry is mostly one host, so a dead credential is not N repo problems — it is one machine
problem discovered N times. Left unchecked that is what turned an expired token into the worst
run this tool produces: every repo attempted its own fetch, every fetch woke the credential
helper, and the machine spent minutes on work that could not succeed before printing anything.

So the first proof that a host is unreachable closes it for the rest of the run, and the repos
behind it are reported as not checked rather than attempted. The failure summary already says
what to fix once; this stops the run paying for it once per repo.

Two tiers, because the causes differ in what they prove:

- **A host-wide cause trips on the first failure.** A rejected key, an unverified host key and a
  name that does not resolve are all facts about the host, not about the repo that happened to
  discover them — the next repo cannot get a different answer.
- **A flaky cause needs `FLAKY_THRESHOLD` of them.** A refused connection can be one machine
  briefly out, and a timeout is routinely one legitimately enormous repo. Tripping on the first
  would let a single slow clone cancel a run that was working.

`NOT_FOUND` never trips, at either tier: a repo that is not there says nothing about the host,
and it is exactly what a private repo you have no access to reports.

The key is (host, ssh-or-https), not the host alone. The two reach the same machine through
completely different credentials — an ssh key that is loaded and an https token that expired live
on one host every day — so closing github.com because one of them failed would skip every repo
using the other. For the same reason a host that has answered *successfully* this run can never
trip: whatever failed after that, it was not the host being unreachable.
"""

from __future__ import annotations

import threading
from collections import Counter
from dataclasses import dataclass

from syncer.diagnose import Cause
from syncer.diagnose import classify_failure
from syncer.diagnose import remote_host
from syncer.repos import GitFailure

# Facts about the host: the next repo asking the same question gets the same answer.
HOST_WIDE_CAUSES = frozenset({Cause.AUTH, Cause.HOST_KEY, Cause.DNS})
# Facts that might be about one repo or one moment, so they have to repeat before they count.
FLAKY_CAUSES = frozenset({Cause.NETWORK, Cause.TIMEOUT})
FLAKY_THRESHOLD = 3


@dataclass(frozen=True)
class Trip:
    """Why a host was closed. Carried onto every repo skipped because of it, so the report can
    say which host and which cause rather than only that something was skipped."""

    host: str
    cause: Cause

    @property
    def summary(self) -> str:
        return f'{self.host} ({self.cause.value.replace("_", " ")})'


def _key(url: str) -> tuple[str, str] | None:
    """(host, transport) for a URL, or None when there is no host to draw a conclusion about."""
    host = remote_host(url)
    if not host:
        return None
    return host, 'https' if url.startswith(('http://', 'https://')) else 'ssh'


class HostBreaker:
    """Records what each host has said this run and answers whether it is worth asking again.

    Shared across worker threads, so every read and write takes the lock. The window it cannot
    close is the one already in flight: with `jobs` fetches running when the first failure lands,
    that many were always going to be attempted. Bounding the damage at `jobs` instead of at the
    size of the registry is the whole win.
    """

    def __init__(self, *, flaky_threshold: int = FLAKY_THRESHOLD) -> None:
        self._lock = threading.Lock()
        self._failures: dict[tuple[str, str], Counter[Cause]] = {}
        self._reached: set[tuple[str, str]] = set()
        self._flaky_threshold = flaky_threshold

    def record_success(self, url: str) -> None:
        """Note that this host answered. Immunizes it for the rest of the run."""
        key = _key(url)
        if key is None:
            return
        with self._lock:
            self._reached.add(key)

    def record_failure(self, url: str, failure: GitFailure) -> None:
        """Note a failure against this host, under the cause its own output names.

        An unrecognized stderr is recorded as nothing at all — diagnose refuses to guess a cause,
        and a breaker that trips on 'something went wrong' would skip repos over a message no one
        has read.
        """
        key = _key(url)
        cause = classify_failure(failure)
        if key is None or cause is None:
            return
        with self._lock:
            self._failures.setdefault(key, Counter())[cause] += 1

    def trip_for(self, url: str) -> Trip | None:
        """The reason this host is closed, or None if it is still worth asking."""
        key = _key(url)
        if key is None:
            return None
        with self._lock:
            if key in self._reached:
                return None
            counts = Counter(self._failures.get(key, ()))
        host = key[0]
        for cause in counts:
            if cause in HOST_WIDE_CAUSES:
                return Trip(host=host, cause=cause)
        for cause, count in counts.items():
            if cause in FLAKY_CAUSES and count >= self._flaky_threshold:
                return Trip(host=host, cause=cause)
        return None
