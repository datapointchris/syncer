# CHANGELOG


## v7.0.0 (2026-08-04)

### Bug Fixes

- **config**: Reject a url_template that doubles the scheme
  ([`c5e766d`](https://github.com/datapointchris/syncer/commit/c5e766dc4a1d0e7d1862e20f8121426a0ba30490))

The registry `host` ships as 'https://github.com', scheme included, so writing the obvious-looking
  template 'https://{host}/{owner}/{name}' silently produces 'https://https://github.com/...'. git
  then reports `Could not resolve host: https`, which names neither setting responsible and reads
  like a network fault.

Caught at load time rather than by a connectivity check, because a URL carrying two schemes is
  malformed on inspection.

- **report**: Keep the origin check when the fetch fails
  ([`411cf41`](https://github.com/datapointchris/syncer/commit/411cf41673686f3a1d7ae5dc899718d7546330e9))

CI caught two things a credentialed laptop hid.

The fetch-failure gate returned before computing origin_mismatch, which needs no network and is
  often the *reason* the fetch failed: a repo pointing at a host you have no credential for fails
  exactly like a network problem, and without this line the report sends you to debug the network
  instead of the remote. It is now computed and rendered on the failure path, and the failure
  summary groups on the real origin rather than the registry's expectation.

The origin-mismatch tests pointed at a live github.com URL, so they passed on a machine with
  credentials and failed on one without — the check compares two strings and never needed the
  network. They now use a local bare remote, so the fetch genuinely succeeds everywhere, plus a new
  case covering the failed-fetch path.

Also stops asserting `git init` produces 'main': it honours init.defaultBranch, which is 'master' on
  a stock runner.

### Features

- **cli**: Exit non-zero on errors and add --json
  ([`024f742`](https://github.com/datapointchris/syncer/commit/024f7422d760060c708b838736718dc4e6ee0501))

Every run exited 0 no matter how many repos failed, and `issues` printed "N issue(s) found" and
  exited 0 too — the exact shape of a check whose caller has to scrape text for what the exit code
  should have said. There was no machine-readable output on the sync surface at all.

One rule, stated once in exit_code_for: exit 1 iff any report reaches Severity.ERROR. Report-only
  and --apply share it. WARNING stays 0 on purpose — a repo that is `ahead` is the normal state of a
  machine somebody works on, and a code that is non-zero every day is one nobody can automate
  against.

--json reuses the existing RepoSnapshot/RunSummary models rather than a parallel shape, so the JSON
  and the event stream cannot disagree about a run. Grouped failure causes ride along with git's own
  stderr.

Also fixes the host a failure is attributed to: a fetch failure is about the clone's real origin,
  not the registry's expected URL. Grouping on the latter filed a corporate host's outage under
  github.com and offered a hint for the wrong machine.

BREAKING CHANGE: syncer, syncer branches and syncer issues now exit 1 when they report a problem,
  where they previously always exited 0.

- **config**: Scaffold a minimal starter and scan repos off disk
  ([`a39aee1`](https://github.com/datapointchris/syncer/commit/a39aee1ef4aff727bfab771f0b8a5fc7575157f7))

`config init` wrote a teaching document. It shipped a [policies.laptop] block that `policy list`
  renders indistinguishably from a built-in, a [repo_overrides] entry for a repo nobody has, and
  three fake repos — so the very first `syncer` run printed three `would clone` lines for repos that
  never existed, teaching a new user on run one that this tool's output is noise.

STARTER_* is now what `init` writes and TEMPLATE_* what `example` prints. A scaffold must have
  nothing in it to delete; a reference must have everything. Those are different documents, and the
  invariant that mattered — a template cannot drift from its model — survives, because the
  round-trip test now parses all four. The starter config deliberately has no `repos_file` line, not
  even commented out: it is the one setting whose correct value differs per machine and whose wrong
  value fails every run outright.

`config scan PATHS...` builds entries from the repos already on disk, deriving each one's owner and
  host from its real origin, so a directory holding both your repos and third-party clones scans
  correctly. It prints for review by default; --write refuses a registry that already lists repos,
  but fills the empty one `init` just wrote — refusing there made the documented two-step flow
  contradict itself. A URL the default {host}/{owner}/{name} shape cannot express is recorded
  verbatim as clone_url rather than emitted as an entry that cannot be rebuilt.

`config edit` gains the same config|registry positional the other two take, and resolves through
  registry_location so it opens what syncer really reads. It opened only config.toml, while the file
  a new machine needs edited is the registry.

BREAKING CHANGE: `syncer config init` writes a minimal config and an empty registry instead of the
  annotated templates. Existing files are untouched, as before.

- **doctor**: Check whether this machine can run syncer at all
  ([`fd0affa`](https://github.com/datapointchris/syncer/commit/fd0affac69cebfc664c96d4600cba1aab0ce49ac))

`config validate` checks structure and `issues` checks reality, and both could pass on a box where
  nothing worked. A first run that failed had no way to distinguish a missing credential from a
  mis-pointed registry from a host that was never reachable — the output named a path but never what
  had chosen it, so the tool looked like it had someone else's layout hard-coded.

doctor runs nine checks in prerequisite order, so the first failure is the one to act on: git, the
  resolved config/registry/state paths with each one's provenance, both files parsing, template
  placeholders, whether clone URLs resolve at all, reachability, clones on disk, and policy
  resolution.

Reachability is proved with `git ls-remote` against the URL the registry actually resolves to —
  never `gh`, which a corporate Bitbucket or an internal GitLab cannot answer — and a failure is run
  through the same cause detection the sync report uses, so it names git's words, the cause, and
  what to do. Its timeout is deliberately not git_timeout: that is sized for fetching a monorepo
  over a VPN, and a diagnostic nobody waits for is not a diagnostic.

A registry still holding template placeholders skips the network checks entirely, since reporting a
  DNS failure for `your-github-username` names the wrong problem.

Exits 1 on FAIL and 0 on WARN, so `syncer doctor && syncer --apply` stops on a box that was never
  going to work but not on one whose repos simply are not cloned yet. Never writes anything.

- **git**: Treat an unverifiable repo as an error, not as synced
  ([`40c82c5`](https://github.com/datapointchris/syncer/commit/40c82c57c423321d51fbe848e3063e1cb4515dee))

classify_repo never bound the fetch's return value, and ahead_behind returned (0, 0) when rev-list
  failed — which _primary_from_counts reads as SYNCED. A repo that had never once reached its remote
  therefore reported as fully in sync, stated confidently, which is the exact opposite of the one
  question syncer exists to answer. On a machine with no key or credential, BatchMode makes every
  fetch fail instantly, so that was the whole report.

Repo._git now records a GitFailure for any non-zero exit unless the call passes probe=True,
  refresh_remote returns the fetch's failure, and _build_repo_report turns that into a repo-level
  error before classifying — which is also what refuses execution, since no execute() call is
  constructed. Such a report carries zero rows deliberately: a row is a claim about a branch, and
  the point is that no claim can be made.

Also closes a safety hole of the same shape. uncommitted_changes returned [] when git status failed,
  so the invariant-2 gates read a broken git as a clean tree, i.e. as permission to mutate. Gates
  now call is_dirty, which answers True when it cannot tell.

Run history gains the distinction it was flattening: clone_failed no longer maps to 'missing', so
  'git said no' is no longer recorded identically to 'nobody has cloned it yet'.

RepoSnapshot.status is typed str rather than the RepoStatus Literal — adding a status member made
  the previous release refuse to read its own history file, because a closed Literal on the read
  side rejects anything a newer version wrote. The write vocabulary is still constrained, by mypy.
  read_events now skips an unparseable line for the same reason.

BREAKING CHANGE: a repo whose state cannot be established now renders as an error instead of
  reporting synced, and no action runs against it.

- **report**: Group repo failures by cause and hint once
  ([`dc884fb`](https://github.com/datapointchris/syncer/commit/dc884fba183b30624c899b8f58746de27858c590))

Twenty repos behind one dead VPN produced twenty identical stderr blobs and no statement of the
  single thing to fix. Failures are now collapsed to one block per (cause, host), and each block
  says what to do — the first use of output.hint() anywhere on the sync surface, which until now was
  reachable only from the config commands.

diagnose.py is pure, so its pattern table is exercised against real captured stderr with no
  fixtures, and three honesty rules are testable statements rather than intentions:

- Unrecognised output yields no cause and no guess. A confident wrong explanation sends someone to
  fix the wrong thing. - Remedies derive from the resolved URL's host, so `gh auth login` is
  suggested only when the host really is github.com — a corporate Bitbucket told to run it is noise
  that teaches you to skip hints. - Raw stderr is always shown; the cause is a summary and summaries
  lose things.

Host-key patterns are matched before auth ones because a rejected key emits both, and the key is the
  actionable half. Every auth and host-key hint states the BatchMode caveat: nothing in git's output
  hints that a credential which works by hand fails here because syncer runs git non-interactively.

### Refactoring

- **output**: Consolidate on one console pair
  ([`24b2023`](https://github.com/datapointchris/syncer/commit/24b20238a88484e457dee76beefa02bd4012b885))

output.py declared "stdout is data, stderr is everything else", and the entire sync surface bypassed
  it. There were three separate Console objects — one in repos.py that repos.py never used, plus one
  each in main.py and stats.py — so every runtime error went to stdout, which is the opposite of the
  stated contract.

The icons and line helpers move to output.py with them. They are presentation, and repos.py has no
  business importing rich to run a subprocess; it is now stdlib-only, which is a structural proof
  that the git layer is only a git layer.

highlight=False globally, which was already the setting on two of the three consoles. Rich's
  automatic highlighting colours anything shaped like a number or a path, turning a report of branch
  names and commit counts into confetti.

Also gives the origin-mismatch lines the soft_wrap every other path message has. Both are URLs, the
  whole point of the check is to hand you two you can compare, and Rich was breaking them mid-path.

- **repos**: Move find_untracked_repos out of main
  ([`1e54a87`](https://github.com/datapointchris/syncer/commit/1e54a8717c58c726e86c24ebb2941cad4ccb77fb))

It is repo discovery, which is repos.py's domain, and `config scan` needs it too — a second copy in
  a command module is how two callers drift apart. Its tests move with it, per one test module per
  source module.

### Breaking Changes

- **config**: `syncer config init` writes a minimal config and an empty registry instead of the
  annotated templates. Existing files are untouched, as before.


## v6.0.3 (2026-08-04)

### Bug Fixes

- **clone**: Surface git's error when a clone fails
  ([`ffb16d2`](https://github.com/datapointchris/syncer/commit/ffb16d2766f158dd8a81506b283e7a8a44327127))

A failed clone rendered as a bare 'clone failed' line. Repo.clone dropped result.stderr at the
  source and the report call site hard-coded the detail slot to None, so auth, an unknown host key,
  DNS, a bad url_template and a 600s timeout were one indistinguishable line. capture_output means
  git's own message never reaches the terminal either, so the screen was empty.

clone() now returns (ok, stderr) like every other mutator, and the report names the attempted URL as
  well as git's words: a wrong url_template or an empty registry owner produces a URL git rejects
  for reasons its message alone never explains.

Also scales the clone ceiling from git_timeout rather than a hard-coded 600s, which is what
  config.toml and the README already claimed.

### Chores

- **toolchain**: Adopt the generated configs and CI
  ([`3ae9763`](https://github.com/datapointchris/syncer/commit/3ae976319ad7983b04b20cbae7c17b4bfc05ab17))

Brings the repo onto forge toolchain manifest 11.

codespell now skips CHANGELOG.md, which semantic-release generates from commit subjects: the typo it
  caught lives in a commit message that will never be rewritten, and fixing the file is undone on
  the next release.

### Documentation

- Flush dormant markdownlint violations
  ([`9b4023c`](https://github.com/datapointchris/syncer/commit/9b4023c67181bb64d64033f0a79e31b814179ea8))

markdownlint only runs on the files a commit touches, so unmodified docs accumulate violations
  invisibly. The toolchain sync bumps markdownlint to v0.47, which added MD060, and runs --all-files
  — surfacing every one of them at once, in the middle of an unrelated change.

Table separators are normalized to the compact `| --- |` style MD060 expects, which --fix cannot
  repair; everything else is markdownlint --fix. CHANGELOG.md is excluded instead of normalized:
  semantic-release regenerates it on every release, so any fix there is undone and comes back as a
  rebase conflict.

- Note the scaffolded laptop policy is not a built-in
  ([`6e2bbfc`](https://github.com/datapointchris/syncer/commit/6e2bbfc456c0f6e465520125ac074523a72c137e))

config init writes a config.toml containing an example [policies.laptop] block, so policy list shows
  it beside standard/observe/mirror on a fresh machine and it reads as pre-installed.


## v6.0.2 (2026-07-31)

### Bug Fixes

- **ci**: Run ruff and pytest without depending on repo dev deps
  ([`84cbd2c`](https://github.com/datapointchris/syncer/commit/84cbd2c50e2c1e0ac90ee0afe3967c3fabdc9567))

`uv run ruff` resolved ruff from the repo's own dependencies, so a repo that treats ruff as a fleet
  tool rather than a project dependency failed to spawn the binary instead of linting. ruff now runs
  through uvx at the version its pre-commit hook pins; pytest is supplied with --with so a real test
  suite is never silently skipped; and mypy's guard tests for the dependency by import, since the
  [tool.mypy] section it used to look for is now in every repo.

Regenerated by `forge dies run maintenance/sync-ci.sh`.


## v6.0.1 (2026-07-31)

### Bug Fixes

- **ci**: Validate on push, not only via a release call
  ([`972355a`](https://github.com/datapointchris/syncer/commit/972355afc037e7b4c7581861b7c9a78a70912802))

The workflow triggered on pull_request and workflow_call. Development here is trunk-based, so the
  only trigger that ever fired was this repo's release pipeline calling it, and the checks ran as
  part of a release rather than as a gate on the push itself.

### Chores

- **config**: Adopt the standard pyright section
  ([`db86397`](https://github.com/datapointchris/syncer/commit/db86397b20ed56ef2f3b7bfa522e5d8cc475b573))

Synced from forge pyproject template. With no [tool.pyright] section the editor LSP settings
  applied, and their ignore = ["*"] suppressed every diagnostic. A config file takes precedence over
  those settings, so basedpyright now reports against the same "standard" mode as the rest of the
  portfolio instead of reporting nothing.

- **config**: Record the keys the pyproject sync owns
  ([`4c5fe1f`](https://github.com/datapointchris/syncer/commit/4c5fe1f6c66b8325a6d929f597dd042a3c443b4d))

forge now writes [tool.forge] managed, listing the exact keys the standard sets. Deletion on a later
  sync is scoped to that record, so dropping a key from the template retracts it here without having
  to guess which settings belong to this project.

Purely additive: nothing else in this file changed.

### Documentation

- Install from a real tag, not a ref named latest
  ([`96a3367`](https://github.com/datapointchris/syncer/commit/96a336700c204edec232e77f45fe408a7d105f5f))

`@latest` is not a git ref and this repo has none, so the documented install command fails outright.
  It also has to be a tag rather than the default branch: `update` reads uv's receipt and refuses to
  reinstall over a branch install, whose version cannot be compared against a release.


## v6.0.0 (2026-07-29)

### Features

- **config**: Create the registry and explain its path
  ([`cf747a0`](https://github.com/datapointchris/syncer/commit/cf747a02e2febc51fee7cf8abf5d4434cc5b415f))

A resolved registry path now carries why it was chosen, and every message about a missing one prints
  it plus both exits — create one there, or drop the pointer. Naming the path without the tier that
  chose it made an inherited repos_file look like a hard-coded path inside syncer, with no way to
  find the config that set it.

The tool-config template no longer recommends a fleet-specific registry path. Read as an
  instruction, it is how a machine that cannot have that path ended up pointed at it.

`config init` now writes the registry as well as the tool config, at the path syncer reads, and
  refuses the moment either file exists. Creating an absent file is not modifying shared
  infrastructure; the old scoping made scaffolding a manual redirect that printed a template with no
  path.

BREAKING CHANGE: `config example --registry` is now `config example registry`, and `init`/`example`
  take the same optional positional as `config path` (config, registry, or omitted for both). A
  boolean flag named after a noun answered "which file" in a grammar built for on/off.

### Breaking Changes

- **config**: `config example --registry` is now `config example registry`, and `init`/`example`
  take the same optional positional as `config path` (config, registry, or omitted for both). A
  boolean flag named after a noun answered "which file" in a grammar built for on/off.


## v5.3.0 (2026-07-29)

### Features

- **policy**: Report watched branches that exist only on the remote
  ([`2e8eb70`](https://github.com/datapointchris/syncer/commit/2e8eb70d6b08abb3ba5f83a43515c9a14c632833))

A fetch already brings down every branch on the remote, but the pipeline only iterates local
  branches — so a long-lived branch deliberately never checked out (develop/uat/prod) is invisible,
  and nothing tells you origin/prod moved.

watch_remote names fnmatch patterns to report on anyway, with each branch's age. Opt-in and empty by
  default: every repo has remote branches you will never care about, and a check that fires on all
  of them forever is one you learn to ignore.

Informational only. It never reaches decide() and never affects severity — there is no local branch
  to sync, and a repo is not unhealthy for having branches you deliberately do not keep.
  Deliberately does NOT create local tracking branches: a local copy you never check out is pinned
  at whenever you made it and silently serves stale history while looking like a normal branch,
  whereas origin/<branch> is current after any fetch and needs no maintenance.


## v5.2.0 (2026-07-29)

### Features

- **issues**: Flag a clone whose origin disagrees with the registry
  ([`1b140b0`](https://github.com/datapointchris/syncer/commit/1b140b096efc43bc24f2f709a05d92b56a568bf2))

~/code/refs/homelab pointed at datapointchris/homelab while the exemplar registry declared
  khuedoan/homelab — cloned in April, pulled ever since, undetected for 3.5 months. scikit-learn was
  the same. `gh repo clone <bare-name>` resolves to the authenticated user, so any reference repo
  that also exists under your own account silently gets your fork as origin, and searches read a
  stale duplicate of your own work as if it were reference material.

Nothing compared the two, so nothing could notice. origin_mismatch() normalises both sides to
  host/path before comparing — https, scp-style SSH and ssh:// with a port all reach the same repo,
  and flagging every SSH clone of an https registry entry would be noise that gets the check
  ignored.

Report-only: a deliberate fork with a legitimately different origin is indistinguishable from a
  mistake without asking, so the remote is never rewritten. Surfaced in `issues` alongside the other
  registry-vs-reality drift, and as a WARNING annotation on the default run — not a lifecycle
  status, which would replace the branch report for a repo whose only problem is where it points.

Immediately found a third instance the incident never caught: ~/code/refs/vue-core resolved to
  vuejs/vue-core, which does not exist; the repo is vuejs/core.


## v5.1.0 (2026-07-29)

### Features

- **policy**: Add a protected branch list enforced in execute
  ([`baeca59`](https://github.com/datapointchris/syncer/commit/baeca590c9e926a8b60ac3cca0ab87cd9c1f9264))

Protecting develop/uat/prod from an aggressive fallback like '*:ahead = push' relied on remembering
  an exact-name rule per branch; forget one and a stray local commit reaches a shared branch.
  `protected` is a list of fnmatch patterns on Policy, checked centrally in execute() before
  dispatch, so it is a hard guard rather than a function of rule ordering — matching how every other
  invariant in the executor works.

PROTECTED_ALLOWED is an allowlist, not a denylist, so an Action added later is refused on a
  protected branch by default. It holds only actions that provably neither publish local work nor
  lose it, which is why fast_forward stays permitted: advancing to what the upstream already
  contains does neither, and refusing it would make the setting useless for exactly the long-lived
  branches it exists to protect.

Machine-local, like every other policy setting — it lives on a Policy in config.toml, the portable
  registry has no policy fields, and no built-in sets one (locked by a test, so a "sensible default"
  cannot become a fleet-wide list).

Unlike the other execute-time gates, protection is static config and therefore knowable without
  running anything, so protection_refusal() is shared with the reporter: a report-only run marks
  what --apply would refuse instead of printing a `push` it will never make. The refusal is a
  WARNING rather than an ERROR, since it is the guard working as configured, not a failure.


## v5.0.0 (2026-07-29)

### Bug Fixes

- **policy**: Type the matrix roles explicitly for mypy
  ([`3669ba3`](https://github.com/datapointchris/syncer/commit/3669ba39238d63b21388f4fcfea6214e44716ba4))

**flags unpacking into BranchState is unverifiable by mypy, so ROLES carries (label, is_default,
  is_current) directly. Caught by CI, which type-checks the whole tree while the pre-commit hook
  sees only changed files.

### Documentation

- Cover the config and policy groups, and the XDG paths
  ([`0012a9a`](https://github.com/datapointchris/syncer/commit/0012a9a5991828aca440940b64b4df52be06474d))

README gains a from-scratch setup sequence and points at `syncer policy show` instead of
  hand-maintaining a rule table that would rot. Also catches up on what shipped in 4.3.1-4.6.1 and
  was never documented: fast_forward and the intents-not-mechanisms rule, owns_branch_naming,
  exclude_paths, git_timeout, per-registry event streams, and stats -c.

CLAUDE.md records the two invariants a future change would otherwise break: the templates are the
  single source for init and example, guarded by a round-trip test, and the decision matrix is
  computed from decide() rather than written down.

### Features

- **config**: Add the syncer config command group
  ([`c18bc87`](https://github.com/datapointchris/syncer/commit/c18bc8726c46060f9ca34caf3a76dccb553132fe))

syncer had no config tooling: `init` wrote one line, nothing validated a config, and nothing printed
  an example. Setting up a fresh machine meant reading the source. The group covers init, example,
  path, show, edit, and validate, with annotated TOML and JSON templates in config.py as the single
  source for both init and example — a round-trip test parses each into its model, which is what
  stops the examples drifting from the schema.

config init writes config.toml and never the registry: syncer never writes repos.json, which forge
  and indy also read. Scaffolding is `config example --registry > <path>`, so the invariant stays
  absolute.

validate checks structure and cross-references, including the one rule nothing enforced: a registry
  sync_policy hint must name a built-in, since config.toml is not synced between machines and a hint
  naming a machine-local policy silently degrades to `unknown policy` everywhere else. Whether repo
  paths exist on disk stays `syncer issues` — validate checks the files, issues checks reality.

Also fixes a broken config.toml surfacing as a raw pydantic traceback from whichever command loaded
  it first. Every load path now raises ConfigError and prints the failing key and why, and policies
  are built one at a time so the location names which policy the bad rule is in — pydantic's own
  error says only `rules`, which is no help in a file holding several.

BREAKING CHANGE: `syncer init` is now `syncer config init`, with no alias.

- **config**: Default the registry to the XDG config path
  ([`4287a22`](https://github.com/datapointchris/syncer/commit/4287a22398eb4cab7d14ad2a9c70c8506f2ae019))

With repos_file unset, resolution globbed ~/.config/syncer/*.json — picking up any stray JSON that
  happened to sit there — then hard-exited telling the user to write repos_file =
  "~/dev/repos.json". A fresh machine's first experience was an error naming one fleet's directory
  layout.

The default is now $XDG_CONFIG_HOME/syncer/repos.json, and the glob and its deprecation warning are
  deleted rather than kept alongside: once the exact filename in that directory is the default, the
  glob is redundant. The fleet's ~/dev/repos.json stays an explicit repos_file override, which is
  what it always was — sharing the registry with forge and indy is a fleet arrangement, not
  something that belongs in the tool's defaults.

BREAKING CHANGE: a machine relying on the ~/.config/syncer/*.json glob must name its registry in
  config.toml or move it to ~/.config/syncer/repos.json.

- **paths**: Move run history to XDG state, resolved via env
  ([`d0eb462`](https://github.com/datapointchris/syncer/commit/d0eb4622060443acafaf514f86358a8ce72a80a1))

Run history is state, not data: it persists across runs, nobody authors it, and deleting it changes
  behaviour rather than costing a recompute. DATA_DIR (~/.local/share/syncer) becomes STATE_DIR
  ($XDG_STATE_HOME/syncer), and both config and state homes now resolve through their environment
  variables rather than a hardcoded ~/.config.

adopt_legacy_events generalizes into migrate_legacy_events, which sweeps the old data dir for both
  shapes — the pre-split global stream and already-split per-registry ones — because a machine may
  have skipped the release that split them. Folded into this commit rather than a later one: moving
  the constant without the sweep ships a build that silently orphans every machine's history.

- **policy**: Add policy list and show with a computed decision matrix
  ([`ff5e091`](https://github.com/datapointchris/syncer/commit/ff5e09154c501ca13a978b3711eebf634fb05113))

Authoring a policy meant knowing every PrimaryState, every Action, and the four-tier selector
  precedence, none of which was reachable from the CLI.

`policy show` enumerates decide() over the full state taxonomy x the three branch roles rather than
  documenting it in prose, so the table cannot drift from what --apply will do — it is the decision
  function, not a description of one. That is only possible because decide() is pure. --branch
  re-evaluates for a real name, which is how you confirm `release/*:ahead` beats `*:ahead` before
  trusting --apply across a set of repos.

Both the matrix and its test iterate PrimaryState, so a state added later shows up the day it exists
  rather than the day someone remembers to document it.


## v4.6.1 (2026-07-27)

### Bug Fixes

- **issues**: Scope the master check to registries whose naming we control
  ([`b6080c2`](https://github.com/datapointchris/syncer/commit/b6080c209fa6bd7281aaac6352b2bede476b3e03))

is_ours gated the check on the repo owner matching the registry owner, which is true for a work
  registry — so all thirty repos would be flagged for defaulting to master, something the org
  decides and nobody here can change. Registries gain owns_branch_naming (default true) to turn the
  check off as a set.

is_fork also shelled out to gh unconditionally. gh cannot answer for a Bitbucket repo, so that was a
  subprocess per flagged repo that always returned false; it now short-circuits on the resolved
  clone URL.


## v4.6.0 (2026-07-27)

### Features

- **tracking**: Give each registry its own event stream
  ([`2f82c1f`](https://github.com/datapointchris/syncer/commit/2f82c1ffbf94f7982b573b5a72846e61e988076b))

Every run appended to one global events.jsonl regardless of which registry was in play, and stats
  had no --repos-file at all, so a second working set was impossible to report on. Worse,
  find_stale_repos scopes to the paths in the most recent run, so alternating a personal and a work
  run made each set's dirty-repo warnings disappear on the other's run.

Streams are now keyed on the registry file (<stem>-events.jsonl) and stats takes --repos-file, so it
  reports on the same set the sync ran against. The pre-split events.jsonl is adopted by the default
  registry with a rename — once, and never by a registry named with --repos-file, which never
  contributed to it.

events_file is a required argument on run_sync/show_stats rather than defaulting to a module
  constant; demo now writes to its own temp file instead of polluting real history.


## v4.5.0 (2026-07-27)

### Features

- **config**: Allow clone URLs the three-part path cannot express
  ([`ba657d9`](https://github.com/datapointchris/syncer/commit/ba657d9365c6e68ba2f9c709fb68a5d6f5ef410b))

Repo.url was hard-wired to '{host}/{owner}/{name}', with no .git suffix and no escape hatch. That
  covers GitHub and Bitbucket Cloud over HTTPS and nothing else: scp-style SSH
  (git@host:owner/repo.git) has no slash after the host, and Bitbucket Data Center wants both a /scm
  prefix and the .git suffix.

Registries gain url_template with {host}/{owner}/{name} placeholders, validated at load time so an
  unknown placeholder fails loudly instead of producing a URL that only breaks at clone time.
  Individual repos gain clone_url for the one entry that does not follow its host's convention. A
  registry is a single host, so the template belongs there rather than repeated on every entry.


## v4.4.1 (2026-07-27)

### Bug Fixes

- **repos**: Run git non-interactively with a timeout
  ([`14b481f`](https://github.com/datapointchris/syncer/commit/14b481f5808fad45f774d4de480bb33c7a2c4ddf))

Git prompts for credentials on /dev/tty, which capture_output does not redirect, so an expired
  credential or an unknown SSH host key would leave every worker thread blocked on the same terminal
  with nothing on screen explaining why. No git call had a timeout either, so a wedged connection
  held its thread forever.

All git and gh invocations now go through run_command: GIT_TERMINAL_PROMPT=0, -o BatchMode=yes
  appended to any configured GIT_SSH_COMMAND, and a timeout that comes back as an ordinary non-zero
  result rather than raising out of the worker.

git_timeout is machine-local config (default 120s, 600s for clones) because the headroom a fetch
  needs is a property of the box's network, not of the repo.


## v4.4.0 (2026-07-27)

### Features

- **policy**: Prove branch integration against a configurable merge target
  ([`7dc92e8`](https://github.com/datapointchris/syncer/commit/7dc92e8825677f6a84c27f853c40237b89c7c046))

delete_local checked ancestry against the repo's default branch, so a flow that merges feature
  branches into develop and only promotes to main at release time had every merged branch refused
  forever. Ancestry also cannot see a squash merge, which is the default on Bitbucket and GitHub, so
  even the right target was not enough on its own.

Policy gains merge_target (None = the repo's default branch), and integration is now proven by
  ancestry OR patch equivalence via git cherry. A multi-commit branch collapsed into one squash
  commit has no matching patch-ids and is still refused — false negatives cost a refusal, which is
  the safe direction.

execute() takes the policy so the guard can read merge_target live; it can never widen what an
  action may do. Deleting the merge target itself is refused explicitly, since contains_branch(x, x)
  is trivially true.

BranchState.merged_into_default becomes merged_into_target and classify resolves it against the same
  target, so the reported state agrees with the guard.


## v4.3.1 (2026-07-27)

### Bug Fixes

- **policy**: Add fast_forward so behind branches actually advance
  ([`de002e2`](https://github.com/datapointchris/syncer/commit/de002e2ddd7f2a57ae1d3bd31ccf135afc74c47c))

pull_ff (merge --ff-only) refuses unless the branch is checked out and ff_ref (update-ref) refuses
  unless it is not, so a rule naming either mechanism was refused for half of all checkout states.
  Both built-ins did exactly that: `default:behind = pull_ff` never ran unless the default branch
  happened to be current, and `*:behind = ff_ref` never ran when it was.

fast_forward names the intent and dispatches to whichever mechanism applies. Both delegates already
  re-verify strict ancestry and dirtiness themselves, so this adds no new primitive and the hard
  invariants are unchanged. pull_ff and ff_ref stay on the menu as explicit escape hatches.

### Build System

- **deps**: Require pyselfupdate 0.2.1
  ([`0f83970`](https://github.com/datapointchris/syncer/commit/0f83970014b9e7a4fb443ed8e48e0605ecca1362))

0.2.0's run_update fetched the changelog after uv had already rewritten the environment, and
  returned into typer rather than exiting. syncer survives that on the stdlib path, but a floor
  below the fix means a fresh install can still land on it.

### Continuous Integration

- Add generated validate.yml and gate release on it
  ([`19aef58`](https://github.com/datapointchris/syncer/commit/19aef58dbf7d5b71b5992c59256fe4daf04ff85a))

Release triggered on push to main with no validation at all, so it published whatever was on main.
  Adds the forge-generated CI block (ruff check, ruff format, mypy, pytest) and makes release depend
  on it.

Verified locally before wiring the gate: all four checks pass.

- Regenerate validate.yml at toolchain 6
  ([`49e3835`](https://github.com/datapointchris/syncer/commit/49e38358c59ca93637d487b0a3ce41c5899c283f))

Stamp only — the python block is unchanged. Toolchain 6 adds the pinned release-binary mechanism and
  the shell CI block.

### Refactoring

- **tracking**: Name the RepoStatus literal and use it in tests
  ([`a7b8ec4`](https://github.com/datapointchris/syncer/commit/a7b8ec46e7d748c85d5d99cc123db547fd537f5b))

The status literal was inlined in RepoSnapshot, so the test helper had to widen its parameter to str
  and mypy rejected passing it through. Naming the alias lets the helper declare the type it
  actually accepts.


## v4.3.0 (2026-07-27)

### Features

- Adopt pyselfupdate for update and add a daily notice
  ([`55a69ed`](https://github.com/datapointchris/syncer/commit/55a69edb05d0daad4ab045b31cb86b99245888e4))

Replaces a hand-rolled update that shelled out to `gh release view` and carried its own
  `fetch_github_changes` — a function that existed verbatim in relate as well, which is what made it
  worth a library rather than a copy. Both are gone; the library resolves the release, compares
  versions by semver rather than PEP 440, and reads uv's install receipt to refuse a checkout it
  should not replace.

Adds the once-a-day notice to the root callback, deferred to exit so it lands after the command's
  own output. It never raises and never prints an error; `syncer update` is the only place a failure
  surfaces.

httpx was only ever there for the changelog fetch, so it leaves with it.


## v4.2.0 (2026-07-25)

### Features

- **config**: Model the toolchain field on registry entries
  ([`fae9302`](https://github.com/datapointchris/syncer/commit/fae9302f9b851f2604a31b204da0528cbcfb2e53))

repos.json entries now carry a `toolchain` block declaring a repo's build surface — components (a
  stack and the dir it lives in) and sql_dialect. forge owns and consumes it to generate pre-commit
  configs and CI workflows; syncer neither reads nor validates the shape.

It is modelled as an opaque dict so the registry schema documents what is actually in the file, and
  so forge can extend it without touching syncer. Pydantic was already ignoring it silently, which
  left the docs claiming a purity the file no longer had.

The registry is no longer purely identity, and CLAUDE.md now says so. It lives here rather than in a
  forge-local file because a separate file would be keyed by repo name, and repo names are not
  unique across registries — that join already misattributed one repo's planning docs to another.
  The bar for anything added here stays: a portable fact about the repo itself, never machine-local
  state.


## v4.1.0 (2026-07-25)

### Documentation

- Correct init usage, add stats, add repo CLAUDE.md
  ([`b883d44`](https://github.com/datapointchris/syncer/commit/b883d44ea91417da9d5075b015c3dff24aa1f2c1))

Fix the stale 'syncer init name' example (init takes no arg and writes ~/.config/syncer/config.toml)
  and add the missing 'syncer stats' entry to the usage list. Add a CLAUDE.md documenting the
  pure/impure pipeline split, the two-file config model, and the execute-time safety invariants.

### Features

- Treat each repo registry as a separate set
  ([`2231148`](https://github.com/datapointchris/syncer/commit/2231148612b1e503854799dd82a19822392df838))

Moving the twenty exemplar clones out of repos.json into their own registry stopped anything from
  pulling them, and nothing said so. Three gaps combined to hide it.

--repos-file/-c did not exist: the registry came only from config.toml, so a second registry could
  not be run at all. `owner` and `host` were required, but an all-third-party registry has no single
  owner — each clone names its own.

The untracked scan only looked at direct children of each search path, so every repo one level
  deeper was invisible to it: ~/code/refs, ~/code/python-projects, ~/code/sql, ~/code/zmk. That
  check exists precisely to catch repos falling out of a registry, and it stayed silent while twenty
  did. It now recurses, matches on resolved path rather than name, and stops descending at a repo.

Recursing surfaced repos that are deliberately unregistered, so a registry can now disclaim a
  subtree with `exclude_paths`: ~/code/refs belongs to the exemplar registry, ~/code/1904labs to no
  personal registry at all.

The `using master` check now applies only to repos we own. Upstream's default branch is not
  something we can act on, so every exemplar clone would have reported it forever.


## v4.0.0 (2026-07-24)

### Chores

- **pre-commit**: Bump refurb to v2.3.1
  ([`7f28132`](https://github.com/datapointchris/syncer/commit/7f2813280f826015746f7441b10333aa326ecac9))

v2.1.0 crashes on newer mypy: AttributeError for allow_redefinition. v2.3.1 uses the renamed
  allow_redefinition_new attribute.

- **pre-commit**: Restrict hooks to pre-commit stage
  ([`835d7c4`](https://github.com/datapointchris/syncer/commit/835d7c4a92e112180e2fd020e0097d6d67b03a96))

Add default_stages: [pre-commit] so hooks without an explicit stages: run only at the pre-commit
  stage. Without it, unrestricted hooks (ruff, codespell, bandit, etc.) also ran at the
  prepare-commit-msg and commit-msg stages, firing multiple times per commit.

### Documentation

- **cli**: Add examples, --version flag, and richer help styling
  ([`16fad2d`](https://github.com/datapointchris/syncer/commit/16fad2d94344c29b5173a81b5f372850acf2f2af))

- Add an Examples block (epilog) covering the common workflows. - Add a top-level --version/-V eager
  flag alongside the version command. - Enable rich markup so key flags render emphasized in the
  description. - Order the Manage panel setup-first (init, version, update).

- **cli**: Group help into panels and sharpen descriptions
  ([`826e59b`](https://github.com/datapointchris/syncer/commit/826e59b5617c452bbd2105b8cfecc9ec83319726))

Organize commands into Sync / Inspect / Manage / Examples panels (Sync first) instead of a flat
  definition-ordered list, tighten the top description so it stops wrapping mid-phrase, and give
  issues/stats/init help text that says what they actually check or create.

### Features

- Add policy executor and 'branches --apply'
  ([`2049197`](https://github.com/datapointchris/syncer/commit/2049197e024c8c69e47e42ec95859c4bd10048d9))

Second slice of the sync_policy feature: the impure execute() half of the classify -> decide ->
  execute pipeline, plus an opt-in mutation surface. The default 'syncer branches' stays read-only;
  --apply runs each policy's decided action.

execute(action, state, repo) enforces the 7 hard invariants no policy can override, re-verifying
  every precondition live immediately before the write and refusing (never forcing) on failure: -
  never --force / -f / --force-with-lease - never mutate a dirty working tree - pull_ff / ff_ref
  require strict ancestry, re-checked at write time - rebase_push aborts cleanly on conflict ->
  refusal, never half-rebase - delete_local only under GONE and merged and not-current and
  not-default and clean - acts on the classified branch via explicit refspecs / ref names

repos.py gains branch-explicit primitives (merge_ff_only, update_ref, push_branch,
  delete_local_branch) that fix the decision/mutation branch mismatch — they never act on 'whatever
  is checked out'.

19 new tests: L3 effect + refusal per action, and L4 invariant sweep that spies on git argv to prove
  no --force* is ever issued, the dirty tree is never mutated, and a rebase conflict leaves a clean
  tree.

- Add sync-policy engine and per-branch report
  ([`390b2d7`](https://github.com/datapointchris/syncer/commit/390b2d7887076e7bd0803790cbd64120b7cdc4e9))

First implementation slice of the sync_policy / multi-branch feature (observe-only). Adds the
  classify -> decide pipeline; execute() is a later slice, so the existing sync_repos mutation path
  is untouched.

- policy.py: pure core — PrimaryState/Action/Scope enums, BranchState + Policy models, the pure
  decide() rules engine with deterministic selector precedence (exact > glob > default > current >
  *), and the built-in standard/observe/mirror policies. - classify.py: git -> BranchState per
  branch, after fetch --prune and git remote set-head origin --auto (fixes the stale-master
  resolution bug at its source). - report.py + 'syncer branches': read-only per-branch report
  showing the action each resolved policy would take. - config.py: optional sync_policy hint on
  RepoConfig; ToolConfig loading of [policies.*]/default_policy/[repo_overrides]; 5-level policy
  resolution. - repos.py: multi-branch git reads (branch_upstream, ahead_behind, is_merged_into,
  local_branches, fetch_prune, set_head_auto).

90 new tests: full L1 decide() truth table per policy, selector precedence + validation, and L2
  classify() fixtures for every state including the stale-origin/HEAD regression.

- Drive the default syncer run with the policy engine
  ([`b6e4696`](https://github.com/datapointchris/syncer/commit/b6e4696481b227929a5a34c44768c5e55c35b3de))

The default no-args run now uses the concurrent classify -> decide -> execute pipeline across all
  branches, replacing the old sequential default-branch-only loop (sync_repos is removed). It is
  report-first: 'syncer' shows what each policy would do, 'syncer --apply' executes the safe
  actions. --dry-run forces report-only even with --apply.

Output is ordered by attention — synced, then operations, then warnings, then errors — so repos
  needing action land at the bottom nearest the prompt (least scrolling). Repos are processed
  concurrently (default 16, -j to tune) as with 'branches'.

The default run and 'syncer branches' now share one core (report.gather_reports + render_report):
  the default run adds repo lifecycle (clone missing, flag not-git/no-remote/path-mismatch), event
  emission, and stale warnings via include_lifecycle=True.

events.jsonl gains per-branch detail additively: RepoSnapshot keeps every repo-level field (stats.py
  untouched) and adds an optional 'policy' and 'branches: list[BranchSnapshot]', both defaulting
  empty so older event lines still validate.

BREAKING CHANGE: 'syncer' with no flags no longer mutates; use 'syncer --apply' to pull/push/clone.

- Process repos concurrently in 'syncer branches'
  ([`d3c8a16`](https://github.com/datapointchris/syncer/commit/d3c8a1648fe9ad950f7b637d2bb6c33ff045f850))

Fetch and classify/apply repos on a thread pool (default 16, -j to tune) instead of sequentially.
  Git calls are I/O-bound and release the GIL, so a run over many repos takes roughly as long as the
  slowest repo rather than the sum — removing the friction of running it repeatedly.

- All git work runs in worker threads (each repo is independent); rendering stays on the main thread
  in path order via order-preserving pool.map, so concurrent output never interleaves. - A bounded
  per-task random jitter (<=0.3s) staggers the initial fetch burst so N fetches don't hit the remote
  at the same instant. Bounded per task, so there's no N*delay floor on large repo sets; the pool
  itself is the rolling queue for repos beyond the concurrency cap.

Adds the -j/--jobs flag. report_branches now returns via a thread-safe _build_repo_report worker + a
  RepoBranchReport dataclass.

### Refactoring

- **update**: Unify upgrade output and show changelog
  ([`fd71d8e`](https://github.com/datapointchris/syncer/commit/fd71d8e3d78587a1d407794ead12553dc400fc18))

Drop the "Updating X → Y" preamble and route all outcomes through consistent status glyphs: `✓
  syncer already at latest: <tag>`, `✓ syncer upgraded: <before> → <after>`, `✗ syncer upgrade
  failed: <reason>`. After a successful upgrade, fetch commit subjects between the old and new tags
  from GitHub's compare API and print them under "Changes:". Add httpx dependency for the GitHub
  call.

### Testing

- Add L5 regression for the master/main incident
  ([`47150cf`](https://github.com/datapointchris/syncer/commit/47150cfb90b0683875b5f57c157b9eba9c0c8fc5))

End-to-end reproduction of the 2026-07 incident that motivated the feature: a clone left on local
  master tracking a stale origin/master, origin/HEAD still pointing at it, origin/main ahead,
  untracked file present. Asserts classify_repo prunes the stale ref, repoints origin/HEAD, resolves
  the real default (main), and surfaces the orphaned master as GONE instead of a false 'synced'.

### Breaking Changes

- 'syncer' with no flags no longer mutates; use 'syncer --apply' to pull/push/clone.


## v3.1.0 (2026-04-13)

### Features

- Support per-repo owner for cloning reference repos
  ([`b1558c6`](https://github.com/datapointchris/syncer/commit/b1558c68166bff1efc6607a21350308e0c33424b))

RepoConfig now reads the optional owner field from repos.json. When set, it overrides the top-level
  owner for URL construction, enabling syncer to clone third-party repos (e.g. tiangolo/fastapi)
  instead of always assuming datapointchris/<name>.


## v3.0.0 (2026-04-05)

### Refactoring

- Rename doctor to issues, remove all writes to repos.json
  ([`4d56170`](https://github.com/datapointchris/syncer/commit/4d56170f5f0f6d6a9a3a604718ac87fbf8c3dd16))

repos.json is shared infrastructure — syncer reads it but no longer writes to it. Renamed doctor to
  issues (report-only, no --fix flag). Removed rename_master_to_main and helper methods (moved to
  forge die). Added description field to RepoConfig for forward compatibility.


## v2.0.0 (2026-04-03)

### Chores

- Deduplicate .planning gitignore entry
  ([`bcbf766`](https://github.com/datapointchris/syncer/commit/bcbf7668e069af03e8aeaeb0cb7a244c5f9db724))

### Features

- Read repos from external registry via config.toml
  ([`bf7b062`](https://github.com/datapointchris/syncer/commit/bf7b0625a66a07cdf8ef9a130218fab76f18c1e1))

Config now reads ~/.config/syncer/config.toml with repos_file pointing to ~/dev/repos.json instead
  of looking for JSON files in the config dir. Each repo has a status field
  (active/dormant/retired). Retired repos are filtered from sync operations. Legacy fallback with
  deprecation warning for migration.


## v1.7.1 (2026-03-31)

### Bug Fixes

- Hardcode package name in release build_command
  ([`470e45b`](https://github.com/datapointchris/syncer/commit/470e45b9da034aa02ed2801fc5f857503e3a7fad))

$PACKAGE_NAME is not set by python-semantic-release, so uv lock --upgrade-package was silently
  no-oping every release, leaving uv.lock out of sync. Also includes the stale uv.lock from the
  1.6.0 release.


## v1.7.0 (2026-03-31)

### Features

- Sort repos alphabetically by path for consistent output
  ([`25ca2f2`](https://github.com/datapointchris/syncer/commit/25ca2f213bcb273b8ff217ebf4e46b9d447d4291))

Repos are sorted at config load time so all commands (sync, doctor, stats) process them in the same
  order, grouped by parent directory.


## v1.6.0 (2026-03-31)

### Chores

- Add .planning to gitignore
  ([`8cd9faa`](https://github.com/datapointchris/syncer/commit/8cd9faa25a92d48d460a8904f7fa2153a4d72d58))

### Features

- Auto-sync diverged repos with pull --rebase then push
  ([`fbdf7de`](https://github.com/datapointchris/syncer/commit/fbdf7de3b0e44a882c69fbc84219dc6838afe9af))

When a repo has both unpushed and behind commits (with no uncommitted changes), attempt git pull
  --rebase followed by git push. On rebase conflict, abort and report for manual resolution.


## v1.5.3 (2026-03-09)

### Bug Fixes

- Exclude renamed repo paths from stale repo detection
  ([`1172595`](https://github.com/datapointchris/syncer/commit/1172595f2e509943d3286ee01037a40ecef34176))

The find_stale_repos function now filters candidates against the latest event's repo paths, so old
  paths from renamed or removed repos no longer appear as permanently stale.


## v1.5.2 (2026-03-01)

### Bug Fixes

- Exclude claimed paths from search fallback to prevent false path mismatch
  ([`0befb8a`](https://github.com/datapointchris/syncer/commit/0befb8a3b7f0c3baeb0c936924de5f764595ec7c))

When a repo's configured path didn't exist, find_repo_in_search_paths matched by directory name
  only, ignoring that another config entry already owned that path. This caused ichrisbirch-rust at
  ~/code/rust/ichrisbirch to falsely match the ichrisbirch webapp entry, reporting a spurious "path
  mismatch".

Pass claimed_paths (all configured repo paths) to find_repo_in_search_paths so it skips any
  candidate directory already owned by another entry. Applied in both sync_repos and doctor.


## v1.5.1 (2026-03-01)

### Bug Fixes

- Sync uv.lock with updated dependencies
  ([`93d7bd1`](https://github.com/datapointchris/syncer/commit/93d7bd1863e44cbfa14fe6679f12a6ad4b4cdf5b))

Updates virtualenv 20.37.0 -> 21.1.0 and adds python-discovery 1.1.0 as a new transitive dependency
  of virtualenv.


## v1.5.0 (2026-03-01)

### Features

- Show clone counts in sync summary bar
  ([`d880c02`](https://github.com/datapointchris/syncer/commit/d880c02c1213b24012622c29aa229a74926f487c))

Repos needing cloning were handled per-repo but invisible in the summary. Dry-run now shows "N to
  clone" and real runs show "N cloned".

- Add clonable/cloned counters to sync_repos - Append clone entries to summary_parts in correct
  order - Add cloned field to RunSummary (default 0 for backwards compat) - Show cloned count in
  stats recent runs display - Add missing-repo scenario to demo setup


## v1.4.1 (2026-02-17)

### Bug Fixes

- Align stats bar charts to longest repo path
  ([`b9c8ece`](https://github.com/datapointchris/syncer/commit/b9c8ece79d2138bd7ba9892d39c0f243695d3885))

Replaced hardcoded 30-character label width with dynamic sizing based on the longest label in each
  section (commits, repo age, frequently dirty). This prevents misalignment when repo paths exceed
  30 characters.

- Sync uv.lock with 1.4.0 release
  ([`66124c4`](https://github.com/datapointchris/syncer/commit/66124c4f00478233ae2f73972ba2d541cf7a6dc1))

This updates uv.lock to reflect the 1.4.0 version. Last manual sync needed since the build_command
  now handles this during releases.


## v1.4.0 (2026-02-17)

### Bug Fixes

- Update uv.lock during semantic-release
  ([`149a23f`](https://github.com/datapointchris/syncer/commit/149a23ff57f99bae7265eebb8e67b199dc7a31b7))

Add build_command to semantic_release config to run 'uv lock' during release commits, ensuring the
  lock file stays in sync with version bumps. Also updates uv.lock to current 1.3.1 version.

### Build System

- Use build(release) prefix for semantic-release commits
  ([`caa946f`](https://github.com/datapointchris/syncer/commit/caa946fdf97b469e553141c2199ba4e2f3edfd00))

Changes the commit message template for semantic-release from chore(release) to build(release) to
  better reflect that releases are build system changes.

### Continuous Integration

- Install uv in release workflow for build_command
  ([`608c465`](https://github.com/datapointchris/syncer/commit/608c465f73a755745496be3947cde8743f60d3d9))

The semantic-release build_command runs `uv lock`, which requires uv to be available in the CI
  environment. This adds the setup-uv action to install it before the release step.

- Install uv inside semantic-release build command
  ([`10bf654`](https://github.com/datapointchris/syncer/commit/10bf6540404f50ea77bac11e3dcb1d3a6e7ee6ef))

The semantic-release action runs in a Docker container, so the setup-uv step on the runner doesn't
  help. Changed build_command to install uv via pip inside the container before running uv lock.
  Removed the now-unnecessary setup-uv workflow step.

- Use recommended uv lock integration for semantic-release
  ([`e583e31`](https://github.com/datapointchris/syncer/commit/e583e31fd52e8e95953de174df42c003c1a0468c))

Updated the semantic-release build_command to follow the official uv integration guide: uses
  --upgrade-package to only update the package version in the lock (not all deps), and stages
  uv.lock for inclusion in the release commit.

### Features

- Add commit and repo age graphs, sort repos by activity
  ([`29fff61`](https://github.com/datapointchris/syncer/commit/29fff61113aa8a13835a0c94c2fa05a1defded53))

Adds two new bar chart visualizations to the stats command: - Commits by Repo: shows total commit
  count per repository - Repo Age: displays time since first commit with duration formatting

Also sorts the All Repos table by last active date (most recent first) and adds comprehensive test
  coverage for all new functionality.


## v1.3.1 (2026-02-17)

### Bug Fixes

- Skip update when already at latest release
  ([`a4aa784`](https://github.com/datapointchris/syncer/commit/a4aa7840556dfa0cdd476c930b33a9a74aba31d3))

The update command now compares the installed version against the latest GitHub release tag and
  skips reinstallation if already up to date, providing better user feedback about the current
  version status.

### Chores

- **release**: 1.3.1
  ([`8943168`](https://github.com/datapointchris/syncer/commit/8943168fd0e7a85ea17ca8ee5c68954b303a6a85))


## v1.3.0 (2026-02-17)

### Chores

- **release**: 1.3.0
  ([`747b73a`](https://github.com/datapointchris/syncer/commit/747b73a601bf429dc78f8ba9ea3d7de049a82539))

### Documentation

- Rewrite README for current architecture
  ([`7c49a2d`](https://github.com/datapointchris/syncer/commit/7c49a2dabe3904da5d128bdd1a24e289ef7c19a7))

The old README documented obsolete features (pipx install, create-release script, plugins, manual
  update workflow). This complete rewrite reflects the current tool:

- uv-based installation and updates - All CLI commands (sync, doctor, demo, init, version, update) -
  Config file format and location - Doctor auto-fix capabilities - Simplified troubleshooting
  (removed obsolete plugin notes)

The documentation now matches what the tool actually does as of v1.2.0.

### Features

- Add git stats properties to Repo class
  ([`46db748`](https://github.com/datapointchris/syncer/commit/46db7487e4314afab6667e5cf44951361a195037))

Add last_commit_date, first_commit_date, and total_commits properties to query git repository
  statistics on-demand. These complement existing status properties (ahead/behind) for tracking
  repository activity.

- Add syncer stats command
  ([`a96a4c0`](https://github.com/datapointchris/syncer/commit/a96a4c085d2a6829c737fac93e268367c16b1ad5))

Adds a comprehensive stats dashboard showing: - Summary of last 30 days (total runs, last run, avg
  issues) - Frequently dirty repos with visual bar charts - Stale repo warnings (uncommitted > 3
  days) - All repos table with live git stats - Recent run history (last 10 runs)

Includes stats.py module and full test coverage in test_stats.py.

- Add tracking data models and JSONL storage
  ([`f340dab`](https://github.com/datapointchris/syncer/commit/f340dabba775e920f8678859ce9ff6c7abe8b58a))

This introduces a tracking module to record sync run data for future analytics and stale repo
  detection. Adds DATA_DIR constant pointing to ~/.local/share/syncer/ for XDG-compliant data
  storage.

New models: - RepoSnapshot: captures repo state (status, branch, counts) - RunSummary: aggregates
  results of a sync run - SyncRunEvent: timestamped record of a complete sync operation

Storage functions emit/read events to JSONL file. Includes find_stale_repos() to identify repos with
  uncommitted changes persisting across multiple runs over a threshold period.

- Emit tracking events and warn about stale repos
  ([`7281df1`](https://github.com/datapointchris/syncer/commit/7281df1ab11421220650cba629e04c05c428d496))

sync_repos now builds RepoSnapshot for each repo (across all status paths), times the sync run, and
  emits a SyncRunEvent to JSONL after each non-dry-run sync. After emitting, it reads back events to
  detect and warn about repos with long-standing uncommitted changes.

main.py passes the resolved config name through to enable tracking.

### Testing

- Expand repos test suite and fix stale ref handling
  ([`6664be3`](https://github.com/datapointchris/syncer/commit/6664be38ce226ac41fc76055e93c374fea006efe))

Improve default_branch to validate that origin/HEAD tracking refs point to existing branches,
  falling back to local detection if the ref is stale (points to deleted branch).

Add rename_master_to_main method that handles partial states from previous attempts idempotently -
  checks each step (local rename, remote push, GitHub default, remote master deletion, origin/HEAD
  update) and only performs needed actions.

Expand test suite from 24 to 48 tests covering: - Display width calculations for icons and text -
  Status line formatting with/without branch names - Stale origin/HEAD ref handling and fallback
  logic - Rename idempotency in various partial states - Fork detection via gh CLI - Behind/unpushed
  commit tracking - Stash count


## v1.2.0 (2026-02-17)

### Chores

- **release**: 1.2.0
  ([`e8b41b1`](https://github.com/datapointchris/syncer/commit/e8b41b18fa4b11059165280b21d2ab4da6c452e0))

### Features

- Improve doctor output and add fork detection
  ([`9d22f01`](https://github.com/datapointchris/syncer/commit/9d22f01f8dcd5600841e12f41c2bf8c5344b0a6b))

Doctor now streams output as it goes with formatted status lines matching sync output. Config paths
  (e.g. ~/tools/syncer) are shown instead of repo names for clarity. Fork detection via gh repo view
  prevents attempting to rename master→main on forks. Warnings are yellow, local-changes errors are
  red. Replaced [dim] markup with [white] for readability.


## v1.1.0 (2026-02-17)

### Chores

- **release**: 1.1.0
  ([`6b0917b`](https://github.com/datapointchris/syncer/commit/6b0917bc2145bab87c0d57d7ac01d6609e6f7b85))

### Features

- Add nerd font icons, auto-pull/push, demo command, and doctor master detection
  ([`268d266`](https://github.com/datapointchris/syncer/commit/268d266fc115f89d873a908ea9d1602f5776a07a))

Enhances syncer with visual improvements and automation:

- Add nerd font icons (✓, ⚠, ✗, etc.) for better visual status - Column-aligned output with
  underscore padding for consistent layout - Auto-pull for repos cleanly behind remote (no local
  changes) - Auto-push for repos with unpushed commits (no uncommitted changes) - New `syncer demo`
  command that creates real temp git repos in various states - Doctor command now detects repos
  still on master branch with --fix to rename to main - Detailed file/commit listing shown under
  repos with issues - Summary line shows counts: synced, pulled, pushed, attention needed


## v1.0.0 (2026-02-17)

### Bug Fixes

- Add .profile back in and include poetry lock
  ([`3c02fbb`](https://github.com/datapointchris/syncer/commit/3c02fbb3fce014c79a4a9280c61bd6176c984295))

- Exiting in too far outer block for create-release
  ([`24a9f36`](https://github.com/datapointchris/syncer/commit/24a9f3685c529b24a599b1453ade91bd243ec43e))

- Github version command can now be run from any directory
  ([`3ff2fb1`](https://github.com/datapointchris/syncer/commit/3ff2fb19a5f9518739c9d8dbc9c7d51d6393cc4f))

- Handle case when target is exisiting directory
  ([`e7c22f1`](https://github.com/datapointchris/syncer/commit/e7c22f156aeeedeef9c9a443de66649b6dd0cd9c))

In the case that the target isn't a symlink already (meaning an update) then the target is renamed
  with suffix '_bak' and the symlink is created. Avoiding period '.' in the name since some symlinks
  are directories.

- Remove -a from git tag command
  ([`123cc43`](https://github.com/datapointchris/syncer/commit/123cc43e15d9ef06399cb0f4a40f39d8f511f173))

- Remove /etc/hosts symlink for permission errors
  ([`1984342`](https://github.com/datapointchris/syncer/commit/1984342852c170da6bb9316bca27cdfdc9dcd522))

- Remove zsh-autosuggestions from plugins sync, they were awful and distracting
  ([`a146965`](https://github.com/datapointchris/syncer/commit/a146965a4c67e68c156846ca71e2b0d2355c18df))

- Reverse string quotes in git tag for shell command
  ([`614f70a`](https://github.com/datapointchris/syncer/commit/614f70ac27cef3b9ab6842ab7b7ce54b69835093))

- Shell=true without split for subprocess.call
  ([`7f5bd09`](https://github.com/datapointchris/syncer/commit/7f5bd09d1f2a6240b4b05f4e136d7bc5bf898002))

- Subprocess.call does not need commands split
  ([`f1949a1`](https://github.com/datapointchris/syncer/commit/f1949a1a621755f8bf711f2bddf32d71a7700a39))

- Update github version command to return exact version
  ([`45c4e4a`](https://github.com/datapointchris/syncer/commit/45c4e4a2fb28f8ac080a0b5b65d9a1f75f3cca8d))

- Use root logger in main
  ([`36266a6`](https://github.com/datapointchris/syncer/commit/36266a6d44e53f672a604bd557b02ce868706d02))

- Use shell=True for git tag shell command
  ([`cc1e344`](https://github.com/datapointchris/syncer/commit/cc1e344209591c85aa73d06f96224b774e2dac99))

### Build System

- Create release 0.6.1 - Add logging when creating a release
  ([`e944b66`](https://github.com/datapointchris/syncer/commit/e944b663e99a7728f96557966195884a5db929dc))

- Create release 0.6.3 - Sync .profile dotfile
  ([`b221ead`](https://github.com/datapointchris/syncer/commit/b221ead4d8057eed44f95d5aaf82320b81a09bdd))

- Create release 0.6.4 - Get latest version functionality
  ([`ce2e916`](https://github.com/datapointchris/syncer/commit/ce2e916a6d8a394815ccd2cad475cc7908b6af7a))

- Create release 0.6.5 - Add --force to update command
  ([`da62d51`](https://github.com/datapointchris/syncer/commit/da62d511a01d195369de81cbc81f82deab2d0dc3))

- Create release 0.7.1 - Update dependencies
  ([`4c1593d`](https://github.com/datapointchris/syncer/commit/4c1593d88a968e4b1164fe40e3d3386640c36fef))

- Create release 0.8.0 - Add help text
  ([`5914174`](https://github.com/datapointchris/syncer/commit/591417455093b30e2cddf36134c43c16b294e126))

- Create release 0.8.1 - Remove old github projects
  ([`5eef84a`](https://github.com/datapointchris/syncer/commit/5eef84a58b051f55fa3faf6fe98723ac4da0aeb1))

- Create release 0.8.2 - Remove misc projects
  ([`5125b9b`](https://github.com/datapointchris/syncer/commit/5125b9b3ac467074fe7c4954929ed3487f91a00e))

- Create release 0.8.4 - Minor fix: poetry lock update and re-adding .profile
  ([`17a74d6`](https://github.com/datapointchris/syncer/commit/17a74d6d08b77bc43f227a1d619c6c53b657cfec))

- Create release 0.9.0 - streamlined create-release
  ([`e89e153`](https://github.com/datapointchris/syncer/commit/e89e153a69ea10cf9ba1c5e112d0492db36ee455))

- Create release 0.9.1 - run update from any local directory
  ([`c5a6726`](https://github.com/datapointchris/syncer/commit/c5a6726dacef1a7b65838b4eb3c429afe456abba))

- Create release 0.9.2 - Add chatter to repos
  ([`c9359f1`](https://github.com/datapointchris/syncer/commit/c9359f1b18c118e25844533796b3117de477c0d7))

- Create release 0.9.3 - Move applicable dotfile configs to XDG config dir
  ([`dae320e`](https://github.com/datapointchris/syncer/commit/dae320effddec1b48f210c918add56208ed2de7f))

- Create release 0.9.4 - Handle existing directories in dotfiles
  ([`2d7852f`](https://github.com/datapointchris/syncer/commit/2d7852f08a89a0698a73761dc47ade7f797b8f23))

- Create release 0.9.5 - Add youtube-playlists to synced repos
  ([`ef4cb05`](https://github.com/datapointchris/syncer/commit/ef4cb056b0c2a2b8200db883d5c3863a733c2f6b))

- Create release 0.9.6 - Add more dotfiles to sync
  ([`e945a1f`](https://github.com/datapointchris/syncer/commit/e945a1f9ab83be767b8f5e5169413c4400ddc50b))

- Create release 0.9.7 - Add /etc/hosts to symlinks
  ([`67c225f`](https://github.com/datapointchris/syncer/commit/67c225f33ca6738de435abfbdd83fd80d4d4680a))

- Create release 0.9.8 - Remove /etc/hosts symlink
  ([`e545374`](https://github.com/datapointchris/syncer/commit/e545374d42c77a83a147fb4710f388e0bf154615))

- Create release 0.9.9 - Remove ichrisbirch from synced bin
  ([`ec28544`](https://github.com/datapointchris/syncer/commit/ec28544be1afb86841faeba8c4aadeb510f84da5))

- Update dependencies
  ([`28047c2`](https://github.com/datapointchris/syncer/commit/28047c2fd237d7ffeb88fcc815892ebcee22c427))

### Chores

- Move data folder inside package to be included with install
  ([`57e72e7`](https://github.com/datapointchris/syncer/commit/57e72e7624d02a6b25a00fd1f8042f64b0a8828b))

- Remove dead python projects
  ([`71bfc7b`](https://github.com/datapointchris/syncer/commit/71bfc7bd5803f981c3aa18c4c5e4b40e8e8f080c))

- Remove unused main command
  ([`42282fa`](https://github.com/datapointchris/syncer/commit/42282faae2bda08a876b9fcc1d2eb914a5745319))

- Update deps
  ([`e442ffe`](https://github.com/datapointchris/syncer/commit/e442ffe40db90b7a0cbcbb80e7db0e55e2b1bcc9))

- Update gitignore to remove __pycache__
  ([`875e577`](https://github.com/datapointchris/syncer/commit/875e57713a739c0c9abc9e1ce8592f2cffc49a56))

- Update python version
  ([`e1f9ac9`](https://github.com/datapointchris/syncer/commit/e1f9ac948c69978950700de6fcddf427fada591e))

- **release**: 1.0.0
  ([`94ec373`](https://github.com/datapointchris/syncer/commit/94ec373337e8e2c669af4d484ad9a5871089216b))

### Documentation

- Add auto generated docs to README.md
  ([`7258f2e`](https://github.com/datapointchris/syncer/commit/7258f2e9bd2b4ea7115a258ebe545e504c598e52))

- Add warning for running syncer plugins inside of tmux
  ([`cd1cf28`](https://github.com/datapointchris/syncer/commit/cd1cf28695b72f5835a630cb24f7557c5de0134d))

- Correct readme with push instructions
  ([`bd9b6ad`](https://github.com/datapointchris/syncer/commit/bd9b6adfa0b72b4f2c19f1fe63e15a33105ecf99))

- Update README with install and update instructions
  ([`fed9e6b`](https://github.com/datapointchris/syncer/commit/fed9e6b5acd8ffbee5d49f6f91b5cdd5908f8c1a))

### Features

- Add --force flag to update command
  ([`b8e36b6`](https://github.com/datapointchris/syncer/commit/b8e36b6ea23ee6a333259e88765db1272f1b01f2))

- Add .profile to universal dotfiles to sync for rust installation
  ([`03318a4`](https://github.com/datapointchris/syncer/commit/03318a46d747581f9359bdc6a44d1a8fa30fa505))

- Add /etc/hosts symlink for macos
  ([`79414b6`](https://github.com/datapointchris/syncer/commit/79414b67523ef6950bf6da3f5cfdeab9c5050d30))

- Add 1904labs projects to sync
  ([`917402f`](https://github.com/datapointchris/syncer/commit/917402f6d48ced4e098176eb252e1b13f0e5c7f9))

- Add aerospace, eza, zellij to synced dotfiles
  ([`0b72ec7`](https://github.com/datapointchris/syncer/commit/0b72ec7d7cf587e44fafddae23b8b1acc00f0623))

- Add capability to check for main branch instead of master branch
  ([`03b10eb`](https://github.com/datapointchris/syncer/commit/03b10eb693bd9d950ecbb384450c4674c96343b7))

- Add chatter to repo list
  ([`841939d`](https://github.com/datapointchris/syncer/commit/841939d82dc0187bd5979263630f1ccafe127883))

- Add commit to create-release and inline logging for Repo
  ([`848c4de`](https://github.com/datapointchris/syncer/commit/848c4de5cf6a283e88a80fce01f5c7234439267c))

- Add extra help text to syncer for install and update
  ([`24b52a8`](https://github.com/datapointchris/syncer/commit/24b52a85c9ca4fed48547688c5c16b2805d611bd))

- Add logging to create_release
  ([`e244bbc`](https://github.com/datapointchris/syncer/commit/e244bbcc3bf54806ecc49aa533378138269c7b57))

- Add plugin type to plugins sync
  ([`a948e36`](https://github.com/datapointchris/syncer/commit/a948e36b14efd2e1efb540c2ce63fefb14429378))

- Add readme command to syncer to display help text
  ([`b1b4625`](https://github.com/datapointchris/syncer/commit/b1b4625c36ad5a86032bf5bbb81ed76c687b3d1b))

- Add repo type to repos sync
  ([`94e3dd7`](https://github.com/datapointchris/syncer/commit/94e3dd7b1a624fc8faac24b3ca3c6556915b68e2))

- Add spacing and use long form of --quiet for pip install
  ([`d5a95e1`](https://github.com/datapointchris/syncer/commit/d5a95e14f91c2f7d9123f9c384e0d65b722d76bf))

- Add testpaths command for testing pathlib
  ([`0907a56`](https://github.com/datapointchris/syncer/commit/0907a567429dc6e04bcee79c238879e82922bec9))

- Add update command to update the user installed syncer package
  ([`9156544`](https://github.com/datapointchris/syncer/commit/91565444b1f2348e7eee11fffdce1b214d1463df))

- Add version command
  ([`3ebce5b`](https://github.com/datapointchris/syncer/commit/3ebce5bdc898152589205f6706ce37db4cc6821c))

- Add youtube-playlists to synced repos
  ([`645b8e2`](https://github.com/datapointchris/syncer/commit/645b8e2cc551f33ba9408cad9072d05fa4a403e0))

- Create convert_readme_to_help_text for adding readme help to command line
  ([`8a8c5a6`](https://github.com/datapointchris/syncer/commit/8a8c5a6d4d5902c841175f97b174d34010cb1065))

- Create create-release function
  ([`ce844e1`](https://github.com/datapointchris/syncer/commit/ce844e1bba41b5c2b27ac32e07e23e8874b858f2))

- Move some dotfiles into XDG_CONFIG_HOME (~.config)
  ([`35df261`](https://github.com/datapointchris/syncer/commit/35df2611f0336c7c11bc9f471b9aebae0194e35c))

- Redesign syncer as repo-sync-only tool with uv build system
  ([`d6126f8`](https://github.com/datapointchris/syncer/commit/d6126f84d5dc2b50a19019f64a8eab238ff9ef71))

Complete redesign focusing on repository synchronization:

Build system changes: - Migrated from Poetry to uv package manager - Adopted src/ layout
  (src/syncer/ instead of syncer/) - Updated to Python 3.13 - Added GitHub Actions release workflow
  with python-semantic-release

Config system rewrite: - Moved to JSON-based config at ~/.config/syncer/ - Added auto-detection for
  GitHub repos - New 'doctor' command for diagnostics - New 'init' command for setup

Core functionality changes: - Rewrote repo sync using subprocess.run with cwd= (removed os.chdir) -
  Replaced colorama with rich for better output - Rewrote CLI with sync/doctor/version/update/init
  commands - Added comprehensive test suite (24 passing tests)

Removed features: - Deleted dotfiles sync (breaking change) - Deleted plugins system (breaking
  change) - Removed create_release, update, utilities, testpaths modules

Infrastructure updates: - Updated pre-commit config for uv workflow - Added .python-version,
  .shellcheckrc - Cleaned up .gitignore for new structure

- Remove deactivate from update function, did not work
  ([`6c46ebc`](https://github.com/datapointchris/syncer/commit/6c46ebc5ba8bc40e7e235d19515ac0f7c3803ffd))

- Remove ichrisbirch from synced bin
  ([`fc1881c`](https://github.com/datapointchris/syncer/commit/fc1881c4f0c457c1b5e9c52df589f8661a33d91e))

- Separate dotfiles config and add syncer config
  ([`520f544`](https://github.com/datapointchris/syncer/commit/520f54486784779755aa2ece27bb0025574fa6b9))

- Update and remove old projects
  ([`4960a2d`](https://github.com/datapointchris/syncer/commit/4960a2d3e353f32d6f4f03e9213430b43019d730))

- Update project to work in any directory
  ([`6154487`](https://github.com/datapointchris/syncer/commit/6154487f8d1d7cd1acb15ea391881fe84265885a))

- Upgrade update to use github release instead of manual install of wheel
  ([`903008c`](https://github.com/datapointchris/syncer/commit/903008cddbfe5b7f9ca0bf78e5be2bc944ceee21))

- Use dynamic wheel path instead of hardcoded version
  ([`22ebf6b`](https://github.com/datapointchris/syncer/commit/22ebf6bda2ad7929e9e1f9bfbb57e3a841c58c1d))

### Refactoring

- Change pathlib.Path to Path
  ([`f62a514`](https://github.com/datapointchris/syncer/commit/f62a514fe70eb0215a8893c101ee8c9a10f5cc28))

- Create repo class to encapsulate state
  ([`b4b826f`](https://github.com/datapointchris/syncer/commit/b4b826f0ef77be425c1137ff142e19c970f3b280))

- Get latest version functionality to separate function
  ([`9a2d565`](https://github.com/datapointchris/syncer/commit/9a2d565b21cc911413bbc0b3b7095680691eb2ee))

- Move tmux plugins into XDG_DATA_DIRECTORY
  ([`85c7597`](https://github.com/datapointchris/syncer/commit/85c7597abe7b57050de3252fee809c6789bed217))

- Remove source and target config subclasses
  ([`6d6a7bf`](https://github.com/datapointchris/syncer/commit/6d6a7bfce0c1abc317299917261130571743a98d))

- Rename projects to repos
  ([`d27f875`](https://github.com/datapointchris/syncer/commit/d27f875e8a6cdf60ae572cb76140b0b7eb4f2245))

- Restructure 1904labs repos
  ([`dd50894`](https://github.com/datapointchris/syncer/commit/dd508942d6bfb258ad98d9d1cc4e62c4bd268403))

- Restructure datapointchris repos
  ([`7b520b2`](https://github.com/datapointchris/syncer/commit/7b520b201ab622c6468756306228d567a6f96b1b))

- Split plugins and projects by type
  ([`27a9166`](https://github.com/datapointchris/syncer/commit/27a9166bf4a8bc41522e7566a3fd88b230fc1869))
