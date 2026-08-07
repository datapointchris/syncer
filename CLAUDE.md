# syncer

A CLI that checks whether local git repos are synced before you switch machines, and
optionally performs the safe sync actions. `check` reports and never writes; `apply` mutates.

This file covers the architecture and the conventions specific to this repo. Universal
rules (commits, git safety, Python style) live in `~/.claude/CLAUDE.md` — not restated here.

## The pipeline: pure core, impure edges

The whole design is a four-stage pipeline split so the decision logic is a pure function and
therefore exhaustively testable without touching git:

| Stage | Module | Purity | Owns |
| --- | --- | --- | --- |
| Classify | `classify.py` | impure (git reads) | Turns real repo state into `BranchState` objects. Runs the read-side remediation (`fetch --prune` + `git remote set-head origin --auto`) so a renamed default resolves before anything is classified. |
| Decide | `policy.py` | **pure** | `decide(state, policy) -> Action`. No git, no FS. A `BranchState` × a `Policy` maps to one action off a pre-vetted safe menu. Also holds the built-in policies. |
| Execute | `execute.py` | impure (git writes) | The only place that mutates. Enforces the hard invariants (below) and refuses rather than forces. |
| Report | `report.py` | impure (concurrency + render) | Runs classify→decide→(execute) per repo on a thread pool, sorts by attention, renders. |

`decide()` being pure is why `tests/test_policy.py` can enumerate the full cartesian product
of primary-states × policies as a no-git truth table. Keep git and FS I/O out of `policy.py`
— that boundary is load-bearing, not incidental.

## A failed git call is never a state

Every accessor on `Repo` used to convert a non-zero exit into a benign-looking value — `[]`
branches, `(0, 0)` counts, a clean tree — and the results were indistinguishable from real
answers. `(0, 0)` in particular is `SYNCED`, so a repo whose fetch died reported as fully in
sync: the exact opposite of the one question this tool exists to answer, stated confidently.

Three rules follow, and none of them is optional:

- **`Repo._git` records.** A non-zero exit appends a `GitFailure` (argv, returncode, stderr) to
  `repo.failures` unless the call passes `probe=True`. Recording is the default so a git call
  added later is visible without anyone remembering to opt in — the same direction
  `PROTECTED_ALLOWED` takes. `probe=True` is for the handful of calls that ask a yes/no question
  (`merge-base --is-ancestor`, the `rev-parse --verify` probes, `git cherry`), where non-zero
  *is* the answer; adding one anywhere else needs an argument, not a habit.
- **An unmeasurable repo is an error, not a state.** `refresh_remote()` returns the fetch's
  failure, and `_build_repo_report` returns a `_failed_report` **before** `build_branch_rows` —
  which is also what refuses execution, since no `execute()` call is ever constructed. Such a
  report carries **zero rows** on purpose: a row is a claim about a branch, and the whole point
  is that no such claim can be made. There is deliberately no `PrimaryState.UNKNOWN` — every
  policy's rule for it would be `report` forever, and a state name could never carry git's
  stderr, which is the only thing that makes the failure actionable.
- **Unknown resolves toward refusal.** `ahead_behind` returns `None` rather than `(0, 0)`;
  `remotes()` returns `None` (cannot ask) distinctly from `[]` (none configured), because
  reporting the first as `no remote` sends you to fix something that is fine. Every
  `execute.py` guard treats "cannot verify" as "do not proceed".

`decide()` depends only on the primary state plus the branch's role/name. The `dirty`/`stashed`
modifiers are **execute-time gates, never decision inputs** — `decide()` is invariant to them
(asserted in `TestDecideModifierInvariance`). `protected` is the same kind of gate and is likewise
invisible to `decide()`.

A gate the reporter can evaluate without executing must still be *shown*, though, or the report
promises an action `apply` never makes. Two are: `protection_refusal()` (static config) and
`dirty_refusal()` (one `git status` per repo). Both live in `execute.py` beside the guards they
mirror, both feed `BranchRow.blocked`, and `TestDirtyRefusalMatchesExecute` asserts the mirror
agrees with `execute()` for every action on the menu — a mirror that drifts is worse than none,
because the arrow is the only part of a row anyone acts on. A gate that can only be discovered
mid-write (a rebase conflict, unreadable counts) stays out of `blocked` deliberately.

## Severity is ownership, not git state

`Severity` orders rows by **who has to do something**, which is not the same as how unusual the
branch state is, and conflating the two is what made a 75-repo report unreadable: 32 repos were
counted as needing attention, 30 of them rendered as a green tick reading `synced, dirty → skip`,
and the one repo syncer could actually fix (`1 behind`) was painted the same yellow as the ones
needing hands. The count came from `_row_severity`; the icon and colour came from the primary
state alone. Two notions of "needs attention" in one report means neither gets trusted.

- **`OPERATION` applies in `check`, not just `apply`** — an action is decided and nothing would
  refuse it, so `apply` clears it without you. `behind → fast_forward` is queued work, not damage.
- **A dirty tree outranks the action band**, whatever the branch state: it is the one thing syncer
  will never resolve, and it refuses every mutator that touches the tree.
- **`_row_severity` is checked before the state's own severity**, so `MUTATING_ACTIONS` (derived
  from `_MUTATORS`, never listed twice) is what defines "syncer will handle this".
- **Icon from the state, colour from the severity.** Only the benign tick can understate a
  severity, so it is the only icon `_SEVERITY_ICON` substitutes.
- **The action arrow renders only for a mutating action.** `skip`/`report`/`prompt` all mean
  "syncer changes nothing", which the row conveys by existing — `→ skip` after every clean repo
  spent a column on the least informative word in the vocabulary.

The summary line splits the same way (`N to sync` in cyan vs `N need you` in yellow), and
`RepoStatus` gained `pending` for it, because a `check` run recording `pulled` would write a
mutation that never happened into the history `stats` reads back as fact.

## Two surfaces, one core

`run_sync` (`sync.py`) and `report_branches` (`report.py`, `--per-branch`) share
`gather_reports` + `render_report`. The difference is a single `include_lifecycle` flag,
which is why the CLI exposes it as an option on both verbs rather than a fourth command:

- **default run** (`include_lifecycle=True`): also clones missing repos under `apply`, flags
  moved/not-git/no-remote repos, emits a run event, prints a summary line, and warns about repos
  left dirty for days.
- **`--per-branch`** (`include_lifecycle=False`): pure per-branch view, skips non-git repos, no events.

There is no second sync path — the old default-branch `sync_repos` loop was deleted, not kept
alongside. If you're tempted to special-case the default run, add it behind `include_lifecycle`.

## Safety invariants (execute.py)

`apply` is safe by construction. `execute()` re-verifies **every** precondition live,
immediately before each write — it never trusts the (possibly stale) `BranchState` from classify
time — and refuses rather than forces. Guaranteed independent of any policy:

1. Never `--force`/`-f`/`--force-with-lease` (no such argv is ever constructed).
2. Never mutate a branch whose working tree is dirty — **or whose cleanliness cannot be
   verified**. Gates call `repo.is_dirty`, which answers `True` when `git status` itself fails.
   The old `uncommitted_changes` returned `[]` on failure, so callers testing its truthiness
   read a broken git as a clean tree, i.e. as permission to mutate. Anything that gates a write
   on a git read needs that polarity: a `list | None` cannot express it, because `None` is falsy.
   `_ff_ref` is the sole exemption (`_WORKTREE_SAFE`): `update-ref` moves a ref that is not
   checked out, so no tree is read or written. **syncer never resolves a dirty tree** — there is
   no commit or stash action and there will not be one; capturing your uncommitted work is a
   decision the tool has no standing to make. It reports the tree and refuses everything that
   would touch it.
3. `fast_forward`/`pull_ff`/`ff_ref` require strict ancestry (upstream strictly ahead), re-checked
   at write time.
4. `rebase_push` aborts on conflict and downgrades to a refusal — never a half-rebase.
5. `delete_local` only under the full `GONE ∧ integrated ∧ ¬current ∧ ¬default ∧ ¬merge-target ∧
   clean` guard. Uses `branch -D` (not `-d`) because a GONE branch has no upstream for git's own
   merged-heuristic to consult — safety comes from our explicit guard, not git's. *Integrated*
   means the target provably holds the work, by ancestry **or** patch equivalence (`git cherry`),
   never inferred from the remote branch having been deleted — a branch deleted without merging
   must still be refused. The target is `policy.merge_target`, defaulting to the repo's default
   branch; a develop-centric flow never makes a feature branch an ancestor of `main`, so
   without this the guard refuses every genuinely merged branch forever.
6. Any precondition that fails at execute time is refused and reported, never forced.
7. Actions use explicit refspecs / ref names, so they always act on the classified branch, never
   the incidentally-checked-out one.
8. A branch matching the policy's `protected` patterns admits no action that publishes or
   destroys. Checked centrally in `execute()` **before dispatch**, so it covers every action
   including ones added later. `PROTECTED_ALLOWED` (`policy.py`) is an **allowlist** — a new
   `Action` is refused on a protected branch by default, which is the safe direction. Only
   actions that provably neither publish local work nor lose it belong in it; `fast_forward`
   does (it advances to what the upstream already contains), `push`/`rebase_push`/
   `set_upstream_push`/`delete_local` do not. `protected` lives on `Policy`, so it is
   machine-local like every other policy setting, and no built-in sets one.

Any new `Action` must be added to the `Action` enum, mapped in `_MUTATORS`, and given a mutator
that re-checks its own preconditions live. Never add an unsafe primitive to the menu.

**Rules name intents, not mechanisms.** `pull_ff` (`merge --ff-only`) needs the branch checked
out and `ff_ref` (`update-ref`) needs it not checked out, so a rule naming either one is refused
for half of all checkout states — which is what `default:behind = pull_ff` and `*:behind = ff_ref`
silently did in both built-ins. `fast_forward` dispatches to whichever applies; the mechanisms stay
on the menu as explicit escape hatches. `TestBuiltinsNameIntentNotMechanism` locks this: no
built-in may ever decide `pull_ff` or `ff_ref`. Any future action with the same
current/non-current split needs the same treatment.

## Config: two files, deliberately split

- **`repos.json`** (default `$XDG_CONFIG_HOME/syncer/repos.json`, repointed by `repos_file` in
  `config.toml`) — the repo **identity registry**: `owner`, `host`, `search_paths`,
  `exclude_paths`, and per-repo `{name, path, status, description, owner?, sync_policy?,
  toolchain?}`. Portable across machines. **syncer never modifies one that holds repos** — it's
  shared infrastructure (also read by `forge` and `indy`). `issues` reports drift but tells you to
  fix paths by hand. Exactly two commands write, and both only *create*: `config init` scaffolds
  one that is absent, and `config scan --write` fills one that is absent or still empty. Both
  refuse the moment the file lists repos, and `scan` treats an unparsable file as content too —
  the last one to clobber silently. A tool that can only tell you to hand-write a file whose shape
  it already knows, from data already on disk, has pushed its own job onto the reader.

  The fleet keeps its registry at `~/dev/repos.json` and points `repos_file` there, because forge
  and indy read the same file. That sharing is a **fleet fact, not a syncer fact** — the default
  must stay a syncer-owned XDG path so a machine that has never heard of `~/dev` still works.
  Nothing shipped in this repo may *recommend* it either: the tool-config template used to say the
  registry "lives at ~/dev/repos.json and every machine names it here", which reads as an
  instruction, and a fleet that then deployed exactly that `config.toml` from a shared dotfiles
  bucket pointed the work box — a git-only WSL node with no Syncthing, so no `~/dev` — at a
  registry it could never have. `config.toml` is machine-local in the operational sense, not just
  by convention: `repos_file` is the one setting whose correct value differs per machine and whose
  wrong value fails every run outright rather than degrading, so that file must never be synced.

  Every resolved registry path therefore carries its provenance (`RegistryLocation.source`), and
  every message about a missing registry prints it plus both exits (create one there, or drop the
  pointer). That failure was undiagnosable from syncer's own output, which named the path but never
  what chose it — so the tool looked like it had `~/dev` hard-coded. A resolution chain owes the
  reader which tier won.

  **It is no longer purely identity.** `toolchain` declares a repo's build surface — `components`
  (a `stack` and the `dir` it lives in) and `sql_dialect` — and is owned entirely by forge, which
  generates pre-commit configs and CI workflows from it. syncer models it as an opaque dict and
  never reads it. It lives here rather than in a forge-local file because a separate file would have
  to be keyed by repo name, and repo names are not unique across registries — that exact join
  already misattributed one repo's planning docs to another. Attaching the data to the entry avoids
  the join. Anything added here must be a **portable fact about the repo itself**, never
  machine-local state; that still belongs in `config.toml`.

  Clone URLs resolve as per-repo `clone_url` → registry `url_template` (`{host}`/`{owner}`/`{name}`)
  → the default `{host}/{owner}/{name}`. The template exists because that default path cannot
  express scp-style SSH (no slash after the host) or a required `.git` suffix, and a registry is
  one host — so it belongs on the registry, not repeated on every entry. `url_template` is
  validated at load time; an unknown placeholder fails loudly rather than producing a broken URL.

  **A registry is a self-contained set.** `--repos-file/-c` swaps the entire working set; it never
  merges with the default. `owner` and `host` are optional so an all-third-party registry works —
  `~/dev/exemplar-repos.json` holds twenty upstream clones that each name their own owner. Repos
  whose owner isn't the registry owner are treated as not ours: the `using master` check is skipped
  for them, because upstream's default branch is not something we can act on. `owns_branch_naming`
  (default `true`) turns that check off for a whole registry — at a company the default branch is
  the org's decision, so the check would fire on every repo forever with nothing to do about any of
  them. `is_fork` short-circuits off GitHub, since `gh` cannot answer for a Bitbucket repo and
  asking is a subprocess per repo that always says no.

  `exclude_paths` lets a registry disclaim a subtree inside its own `search_paths`, so the subtree
  is never reported as untracked. `~/code/refs` belongs to the exemplar registry and `~/code/1904labs`
  belongs to no personal registry at all.
- **`config.toml`** (`$XDG_CONFIG_HOME/syncer/config.toml`) — **machine-local** tool config:
  `repos_file` pointer, `default_policy`, custom `[policies.*]`, `[repo_overrides]`, and
  `git_timeout`.

**Scaffolding and teaching are separate, on purpose.** `config.py` holds two pairs: `STARTER_*`,
which `config init` **writes**, and `TEMPLATE_*`, which `config example` **prints**.

They used to be one pair, on the reasoning that the annotated example a user reads should be
byte-for-byte the file `init` writes. The cost was observed, not theoretical: `init` shipped a
`[policies.laptop]` block that `policy list` renders indistinguishably from a built-in, a
`[repo_overrides]` entry for a repo nobody has, and three fake repos — so the very first `syncer check`
run printed three `would clone` lines for repos that never existed. **A scaffold must have nothing
in it to delete**; a reference must have everything. Those are different documents.

`STARTER_TOOL_CONFIG` is three lines and deliberately contains no `repos_file`, *not even
commented out* — it is the one setting whose correct value differs per machine and whose wrong
value fails every run outright rather than degrading, so a scaffold must not put the idea in front
of someone unprompted. `STARTER_REGISTRY` has `"repos": []`.

The invariant's real purpose survives: the round-trip test in `test_config_cmd.py` parses **all
four** back into their models, and the discoverability test still asserts every `PrimaryState` and
`Action` appears in `TEMPLATE_TOOL_CONFIG` — deliberately the annotated one, since discoverability
belongs in the reference, which is what resolves its tension with a minimal scaffold.

`init`, `example` and `edit` all take the same positional naming which file — `config`,
`registry`, or (for `init`) omitted for both — reusing the vocabulary `config path` established.
That positional replaced a `--registry` boolean: a flag whose name is a noun answers "which file"
in a grammar built for "on or off", and it read as an unrelated mode rather than a target. `edit`
gained it late; it opened only `config.toml` while the file a new machine actually needs edited is
the registry. `example` on a terminal also names the path syncer really reads, because a template
on screen with no path is the half of the answer that cannot be acted on.

`config scan PATHS...` builds entries from the repos already on disk, deriving each one's owner
and host from its **real origin** — so a directory holding both your repos and third-party clones
scans correctly, which is exactly the shape of `exemplar-repos.json`. The commonest host/owner
become the registry defaults and every entry that disagrees keeps its own. It **prints** by
default and writes only under `--write`, and only when no registry exists: the same narrow reading
of the sanctioned single write, because this file is shared with forge and indy.

Every load path raises `ConfigError` carrying one readable line per problem, rather than letting a
pydantic `ValidationError` escape as a traceback. Policies are constructed one at a time in
`parse_tool_config` so the reported location names *which* policy the bad rule is in — pydantic's
own error says only `rules`, which is no help in a file holding several. `config validate` prints
the same lines it collects; it does not have its own error rendering.

Policies are machine-local **on purpose**: the same repo can sync aggressively on an always-on
box and report-only on a laptop. That's why they live in `config.toml`, never in `repos.json`.
The one exception is `sync_policy` in `repos.json` — a weak, portable hint that must name a
policy resolvable on every machine (i.e. a **built-in**), or the repo shows `unknown policy` on
machines whose `config.toml` lacks it (`config.toml` is not synced).

Policy resolution per repo, first hit wins (`resolve_policy_name`):
`--policy` → `repo_overrides` → `repos.json` `sync_policy` → `default_policy` → built-in `standard`.

Built-ins (`policy.py`): `standard` (default-branch auto-sync, feature branches report-only),
`observe` (report everything, mutate nothing — note: **does not pull**), `mirror` (auto everything
safe, opt-in always-on-box policy).

## Read-only checks that are not sync state

Two things the report surfaces that are neither a `PrimaryState` nor an `Action`. Both are
report-only, and both are deliberately kept out of `decide()` — there is nothing to act on.

- **`origin_mismatch`** (`repos.py`) — the clone's real `origin` vs `resolve_clone_url`'s answer.
  `gh repo clone <bare-name>` resolves to the authenticated user, so a reference repo that also
  exists under your own account silently gets your fork as upstream; two did, undetected for 3.5
  months, because nothing compared them. `normalize_remote_url` reduces both sides to `host/path`
  first — https, scp-style SSH and `ssh://host:port/` all reach the same repo, and flagging every
  SSH clone of an https registry entry would be noise that gets the check ignored. Surfaced in
  `issues` and as a **WARNING annotation** on the repo report — *not* a lifecycle status, which
  would replace the branch report for a repo whose only problem is where it points.
- **`watch_remote`** (`Policy`) — branches to report on with no local copy. A fetch brings down
  every remote branch but the pipeline only iterates local ones, so a long-lived branch never
  checked out is invisible. Opt-in and empty by default; **never affects severity**, since a repo
  is not unhealthy for having branches you deliberately do not keep. Never materialise these as
  local branches: a local copy you never check out is pinned at creation and silently serves
  stale history, while `origin/<branch>` is current after any fetch.

## Command groups

Sub-apps live in `src/syncer/commands/`, mounted in `main.py`. `output.py` holds the shared
console pair: **stdout is data, stderr is everything else** — `emit_json` writes to stdout and
bypasses Rich markup; `error`/`hint`/`success` write to stderr with `soft_wrap` so a path stays on
one line and survives a copy-paste.

`policy show` renders a **computed** decision matrix: every `PrimaryState` × three synthetic
`BranchState`s (default / current / neither), each cell produced by calling `decide()`. That is
only possible because `decide()` is pure, and it means the table cannot drift from what `apply`
does. Never replace it with a written-down table, and iterate the enums rather than listing their
members — `test_policy_cmd.py` asserts the matrix agrees with `decide()` cell for cell.

Three diagnostics, three questions, and every help text says which:

| Command | Answers |
| --- | --- |
| `config validate` | is the **structure** right — do both files parse and cross-reference |
| `syncer issues` | is **reality** right — do the registry's paths exist, has anything moved |
| `syncer doctor` | is this **machine** able to run syncer at all |

Each one's all-clear must name what it *measured*, not pronounce on the fleet. `issues` printed
"All repos healthy." for a fleet `check` was simultaneously calling untidy — a verdict on sync
state, from a command that never looks at sync state.

Blurring them means none of them gets trusted. `doctor` (`doctor.py`) exists because the first
two could both pass on a box where nothing worked: a first run that failed could not distinguish
a missing credential from a mis-pointed registry from a host that was never reachable. Its rules:

- **Prerequisite order, and the first FAIL is the actionable one.** No value in reporting that a
  registry lists no repos when git is not installed.
- **Nothing assumes GitHub.** Reachability is proved with `git ls-remote` against the URL the
  registry actually resolves to, so Bitbucket Data Center, an internal GitLab and a bare SSH host
  are all first-class. `gh` is never invoked — a test asserts it.
- **Never probe what is already known to be fake.** A registry still holding template
  placeholders skips the network checks entirely; reporting a DNS failure for
  `your-github-username` names the wrong problem.
- **`PROBE_TIMEOUT_SECONDS` is not `git_timeout`.** The latter is sized for fetching a monorepo
  over a VPN; a diagnostic that hangs two minutes per host is one nobody waits for.
- **Exit 1 on FAIL, 0 on WARN**, so `syncer doctor && syncer apply` stops on a box that was
  never going to work but not on one whose repos simply are not cloned yet.
- It **never writes anything**, unlike `config edit`, which seeds a template.

## Exit codes and `--json`

**One rule, stated once in `exit_code_for` (`report.py`): exit 1 iff any report reaches
`Severity.ERROR`.** `check` and `apply` share it; they differ only in which severities they
can produce. WARNING stays 0 on purpose — a repo that is `ahead` is the normal state of a machine
somebody works on, and a code that is non-zero every day is one nobody can automate against.
`issues` exits 1 when it found any, because printing "N issue(s) found" and exiting 0 is the
exact shape of a check whose caller has to scrape text for what the exit code should have said.

`--json` (both verbs, either view, all to **stdout**, everything else on stderr) reuses the
existing `RepoSnapshot`/`RunSummary` models rather than building a parallel shape, which is what
guarantees the JSON and the event stream agree about a run by construction. There is no
console-mode abstraction: an `as_json` flag skips the renderers, and that is all.

## Concurrency

Repos are processed on a `ThreadPoolExecutor` (default `DEFAULT_JOBS = 16`, `-j` to tune). Git
calls are I/O-bound and release the GIL, so wall-clock ≈ slowest single repo. All git work runs
in worker threads; results are collected then sorted and rendered on the main thread via
`pool.map` (order-preserving), so output never interleaves. A bounded per-task random jitter
(≤0.3s) staggers the initial fetch burst so N fetches don't hit the remote at once — bounded per
task, so no cumulative N×delay floor on large repo sets.

Output is sorted by attention ascending (`synced → operation → warning → error`, path-sorted
within each group) so the repos needing action land at the bottom nearest the prompt.

Every subprocess goes through `run_command` (`repos.py`), which is what makes that concurrency
safe: git prompts for credentials on `/dev/tty`, which `capture_output` does **not** redirect, so
an expired credential or an unknown SSH host key would leave N worker threads blocked on the same
terminal with nothing on screen. `GIT_TERMINAL_PROMPT=0` plus `-o BatchMode=yes` makes those fail
instead of ask, and a timeout (`git_timeout` in `config.toml`, default 120s; 600s for clones)
backstops anything that still blocks. A timeout is returned as an ordinary non-zero result, never
raised — raising out of a worker would lose the whole repo's report instead of the one wedged call.
Never call `subprocess.run` directly for a git or `gh` invocation.

## Run history

The default run appends one `SyncRunEvent` per run to `STATE_DIR/<registry-stem>-events.jsonl`
(`$XDG_STATE_HOME/syncer`, since history is state: nobody authors it, and deleting it changes
behaviour rather than costing a recompute); `syncer stats -c <registry>` reads the matching stream
back. **One stream per registry**, keyed on the registry file, because two registries are two
working sets: a shared file makes `stats` a blend of both, and `find_stale_repos` scopes to the
paths in the most recent run, so alternating a personal and a work run would make each set's
dirty-repo warnings vanish on the other's. The pre-split global `events.jsonl` is adopted
(renamed, so exactly once) by the default registry — never by one named with `--repos-file`, which
never contributed to that history.

`migrate_legacy_events` sweeps the pre-XDG data dir (`~/.local/share/syncer`) for both shapes —
the global stream and already-split per-registry ones — since a machine may have skipped the
release that split them. It renames rather than copies, so it runs once and an existing target
always wins, and it rmdir's the emptied directory so the migration's retirement condition is
observable. Carries the `# MIGRATION (v5.0.0)` marker per `~/dev/standards/data.md`.

The schema (`tracking.py`) evolves **additively** —
`RepoSnapshot` gained `policy` + `branches: list[BranchSnapshot]` and `RunSummary` gained
`pending`, all defaulted so pre-existing event lines still validate. Never make an existing
snapshot field required; add new fields with defaults and keep the legacy-parse test green
(`test_tracking.py`).

Additive fields are only half of it, because the stream is read by **every** version, not just
newer ones. `RepoSnapshot.status` is therefore typed `str`, not the `RepoStatus` Literal: adding
one status member made the *previous release* refuse to read its own history file with a pydantic
traceback, since a closed Literal on the read side rejects anything written by a newer syncer.
`RepoStatus` remains the **write** vocabulary — `_repo_status`/`_operation_status` are annotated
with it, so mypy checks every literal where a snapshot is built — while parsing tolerates values
it does not know. For the same reason `read_events` skips a line it cannot parse instead of
raising: history is a side channel, and one truncated write must not take down the sync report
the user actually asked for. Widening a persisted enum without doing both is a silent break that
only shows up on the machine running the older version.

## Testing & release

- One test module per source module under `tests/`. The exhaustive policy truth table is
  `test_policy.py`; the master→main incident is regression-locked in the suite.
- Released via python-semantic-release off conventional commits. `CHANGELOG.md` is
  **generated at release time** — never hand-edit it; unreleased commits are absent until the
  next version is cut. Per global rules, `refactor` does **not** trigger a release here, so use it
  only for genuine refactors.

## Planning

`.planning/` (symlinked to `~/dev/repos/syncer/planning/`) holds `status.md` (current state +
decisions) and `sync-policy-design.md` (the design doc — a historical artifact; drift from the
implementation is recorded in `status.md`, the design doc itself is left as-written).
