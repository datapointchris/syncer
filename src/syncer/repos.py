from __future__ import annotations

import os
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path

# Ceiling on a single git invocation. Generous enough for a fetch over a VPN; the point is that
# a wedged call eventually reports instead of holding a worker thread forever.
GIT_TIMEOUT_SECONDS = 120
# Cloning a large repo legitimately takes minutes, and it happens once per repo. A multiple of
# the configured timeout rather than a constant, so raising git_timeout for a slow network
# raises the clone ceiling too — which is what config.toml and the README always claimed.
CLONE_TIMEOUT_MULTIPLIER = 5
# Exit code for a timeout, matching the shell's convention for a command killed by `timeout`.
TIMEOUT_RETURNCODE = 124


@dataclass(frozen=True, slots=True)
class GitFailure:
    """A git call that failed, kept so the reason can reach the user.

    The habit this exists to break: every accessor below used to convert a non-zero exit into a
    benign-looking value ([] branches, (0,0) counts, a clean tree), which is how a repo whose
    fetch died still reported `synced`.
    """

    argv: tuple[str, ...]
    returncode: int
    stderr: str

    @property
    def timed_out(self) -> bool:
        return self.returncode == TIMEOUT_RETURNCODE

    @property
    def command(self) -> str:
        return ' '.join(('git', *self.argv))


def normalize_remote_url(url: str) -> str:
    """Reduce a clone URL to `host/path` so equivalent forms compare equal.

    The same repo is reachable as `https://host/o/r`, `git@host:o/r.git` and
    `ssh://git@host:7999/o/r.git`. Comparing raw strings would report every repo cloned over
    SSH against an https registry as a mismatch, which is noise that would get the check
    ignored — and an ignored check is how a wrong origin survives for months.
    """
    url = url.strip().rstrip('/')
    url = re.sub(r'^[A-Za-z][A-Za-z0-9+.\-]*://', '', url)
    url = re.sub(r'^[^/@]+@', '', url)
    host, separator, rest = url.partition(':')
    if separator:
        # A numeric segment is a port; anything else is scp-style `host:owner/repo`.
        port, slash, tail = rest.partition('/')
        url = f'{host}/{tail}' if port.isdigit() and slash else f'{host}/{rest}'
    return url.removesuffix('.git').lower()


def origin_mismatch(repo: Repo) -> str | None:
    """The clone's actual origin when it disagrees with the registry, else None.

    Report-only by design: a deliberate fork with a legitimately different origin is
    indistinguishable from a mistake without asking, so this never rewrites a remote.
    """
    actual = repo.origin_url
    if actual is None or normalize_remote_url(actual) == normalize_remote_url(repo.url):
        return None
    return actual


def _noninteractive_env() -> dict[str, str]:
    """Git environment that fails fast rather than blocking on a prompt.

    Prompting is what concurrency turns into a hang: git asks for credentials on /dev/tty, which
    capture_output does not redirect, so an expired credential or an unknown host key would leave
    every worker thread blocked on the same terminal with nothing explaining why. BatchMode covers
    ssh's password *and* host-key confirmation prompts, and is appended to any GIT_SSH_COMMAND the
    user already configured rather than replacing it.
    """
    env = os.environ.copy()
    env['GIT_TERMINAL_PROMPT'] = '0'
    ssh_command = env.get('GIT_SSH_COMMAND', 'ssh')
    env['GIT_SSH_COMMAND'] = f'{ssh_command} -o BatchMode=yes'
    return env


def run_command(args: list[str], *, cwd: Path | None = None, timeout: int) -> subprocess.CompletedProcess[str]:
    """Run a command non-interactively, turning a timeout into an ordinary non-zero result.

    Every caller branches on returncode, so a timeout has to look like a failure — raising out of
    a worker thread would lose the whole repo's report instead of the one call that hung.
    """
    try:
        return subprocess.run(  # nosec B607
            args,
            cwd=cwd,
            capture_output=True,
            text=True,
            env=_noninteractive_env(),
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(args, returncode=TIMEOUT_RETURNCODE, stdout='', stderr=f'timed out after {timeout}s')


class Repo:
    def __init__(self, name: str, path: Path, owner: str, host: str, timeout: int = GIT_TIMEOUT_SECONDS, url: str | None = None):
        self.name = name
        self.path = path
        self.owner = owner
        self.url = url or f'{host}/{owner}/{name}'
        self.timeout = timeout
        # Thread-confined: report.py builds one Repo per worker task, so a plain list is safe.
        self.failures: list[GitFailure] = []

    def _git(self, *args: str, probe: bool = False) -> subprocess.CompletedProcess[str]:
        """Run git in this repo, recording a non-zero exit unless it is a probe.

        Recording is the default so a git call added later is visible without anyone
        remembering to opt in — the same direction PROTECTED_ALLOWED takes. `probe=True` is for
        the handful of calls that ask a yes/no question, where non-zero *is* the answer and
        recording it would bury the real failures in noise.
        """
        result = run_command(['git', *args], cwd=self.path, timeout=self.timeout)
        if result.returncode != 0 and not probe:
            self.failures.append(GitFailure(argv=args, returncode=result.returncode, stderr=result.stderr.strip()))
        return result

    @property
    def exists(self) -> bool:
        return self.path.exists()

    @property
    def is_git_repo(self) -> bool:
        return (self.path / '.git').is_dir()

    def remotes(self) -> list[str] | None:
        """Configured remote names, or None when git could not be asked.

        Distinguished because they need different words: a repo with no remote is a repo you
        never pushed anywhere, while a `git remote` that failed says nothing about remotes at
        all — reporting the second as 'no remote' sends you to fix something that is fine.
        """
        result = self._git('remote')
        if result.returncode != 0:
            return None
        return [line for line in result.stdout.strip().splitlines() if line]

    @property
    def has_remote(self) -> bool:
        return bool(self.remotes())

    @property
    def origin_url(self) -> str | None:
        """Where this clone actually points, as opposed to where the registry says it should."""
        result = self._git('remote', 'get-url', 'origin')
        return result.stdout.strip() or None if result.returncode == 0 else None

    @property
    def current_branch(self) -> str:
        result = self._git('rev-parse', '--abbrev-ref', 'HEAD')
        return result.stdout.strip()

    @property
    def default_branch(self) -> str | None:
        # probe: every call here asks "does this ref exist", and walking the fallback chain is
        # the normal path on a repo whose origin/HEAD was never set.
        result = self._git('symbolic-ref', 'refs/remotes/origin/HEAD', probe=True)
        if result.returncode == 0:
            branch = result.stdout.strip().replace('refs/remotes/origin/', '')
            # Verify the tracking ref exists (could be stale after a rename)
            if self._git('rev-parse', '--verify', f'refs/remotes/origin/{branch}', probe=True).returncode == 0:
                return branch
        for branch in ('main', 'master'):
            check = self._git('rev-parse', '--verify', f'refs/heads/{branch}', probe=True)
            if check.returncode == 0:
                return branch
        return None

    @property
    def is_detached(self) -> bool:
        return self.current_branch == 'HEAD'

    @property
    def is_dirty(self) -> bool:
        """True when the tree has changes *or* git could not tell us.

        Invariant 2 is 'never mutate a tree you cannot verify is clean'. Returning a list and
        letting callers test its truthiness made a failing `git status` read as clean — i.e. as
        permission to mutate — so the unknown case has to answer True, which a `list | None`
        could never do: None is falsy.
        """
        result = self._git('status', '--porcelain')
        return result.returncode != 0 or bool(result.stdout.strip())

    @property
    def uncommitted_changes(self) -> list[str]:
        """The changed paths, for counting only. Use is_dirty for any safety decision."""
        result = self._git('status', '--porcelain')
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.strip().splitlines() if line]

    def local_branches(self) -> list[str]:
        result = self._git('for-each-ref', '--format=%(refname:short)', 'refs/heads/')
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.splitlines() if line]

    def remote_only_branches(self) -> list[str]:
        """Branches on origin with no local counterpart, newest commit first.

        The refs are already here — a fetch brings down every branch — but the pipeline only
        ever iterates local branches, so a long-lived branch you deliberately never check out
        (develop/uat/prod) is invisible: nothing tells you origin/prod moved.
        """
        result = self._git('for-each-ref', '--sort=-committerdate', '--format=%(refname:short)', 'refs/remotes/origin/')
        if result.returncode != 0:
            return []
        local = set(self.local_branches())
        remote = (line.removeprefix('origin/') for line in result.stdout.splitlines() if line)
        return [branch for branch in remote if branch != 'HEAD' and branch not in local]

    def branch_age(self, ref: str) -> str:
        """Human-readable age of a ref's last commit ('3 days ago'), or '' if it can't be read."""
        result = self._git('log', '-1', '--format=%ar', ref)
        return result.stdout.strip() if result.returncode == 0 else ''

    def branch_upstream(self, branch: str) -> tuple[str, bool]:
        """Return (upstream_short, is_gone) for a branch.

        upstream_short is '' when the branch has no tracking config (NO_UPSTREAM).
        is_gone is True when the tracking config exists but the remote branch was
        deleted (post-prune) — git reports this as an '[gone]' upstream:track.
        """
        result = self._git('for-each-ref', '--format=%(upstream:short)%09%(upstream:track)', f'refs/heads/{branch}')
        if result.returncode != 0 or not result.stdout.strip():
            return '', False
        upstream_short, _, track = result.stdout.rstrip('\n').partition('\t')
        return upstream_short, track.strip() == '[gone]'

    def ahead_behind(self, branch: str, upstream: str) -> tuple[int, int] | None:
        """(ahead, behind) commit counts of `branch` vs `upstream`, or None if git could not say.

        None rather than (0, 0): the counts feed _primary_from_counts, which reads (0, 0) as
        SYNCED — so a missing remote-tracking ref after a failed fetch used to make a repo that
        had never reached its remote report as fully in sync.
        """
        result = self._git('rev-list', '--left-right', '--count', f'{branch}...{upstream}')
        if result.returncode != 0:
            return None
        parts = result.stdout.split()
        if len(parts) != 2:
            return None
        return int(parts[0]), int(parts[1])

    def _target_ref(self, target: str) -> str:
        ref = f'origin/{target}'
        if self._git('rev-parse', '--verify', ref, probe=True).returncode != 0:
            ref = target
        return ref

    def is_merged_into(self, branch: str, target: str) -> bool:
        """True if `branch` is an ancestor of `target` (prefer origin/<target> if present)."""
        # probe: --is-ancestor answers with its exit code; non-zero means "no", not "broke".
        return self._git('merge-base', '--is-ancestor', branch, self._target_ref(target), probe=True).returncode == 0

    def is_patch_applied_in(self, branch: str, target: str) -> bool:
        """True if every commit unique to `branch` has a patch-equivalent commit in `target`.

        Catches squash and cherry-pick integration, which rewrite commits so ancestry can never
        see them. `git cherry` marks a commit '-' when its patch-id is already present upstream
        and '+' when it is not. A multi-commit branch collapsed into a single squash commit has
        no matching patch-ids and correctly reports '+' — a false negative costs only a refusal.
        """
        # probe: a missing target ref is an ordinary "cannot prove it", and the caller's only
        # response is to refuse the delete — which is already the safe outcome.
        result = self._git('cherry', self._target_ref(target), branch, probe=True)
        if result.returncode != 0:
            return False
        return all(line.startswith('-') for line in result.stdout.splitlines() if line.strip())

    def contains_branch(self, branch: str, target: str) -> bool:
        """True if `target` already holds every change on `branch`, by ancestry or by patch."""
        return self.is_merged_into(branch, target) or self.is_patch_applied_in(branch, target)

    @property
    def unpushed_commits(self) -> int:
        branch = self.default_branch
        if not branch:
            return 0
        result = self._git('rev-list', f'origin/{branch}..{branch}', '--count')
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip())

    @property
    def behind_remote(self) -> int:
        branch = self.default_branch
        if not branch:
            return 0
        result = self._git('rev-list', f'{branch}..origin/{branch}', '--count')
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip())

    @property
    def unpushed_commit_lines(self) -> list[str]:
        branch = self.default_branch
        if not branch:
            return []
        result = self._git('log', '--oneline', f'origin/{branch}..{branch}')
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.strip().splitlines() if line]

    @property
    def behind_commit_lines(self) -> list[str]:
        branch = self.default_branch
        if not branch:
            return []
        result = self._git('log', '--oneline', f'{branch}..origin/{branch}')
        if result.returncode != 0:
            return []
        return [line for line in result.stdout.strip().splitlines() if line]

    @property
    def stash_count(self) -> int:
        result = self._git('stash', 'list')
        if not result.stdout.strip():
            return 0
        return len(result.stdout.strip().splitlines())

    @property
    def last_commit_date(self) -> str | None:
        result = self._git('log', '-1', '--format=%aI')
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip()

    @property
    def first_commit_date(self) -> str | None:
        result = self._git('log', '--reverse', '--format=%aI')
        if result.returncode != 0 or not result.stdout.strip():
            return None
        return result.stdout.strip().splitlines()[0]

    @property
    def total_commits(self) -> int:
        result = self._git('rev-list', '--count', 'HEAD')
        if result.returncode != 0:
            return 0
        return int(result.stdout.strip())

    def fetch(self) -> GitFailure | None:
        """Refresh remote-tracking refs. Returns the failure, or None on success.

        The return value is the measurement's validity: every branch state below is computed
        against these refs, so a caller that ignores it is reporting on stale or absent data.
        That is exactly what classify_repo did, which is how a repo that had never reached its
        remote reported as synced.
        """
        result = self._git('fetch', '--quiet')
        return None if result.returncode == 0 else self.failures[-1]

    def fetch_prune(self) -> GitFailure | None:
        """fetch --prune, so a deleted upstream branch classifies as gone rather than synced."""
        result = self._git('fetch', '--prune', '--quiet')
        return None if result.returncode == 0 else self.failures[-1]

    def set_head_auto(self) -> None:
        """Repoint origin/HEAD to the remote's real default (fixes stale ref after a
        default-branch rename). No-op when there's no remote."""
        if not self.has_remote:
            return
        self._git('remote', 'set-head', 'origin', '--auto')

    def pull_rebase(self) -> bool:
        result = self._git('pull', '--rebase')
        return result.returncode == 0

    def rebase_abort(self) -> bool:
        result = self._git('rebase', '--abort')
        return result.returncode == 0

    def merge_ff_only(self, upstream: str) -> tuple[bool, str]:
        """Fast-forward the current branch to its upstream. Fails (never merges) if the
        upstream is not strictly ahead."""
        result = self._git('merge', '--ff-only', upstream)
        return result.returncode == 0, result.stderr.strip()

    def update_ref(self, branch: str, target: str) -> tuple[bool, str]:
        """Advance a (non-current) local branch ref to `target` without a checkout."""
        result = self._git('update-ref', f'refs/heads/{branch}', target)
        return result.returncode == 0, result.stderr.strip()

    def push_branch(self, branch: str, set_upstream: bool = False) -> tuple[bool, str]:
        """Push a specific branch with an explicit refspec (never 'whatever is checked out')."""
        args = ['push']
        if set_upstream:
            args += ['-u', 'origin', branch]
        else:
            args += ['origin', f'{branch}:{branch}']
        result = self._git(*args)
        return result.returncode == 0, result.stderr.strip()

    def delete_local_branch(self, branch: str) -> tuple[bool, str]:
        """Delete a local branch. Uses -D because a GONE branch has no upstream for git's
        own merged-check to consult — safety is enforced upstream by the delete_local guard
        (merged-into-default, not current, not default, clean), not by git's -d heuristic."""
        result = self._git('branch', '-D', branch)
        return result.returncode == 0, result.stderr.strip()

    @property
    def is_github(self) -> bool:
        """Whether `gh` can answer questions about this repo at all. Judged from the resolved
        clone URL, which is where the repo actually lives — a url_template can point somewhere
        the registry `host` does not."""
        return 'github.com' in self.url

    @property
    def is_fork(self) -> bool:
        if not self.is_github:
            return False
        result = run_command(
            ['gh', 'repo', 'view', f'{self.owner}/{self.name}', '--json', 'isFork', '--jq', '.isFork'],
            timeout=self.timeout,
        )
        return result.returncode == 0 and result.stdout.strip() == 'true'

    def clone(self) -> tuple[bool, str]:
        """Clone into the registry path. Returns (ok, stderr), like every other mutator.

        Returning the stderr is what makes a failed clone diagnosable at all: auth, an unknown
        host key, DNS, a bad url_template and a timeout are otherwise indistinguishable, and
        capture_output means git's own message never reaches the terminal either.
        """
        self.path.parent.mkdir(parents=True, exist_ok=True)
        # --quiet, as fetch does: git writes progress to stderr even when it succeeds, and
        # without it the returned text is chatter on success and chatter-plus-error on failure.
        argv = ('clone', '--quiet', self.url, str(self.path))
        # Not _git: there is no repo to run inside yet. Records by hand so a clone failure joins
        # the same set the failure summary groups by cause.
        result = run_command(['git', *argv], timeout=self.timeout * CLONE_TIMEOUT_MULTIPLIER)
        if result.returncode != 0:
            self.failures.append(GitFailure(argv=argv, returncode=result.returncode, stderr=result.stderr.strip()))
        return result.returncode == 0, result.stderr.strip()


def find_repo_in_search_paths(name: str, search_paths: list[Path], claimed_paths: set[Path] | None = None) -> Path | None:
    claimed = claimed_paths or set()
    for search_path in search_paths:
        if not search_path.exists():
            continue
        for item in search_path.iterdir():
            if item.is_dir() and item.name == name and (item / '.git').is_dir() and item not in claimed:
                return item
        for item in search_path.iterdir():
            if item.is_dir() and not item.name.startswith('.'):
                for sub in item.iterdir():
                    if sub.is_dir() and sub.name == name and (sub / '.git').is_dir() and sub not in claimed:
                        return sub
    return None


def find_untracked_repos(search_path: Path, known_paths: set[Path], excluded: set[Path] | None = None, max_depth: int = 3) -> list[Path]:
    """Find git repos under search_path that no registry entry claims.

    Recurses, because repos are routinely nested more than one level down
    (~/code/python-projects/, ~/code/sql/, ~/code/refs/). Scanning only direct
    children silently passed every one of those — which is exactly how twenty
    reference clones dropped out of tracking without a word.

    Matches on resolved path, not name: two repos can share a basename.
    Descent stops at a repo, so nested worktrees and vendored checkouts are not
    reported separately.
    """
    excluded = excluded or set()
    if max_depth <= 0 or not search_path.is_dir() or search_path.resolve() in excluded:
        return []

    found: list[Path] = []
    try:
        entries = sorted(search_path.iterdir())
    except PermissionError:
        return []

    for item in entries:
        if not item.is_dir() or item.is_symlink() or item.name.startswith('.'):
            continue
        if (item / '.git').exists():
            if item.resolve() not in known_paths:
                found.append(item)
            continue
        found.extend(find_untracked_repos(item, known_paths, excluded, max_depth - 1))
    return found
