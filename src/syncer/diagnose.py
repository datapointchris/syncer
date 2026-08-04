"""Turn a git failure into a cause and an actionable hint. Pure: no git, no filesystem.

Kept separate from the reporter so the pattern table can be exercised against real captured
stderr with no fixtures at all, and so the honesty rules below are testable statements rather
than intentions:

1. **Never invent a diagnosis.** An unrecognised stderr yields no cause and no guess. A wrong
   confident explanation is worse than none, because it sends someone to fix the wrong thing.
2. **Derive the remedy from the URL, not from a vendor assumption.** `gh auth login` is only
   ever suggested when the host really is github.com; a corporate Bitbucket or an internal
   GitLab must not be told to run it.
3. **Always show git's own words too.** The cause is a summary, and summaries lose things.
"""

from __future__ import annotations

from collections import OrderedDict
from collections.abc import Iterable
from dataclasses import dataclass
from enum import StrEnum

from syncer.repos import GitFailure
from syncer.repos import normalize_remote_url


class Cause(StrEnum):
    HOST_KEY = 'host_key'
    AUTH = 'auth'
    DNS = 'dns'
    NETWORK = 'network'
    NOT_FOUND = 'not_found'
    TIMEOUT = 'timeout'


# Literal fragments of real git/ssh/curl output, matched case-insensitively.
#
# Order is load-bearing and most-specific-first. A rejected host key emits BOTH
# 'Host key verification failed' and a permission-denied line, so matching AUTH first would send
# you to re-check a credential that was never the problem. Nothing here matches the generic
# 'Could not read from remote repository', which accompanies every one of these.
_PATTERNS: tuple[tuple[Cause, tuple[str, ...]], ...] = (
    (
        Cause.HOST_KEY,
        (
            'host key verification failed',
            'remote host identification has changed',
            'no matching host key type found',
            'and you have requested strict checking',
        ),
    ),
    (
        Cause.AUTH,
        (
            'permission denied (publickey',
            'permission denied, please try again',
            'authentication failed',
            'could not read username',
            'could not read password',
            'terminal prompts disabled',
            'invalid username or password',
            'support for password authentication was removed',
            'access denied',
        ),
    ),
    (
        Cause.DNS,
        (
            'could not resolve host',
            'name or service not known',
            'nodename nor servname provided',
            'temporary failure in name resolution',
        ),
    ),
    (
        Cause.NETWORK,
        (
            'connection refused',
            'connection timed out',
            'network is unreachable',
            'no route to host',
            'failed to connect to',
            'operation timed out',
            'ssl certificate problem',
        ),
    ),
    (
        Cause.NOT_FOUND,
        (
            'repository not found',
            'does not appear to be a git repository',
            'does not exist',
            'remote: not found',
            'project not found',
        ),
    ),
)

# Why a credential that works by hand can fail here, which nothing in git's own output hints at.
_BATCH_MODE_NOTE = 'syncer runs git non-interactively, so anything that would have prompted fails instead of asking'


def remote_host(url: str) -> str:
    """The host of a clone URL, however it is spelled. '' for a local filesystem path.

    normalize_remote_url already folds https, scp-style SSH and ssh://host:port to `host/path`,
    so this only has to strip the path — and recognise that a filesystem path has no host to
    name, which is what an SSH alias with no dots (`work-git`) would otherwise be mistaken for.
    """
    if url.startswith(('.', '/', '~')):
        return ''
    return normalize_remote_url(url).partition('/')[0]


def classify_failure(failure: GitFailure) -> Cause | None:
    """The cause of a failure, or None when its output matches nothing known.

    None is a real answer, not a fallback bucket — see honesty rule 1.
    """
    if failure.timed_out:
        return Cause.TIMEOUT
    text = failure.stderr.lower()
    for cause, fragments in _PATTERNS:
        if any(fragment in text for fragment in fragments):
            return cause
    return None


def hint_lines(cause: Cause | None, url: str) -> list[str]:
    """What to actually do about `cause`, worded for the host `url` really points at."""
    host = remote_host(url)
    is_ssh = not url.startswith(('http://', 'https://'))
    named_host = host or 'the remote'

    if cause is Cause.HOST_KEY:
        return [
            f'{_BATCH_MODE_NOTE} — an unknown host key fails rather than prompting.',
            f'trust it once, after checking the fingerprint: ssh-keyscan {named_host} >> ~/.ssh/known_hosts',
        ]
    if cause is Cause.AUTH:
        lines = [f'{_BATCH_MODE_NOTE}, so run the same command by hand to see what it asks for.']
        if is_ssh:
            lines.append(f'check a key is loaded and accepted: ssh-add -l, then ssh -T git@{named_host}')
        else:
            lines.append(f'check the credential helper has an entry for {named_host}: git credential fill')
        # Only for the host it is actually true of. A work Bitbucket told to run `gh` is noise.
        if host == 'github.com':
            lines.append('or authenticate the GitHub CLI: gh auth login')
        return lines
    if cause is Cause.DNS:
        return [
            f'{named_host} did not resolve — check the spelling in the registry, and whether this needs a VPN.',
        ]
    if cause is Cause.NETWORK:
        return [f'could not reach {named_host} — check the VPN, proxy, and whether the host is up.']
    if cause is Cause.NOT_FOUND:
        return [
            'the URL resolved but there is no repo there — check the registry entry.',
            'an empty registry `owner` builds URLs like https://host//name, which fails exactly this way.',
        ]
    if cause is Cause.TIMEOUT:
        return ['raise git_timeout in config.toml if this host is legitimately slow.']
    return []


@dataclass(frozen=True)
class FailureGroup:
    """One cause affecting one host, however many repos hit it."""

    cause: Cause | None
    host: str
    repos: tuple[str, ...]
    stderr: str
    hints: tuple[str, ...]

    @property
    def summary(self) -> str:
        where = f' reaching {self.host}' if self.host else ''
        what = self.cause.value.replace('_', ' ') if self.cause else 'failed'
        count = len(self.repos)
        return f'{count} repo{"s" if count != 1 else ""}{where}: {what}'


def group_failures(items: Iterable[tuple[str, str, GitFailure]]) -> list[FailureGroup]:
    """Collapse (repo_name, url, failure) triples into one group per (cause, host).

    Grouped because N repos behind one dead VPN produce N identical stderr blobs, and a wall of
    those is its own kind of unreadable — the thing to fix is named once. Insertion-ordered so
    output is stable across runs; the caller has already sorted its reports.
    """
    grouped: OrderedDict[tuple[Cause | None, str], list[tuple[str, str, GitFailure]]] = OrderedDict()
    for name, url, failure in items:
        grouped.setdefault((classify_failure(failure), remote_host(url)), []).append((name, url, failure))

    groups = []
    for (cause, host), members in grouped.items():
        first_name, first_url, first_failure = members[0]
        groups.append(
            FailureGroup(
                cause=cause,
                host=host,
                repos=tuple(name for name, _, _ in members),
                stderr=first_failure.stderr,
                hints=tuple(hint_lines(cause, first_url)),
            )
        )
    return groups
