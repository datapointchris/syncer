# syncer

A CLI that checks whether local git repos are synced before you switch machines, and
optionally performs the safe sync actions. `check` reports and never writes; `apply` mutates.

## The pipeline: pure core, impure edges

The decision logic is a pure function, so it is exhaustively testable without touching git:

| Stage | Module | Purity | Owns |
| --- | --- | --- | --- |
| Classify | `classify.py` | impure (git reads) | Turns real repo state into `BranchState` objects. Runs the read-side remediation (`fetch --prune` + `git remote set-head origin --auto`) so a renamed default resolves before anything is classified. |
| Decide | `policy.py` | **pure** | `decide(state, policy) -> Action`. No git, no FS. A `BranchState` × a `Policy` maps to one action off a pre-vetted safe menu. Also holds the built-in policies. |
| Execute | `execute.py` | impure (git writes) | The only place that mutates. Enforces the hard invariants (below) and refuses rather than forces. |
| Report | `report.py` | impure (concurrency + render) | Runs classify→decide→(execute) per repo on a thread pool, sorts by attention, renders. |

Three modules sit beside the pipeline. None can change what an action does, and `decide()`
consults none of them: `breaker.py` (**pure**) answers whether a host is still worth asking,
`progress.py` draws the live display, and `remedy.py` (**pure**) turns a row syncer will not
clear into the command that clears it.

## A failed git call is never a state

A non-zero exit converted into a benign value — `[]` branches, `(0, 0)` counts, a clean tree — is
indistinguishable from a real answer. `(0, 0)` is `SYNCED`, so a repo whose fetch died would
report fully in sync. Three rules follow:

- **`Repo._git` records.** A non-zero exit appends a `GitFailure` (argv, returncode, stderr) to
  `repo.failures` unless the call passes `probe=True`. Recording is the default so a git call
  added later is visible without opting in. `probe=True` is only for calls whose non-zero exit
  *is* the answer (`merge-base --is-ancestor`, the `rev-parse --verify` probes, `git cherry`).
- **An unmeasurable repo is an error, not a state.** `refresh_remote()` returns the fetch's
  failure, and `_build_repo_report` returns a `_failed_report` **before** `build_branch_rows`,
  which also means no `execute()` call is constructed. Such a report carries **zero rows**: a row
  is a claim about a branch, and none can be made. There is deliberately no
  `PrimaryState.UNKNOWN` — every policy's rule for it would be `report`, and a state name cannot
  carry git's stderr.
- **Unknown resolves toward refusal.** `ahead_behind` returns `None` rather than `(0, 0)`;
  `remotes()` returns `None` (cannot ask) distinctly from `[]` (none configured). Every
  `execute.py` guard treats "cannot verify" as "do not proceed".

`decide()` depends only on the primary state plus the branch's role/name. `dirty`, `stashed` and
`protected` are **execute-time gates, never decision inputs** (`TestDecideModifierInvariance`).

A gate the reporter can evaluate without executing must still be *shown*, or the report promises
an action `apply` never makes. `blocking_refusal()` composes all four so a caller cannot miss one:
`protection_refusal()` (static config), `checkout_refusal()` (which branch is current),
`worktree_refusal()` (it forbids the ref-writers, `ff_worktree` requires one, and that tree must be
clean) and `dirty_refusal()` (one `git status` per repo). All live in `execute.py` beside the
guards they mirror and feed `BranchRow.blocked`. The mirror tests assert each agrees with
`execute()` for every action on the menu. A drifting mirror is worse than none, because the arrow
is the part of a row anyone acts on. A gate discoverable only mid-write (a rebase conflict,
unreadable counts) stays out of `blocked`.

**The checkout mirror is checked in both directions.** Under-predicting draws an arrow `apply`
declines, and a one-directional test cannot see that. `CHECKOUT_REFUSALS` names every refusal
decided by which branch is checked out (`NOT_CURRENT`, `IS_CURRENT`, `DELETE_CURRENT`), and a
mutator returning one while the mirror is silent fails a test. So every checkout-gated mutator
tests the checkout **first**. `_delete_local` answering `NOT_GONE` before `DELETE_CURRENT` would
make the guard unreachable, since `decide()` only routes `delete_local` at a gone branch.

**Refusals are keyed, never matched by text.** `Refusal` is a `StrEnum` and `REFUSAL_TEXT` holds
the wording once. `Outcome.reason` and `BranchRow.blocked` carry the key; `Outcome.message` and
`blocked_message` carry the prose. Severity, the remedy and the mirror tests all join on the key.
A test joining on an English sentence breaks whenever someone rewrites it.

## Severity is ownership, not git state

`Severity` orders rows by **who has to do something**, not by how unusual the branch state is.
Two notions of "needs attention" in one report means neither gets trusted.

- **`OPERATION` applies in `check` too.** An action is decided and nothing would refuse it, so
  `apply` clears it without you. `behind → fast_forward` is queued work, not damage.
- **A dirty tree outranks the action band**, whatever the branch state. syncer never resolves
  one, and it refuses every mutator that touches the tree.
- **`_row_severity` is checked before the state's own severity**, so `MUTATING_ACTIONS` (derived
  from `_MUTATORS`, never listed twice) defines "syncer will handle this".
- **Icon from the state, color from the severity.** Only the benign tick can understate a
  severity, so it is the only icon `_SEVERITY_ICON` substitutes.
- **The action arrow renders only for a mutating action, on both surfaces.** `skip`, `report`
  and `prompt` all mean "syncer changes nothing". `_apply_line` defers to `_branch_line` whenever
  the action does not mutate. Such an action's `message` is still kept, since only an executed
  run can know one.

The summary line splits the same way (`N to sync` in cyan vs `N need you` in yellow).
`RepoStatus.pending` exists because a `check` run recording `pulled` would write a mutation that
never happened into the history `stats` reads.

**Severity is also what the default view renders.** `needs_attention` is one expression —
`report_severity(...) > SYNCED` — so the filter, the sort and the summary counts cannot disagree.
`-v` shows every repo. `render_hidden_note` states the count left out, so a short report is never
mistaken for a short registry. `--json` and the event stream still record every repo, because
hiding is a rendering decision. A `watch_remote` branch always renders: it is opt-in and never
affects severity.

**`-v`, not `--all`.** The flag changes what is *printed*, never what is operated on. `-a` widens
the operated-on set (`ls -a`, `git branch -a`). Spelling this one `--all` would claim the default
checks a subset of the registry.

**The counters are keyed, never spelled twice.** `Tally`/`TALLY_TEXT` in `output.py` does for the
live display and the summary line what `Refusal`/`REFUSAL_TEXT` does for refusals.
`attention_tally` picks the counter, and it cannot be severity alone: ERROR spans a genuinely
wrong branch and a repo git could not be asked about. `is_unverified` is that split, and
`_repo_status` reads it too.

## Two surfaces, one core

`run_sync` (`sync.py`) and `report_branches` (`report.py`, `--per-branch`) share
`gather_reports` + `render_report`, and differ only in `include_lifecycle`:

- **default run** (`include_lifecycle=True`): also clones missing repos under `apply`, flags
  moved/not-git/no-remote repos, emits a run event, prints a summary line, and warns about repos
  left dirty for days.
- **`--per-branch`** (`include_lifecycle=False`): pure per-branch view, skips non-git repos, no events.

There is no second sync path. Special-case the default run behind `include_lifecycle`.

### A directory where a repo belongs is a repo waiting to be cloned

`git clone` refuses a non-empty destination. On a machine being set up every repo path is
non-empty, because the gitignored files a machine cannot rebuild (`.env`, `.planning/`) are
restored before anything is cloned. `Repo.clone_into_existing` builds the repo around what is
there: `init`, `remote add`, `fetch`, then `checkout` of the remote's default. The mechanism is
chosen because **checkout will not write over a file already present, and aborts whole rather
than file by file**, even when the content is identical. So no path in the resulting index
predates the checkout, and removing the `.git` a refusal built cannot orphan a file. Nothing else
is ever deleted.

- **A file the repo neither tracks nor ignores is kept, and the fresh clone is dirty.** Requiring
  a clean tree was the alternative, and it is the only design that would delete a user's file.
- **`not_git` is reserved for a path already holding git state.** A linked worktree keeps its
  `.git` as a *file*, so `is_git_repo` reads False. `git init` there succeeds against the owning
  repo through the gitdir link.
- **`check` says `would clone` at WARNING, and names the entry count**, so a box mid-setup exits 0.

## Safety invariants (execute.py)

`execute()` re-verifies **every** precondition live, immediately before each write, and never
trusts the classify-time `BranchState`. It refuses rather than forces. Guaranteed independent of
any policy:

1. Never `--force`/`-f`/`--force-with-lease`. No such argv is ever constructed.
2. Never mutate a branch whose working tree is dirty **or whose cleanliness cannot be verified**.
   `repo.is_dirty` answers `True` when `git status` fails; a `list | None` cannot express that
   polarity, because `None` is falsy. `_TREE_INDEPENDENT` exempts `_ff_ref` and `_delete_local`,
   which move or remove a ref checked out nowhere (each verifies that first, per invariant 9).
   A dirty tree is no evidence about a ref being deleted. **syncer never resolves a dirty tree**:
   there is no commit or stash action, and there will not be one.
3. `fast_forward`/`pull_ff`/`ff_ref` require strict ancestry, re-checked at write time.
4. `rebase_push` aborts on conflict and downgrades to a refusal — never a half-rebase.
5. `delete_local` only under `GONE ∧ integrated ∧ ¬current ∧ ¬worktree ∧ ¬default ∧
   ¬merge-target`. It uses `branch -D`, because a GONE branch has no upstream for `-d`'s
   heuristic; the explicit guard is the safety. *Integrated* means the target provably holds the
   work, by ancestry **or** patch equivalence (`git cherry`), and is never inferred from the
   remote branch's deletion. The target is `policy.merge_target` (default: the repo's default
   branch), since a develop-centric flow never makes a feature branch an ancestor of `main`.
6. Any precondition that fails at execute time is refused and reported, never forced.
7. Actions use explicit refspecs and ref names, so they act on the classified branch, never the
   incidentally checked-out one.
8. A branch matching the policy's `protected` patterns admits no action that publishes or
   destroys. It is checked in `execute()` **before dispatch**, so it covers actions added later.
   `PROTECTED_ALLOWED` (`policy.py`) is an **allowlist**, so a new `Action` is refused on a
   protected branch by default. `fast_forward` belongs in it; `push`, `rebase_push`,
   `set_upstream_push` and `delete_local` do not. No built-in policy sets `protected`.
9. Never write the ref of a branch another worktree has checked out. `is_current` is *this*
   checkout's HEAD alone. `update-ref` is plumbing git does not check, so it would move a live
   worktree's HEAD and leave its index staging the new commit as deletions.
   `repo.held_by_worktree` answers True when `git worktree list` fails. `_delete_local` guards
   this explicitly even though `branch -D` refuses on its own: git's refusal is a `failed`
   outcome carrying prose, with no `Outcome.reason` to join on and nothing the reporter can predict.

Any new `Action` must be added to the `Action` enum, mapped in `_MUTATORS`, and given a mutator
that re-checks its own preconditions live. Never add an unsafe primitive to the menu.

**Rules name intents, not mechanisms.** A rule naming a mechanism is refused whenever the branch
is checked out somewhere else. `fast_forward` dispatches to whichever applies:

| Checked out | Mechanism | Runs |
| --- | --- | --- |
| here | `pull_ff` | `git merge --ff-only <upstream>` |
| in a linked worktree | `ff_worktree` | `git -C <worktree> merge --ff-only <upstream>` |
| nowhere | `ff_ref` | `git update-ref refs/heads/<branch> <upstream>` |

Dispatch reads git rather than `state.worktree`, so a worktree added or removed since classify
cannot pick the wrong one. The mechanisms stay on the menu as explicit escape hatches.
`TestBuiltinsNameIntentNotMechanism` locks the built-ins against `MECHANISM_ACTIONS`, so a new
mechanism is covered without editing the test. Any future action splitting on checkout location
needs the same treatment.

**`ff_worktree` adds a location, not a primitive.** It runs `pull_ff`'s argv inside the worktree
holding the branch, with the same ancestry check, so ref, index and tree move together. Its dirty
guard is `worktree_is_dirty`, against the tree it writes. `BranchState.dirty` is the main
checkout's, and the two are routinely opposite.

A branch made by `git worktree add <path> -b <slug> origin/<default>` tracks
`origin/<default>`, so it reads `N behind` until it has commits of its own. Nothing is wrong with
it, and advancing it is expected.

## Config: two files, deliberately split

- **`repos.json`** (default `$XDG_CONFIG_HOME/syncer/repos.json`) — the portable repo **identity
  registry**: `owner`, `host`, `search_paths`, `exclude_paths`, and per-repo `{name, path, status,
  description, owner?, sync_policy?, toolchain?}`. **syncer never modifies one that holds repos**,
  since other tools may read it. `issues` reports drift but tells you to fix paths by hand. Only
  two commands write, and both only *create*: `config init` scaffolds an absent file, and
  `config scan --write` fills one that is absent or empty. Both refuse once the file lists repos,
  and `scan` treats an unparsable file as content.
- **`config.toml`** (`$XDG_CONFIG_HOME/syncer/config.toml`) — **machine-local** tool config:
  `repos_registry`, `default_policy`, custom `[policies.*]`, `[repo_overrides]`, and `git_timeout`.

**The registry path resolves `-c/--repos-file` → `$SYNCER_REPOS_REGISTRY` → `config.toml`
`repos_registry` → `DEFAULT_REPOS_FILE`.** syncer reads no variable that is not prefixed
`SYNCER_`. An unprefixed rung is invisible to a systemd timer, which sources no profile.
`TestRegistryLocation` pins that none is consulted.

The default stays a syncer-owned XDG path, and nothing shipped here names a shared one.
`repos_registry` is the one setting whose correct value differs per machine and whose wrong value
fails every run, so `config.toml` is never distributed from a shared source. Every resolved path
carries its provenance (`RegistryLocation.source`). Every missing-registry message prints it plus
both exits: create one there, or drop the pointer.

**Registry details:**

- `toolchain` belongs to a separate tool that generates pre-commit and CI config. syncer models
  it as an opaque dict and never reads it. It lives on the entry because a separate file keyed by
  repo name needs a join, and names are not unique across registries. Anything added to an entry
  must be a portable fact about the repo, never machine-local state.
- Clone URLs resolve per-repo `clone_url` → registry `url_template` (`{host}`/`{owner}`/`{name}`)
  → `{host}/{owner}/{name}`. The template exists for scp-style SSH and a required `.git` suffix.
  An unknown placeholder fails at load time.
- **A registry is a self-contained set.** `-c` swaps the whole working set and never merges.
  `owner` and `host` are optional, so an all-third-party registry works. Repos not owned by the
  registry owner skip the `using master` check, and `owns_branch_naming = false` turns it off for
  a whole registry. `is_fork` short-circuits off GitHub, since `gh` cannot answer for other hosts.
- `config scan PATHS...` derives each entry's owner and host from its **real origin**, so a
  directory mixing your repos with third-party clones scans correctly. It prints by default.

**Scaffolding and teaching are separate.** `STARTER_*` is what `config init` writes, and
`TEMPLATE_*` is what `config example` prints. A scaffold has nothing in it to delete; a reference
has everything. `STARTER_TOOL_CONFIG` carries no `repos_registry`, not even commented out.
`test_config_cmd.py` round-trips all four and asserts every `PrimaryState` and `Action` appears in
`TEMPLATE_TOOL_CONFIG`. `init`, `example` and `edit` take a positional naming the file (`config`
or `registry`), never a `--registry` boolean.

Every load path raises `ConfigError` with one readable line per problem, never a pydantic
traceback. `parse_tool_config` builds policies one at a time, so the error names *which* policy
holds the bad rule. `config validate` prints those same lines.

**A `[policies.X]` table merges onto whatever `X` already is** — the built-in of that name, or
nothing. Patching a built-in and defining a policy are one syntax:

```toml
[policies.standard.rules]
"*:gone" = "delete_local"
```

`rules` merges cell by cell, at the **raw-dict** level in `_build_policies`. Constructing a
`Policy` first would fill absent fields with model defaults and clobber the base: patching
`[policies.mirror.rules]` would reset mirror's `scope` from `all` to `tracked`. `extend` names a
base only when the table's name is not a built-in's. Unknown keys are rejected explicitly, because
pydantic drops extras silently. `ToolConfig.policy_bases` exists only so `policy show` can mark
which rules a patch changed; it never reaches `decide()`.

Policies are machine-local **on purpose**, so they live in `config.toml`, never `repos.json`. The
exception is `sync_policy` in `repos.json`. It is a portable hint and must name a **built-in**, or
the repo shows `unknown policy` on machines whose `config.toml` lacks it.

Per-repo resolution, first hit wins (`resolve_policy_name`): `--policy` → `repo_overrides` →
`repos.json` `sync_policy` → `default_policy` → built-in `standard`. The built-ins are `standard`,
`observe` and `mirror`. `observe` mutates nothing and **does not pull**.

## The report says what to run, for the rows it will not clear

`remedy.py` is **pure** and holds the same honesty rules as `diagnose.py`. `diagnose` turns a git
failure into a hint; `remedy_for(state, action, repo_path, blocked, refused)` turns a state syncer
will not act on into one. Its rules:

- **Nothing is suggested where syncer acts.** An unblocked mutating action gets an empty `Remedy`.
- **No command can lose work**: no `--force`, `reset`, `checkout` or `stash`. `TestHonestyRules`
  asserts it over the whole table by argv **token**, since `-f` is a substring of `--ff-only`.
- **Every command names its directory**: `state.worktree` when one holds the branch. `DIRTY_TREE`
  is the repo's own tree, so its `status` runs in the repo; `WORKTREE_DIRTY` runs it in the
  worktree. `NO_WORKTREE` is in `NO_REMEDY`.
- **A refusal is not one errand**, so `_refusal_remedy` takes the action. `WORKTREE_CHECKOUT` on
  `ff_ref` means merge inside the worktree; on `delete_local` it means dispose of the worktree first.
- **`_tracks_own_remote` decides the wording** for `diverged`. Tracking `origin/main` is work on a
  moved base; tracking `origin/<itself>` is two machines disagreeing.
- **syncer never says what to do with uncommitted work.** The dirty remedy is `git status --short`.
- **The policy note names the rule key that would change the decision.** Candidates come from
  `ACTION_DOCS.applies_to` minus `MECHANISM_ACTIONS`.
- **Every `Refusal` is routed or listed in `NO_REMEDY`**, so a new one fails a test.

The remedy renders on **stdout** with its row, not through `hint()` on stderr, so a redirected
report keeps it.

## Read-only checks that are not sync state

Both are report-only and stay out of `decide()`.

- **`origin_mismatch`** (`repos.py`) compares the clone's real `origin` with `resolve_clone_url`'s
  answer. `gh repo clone <bare-name>` resolves to the authenticated user, so a reference repo that
  also exists under your account silently gets your fork as upstream. `normalize_remote_url`
  reduces both sides to `host/path` first, so https and SSH clones of one repo match. It is a
  **WARNING annotation**, not a lifecycle status, which would replace the branch report.
- **`watch_remote`** (`Policy`) lists branches to report on that have no local copy. It is
  opt-in, empty by default, and **never affects severity**. Never materialize these as local
  branches: an unused local copy is pinned at creation, while `origin/<branch>` is current after
  any fetch.

## Commands

`policy actions show <action>` renders one record from `ACTION_DOCS` (`execute.py`, beside the
guards). Its protection line and `decided_by` are computed, not declared.

**`applies_to` is declared, not derived, and cannot be.** Only `_delete_local` tests
`state.primary`. Every other mutator checks live facts that *are* the definitions of AHEAD,
DIVERGED and BEHIND. Adding a `primary` check to make it derivable would break invariant 6.
`TestActionDocs` proves it instead: it drives every mutator against a repo genuinely in each state
and asserts `applies_to` is exactly the set where the action can act, in both directions.

`policy show` renders a **computed** decision matrix: every `PrimaryState` × three synthetic
`BranchState`s (default / current / neither), each cell from `decide()`. It cannot drift from
what `apply` does. Never replace it with a written table, and iterate the enums rather than
listing members. `test_policy_cmd.py` asserts it cell by cell.

Three diagnostics answer three questions, and every help text says which:

| Command | Answers |
| --- | --- |
| `config validate` | is the **structure** right — do both files parse and cross-reference |
| `syncer issues` | is **reality** right — do the registry's paths exist, has anything moved |
| `syncer doctor` | is this **machine** able to run syncer at all |

Each all-clear names what it *measured*, never a verdict on the whole set. `doctor` (`doctor.py`):

- Checks run in prerequisite order, and the first FAIL is the actionable one.
- Nothing assumes GitHub. Reachability is `git ls-remote` against the resolved URL. `gh` is never
  invoked, and a test asserts it.
- A registry still holding template placeholders skips the network checks.
- `PROBE_TIMEOUT_SECONDS` is not `git_timeout`, which is sized for a monorepo over a VPN.
- Exit 1 on FAIL, 0 on WARN, so `syncer doctor && syncer apply` stops only on a box that cannot work.
- It never writes anything.

## Exit codes and `--json`

**`exit_code_for` (`report.py`) is the one rule: exit 1 iff any report reaches
`Severity.ERROR`.** WARNING stays 0, because `ahead` is the normal state of a machine somebody
works on. `issues` exits 1 when it found any.

`--json` (both verbs, either view) writes to **stdout**, and everything else goes to stderr. It
reuses the `RepoSnapshot`/`RunSummary` models, so the JSON and the event stream agree by
construction. An `as_json` flag skips the renderers; there is no console-mode abstraction.

## Concurrency

Repos run on a `ThreadPoolExecutor` (`-j`, default `DEFAULT_JOBS`). Results are collected with
`as_completed` only so the progress display can count them. The final sort is total on path, so
order never depends on a race. Streaming results as they arrive was rejected because it loses the
sort. Output is sorted by attention ascending (`synced → operation → warning → error`), so the
repos needing action land nearest the prompt.

**A worker's exception is that repo's report, never the run's.** `run_one` catches it into an
ordinary error report. An escaping exception would discard every other repo's result.

### The wait is legible, and it ends when you say so

`progress.py` draws a two-line display on **stderr**: progress, the tally in the summary line's
words, and the in-flight repos with elapsed seconds, longest first. It is deliberately not a
`rich.progress.Progress`, whose columns are per-task and whose elapsed times freeze while nothing
completes. It is off when `console.is_terminal` is false or `--json` is set.

Ctrl-C ends the run, not the current call. `abort_running_commands()` terminates every live git
process and short-circuits later ones. Canceling the queue alone leaves `shutdown` waiting out
`git_timeout`. Nothing renders and **no run event is written**, since a partial sweep is not a
measurement. Exit code 130. An aborted call *is* recorded as a `GitFailure`, because branches
below an unfetched remote would be measured against stale refs. Its stderr matches no
`diagnose` pattern.

### Nothing may open a window, and a dead host is asked once

Every subprocess goes through `run_command` (`repos.py`). It uses `Popen` because the abort needs
the handle. A timeout (`git_timeout`, default 120s; 600s for clones) returns as an ordinary
non-zero result and is never raised, so one wedged call loses only itself. Never call
`subprocess.run` directly for git or `gh`.

**`GIT_TERMINAL_PROMPT=0` disables git's own terminal prompt and nothing else.** An askpass
program and a credential helper are separate mechanisms git prefers over the terminal, and
neither reads it. With an expired token and a GUI credential manager, syncer spawned one helper
window per repo at once and printed nothing. `_noninteractive_env` closes each path: an empty
`GIT_ASKPASS` (git takes the first of `GIT_ASKPASS`/`core.askpass`/`SSH_ASKPASS` that is *set*,
so an empty one short-circuits the chain), `SSH_ASKPASS_REQUIRE=never`, and Git Credential
Manager's two switches, `GCM_INTERACTIVE=never` and `credential.interactive=false`. The helper
stays **configured**. Resetting `credential.helper=` would break every https remote whose stored
credential is fine.

Config is injected through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n`, appended to the user's own,
never through `-c` in argv. Not every git call is built in `Repo._git` — a clone and `doctor`'s
`ls-remote` are not — so the environment is the only place that reaches all of them.

**`breaker.py` stops asking a host that has already said no.** The first host-wide cause (`AUTH`,
`HOST_KEY`, `DNS`) closes that host; `NETWORK` and `TIMEOUT` need `FLAKY_THRESHOLD` of them.
`NOT_FOUND` never trips, because it is what a private repo you cannot see reports. The key is
`(host, ssh-or-https)`, since an ssh key and an https token on one host fail independently. A
host that has answered successfully is never closed. In-flight calls cannot be stopped, so the
damage is bounded at `jobs` rather than at the registry size.

Skipped repos are `RepoBranchReport.skipped` (a `Trip`). They carry **zero rows**, count as
`unverified`, and are **never rendered one per repo, `-v` included**. They fold into the failure
summary for the cause that closed their host and stay out of `hidden_count`.
`render_failure_summary`'s fallback for a skip with no group of its own is unreachable today and
stays on purpose.

## Run history

The default run appends one `SyncRunEvent` to `STATE_DIR/<registry-stem>-events.jsonl`
(`$XDG_STATE_HOME/syncer`, because history is state). `syncer stats -c <registry>` reads the
matching stream. **There is one stream per registry.** `find_stale_repos` scopes to the paths in
the most recent run, so a shared stream would make alternating registries hide each other's
dirty-repo warnings. The legacy global `events.jsonl` is adopted once, by the default registry only.

The schema (`tracking.py`) evolves **additively**. Never make an existing snapshot field required.
Add fields with defaults and keep the legacy-parse test in `test_tracking.py` green.

The stream is read by **every** version, not just newer ones. So `RepoSnapshot.status` is typed
`str`, not the `RepoStatus` Literal. A closed Literal on the read side makes an older release
refuse a history a newer one wrote. `RepoStatus` stays the **write** vocabulary, which mypy checks
where a snapshot is built. `read_events` skips an unparsable line instead of raising, because
history is a side channel and must not take down the report. Widening a persisted enum needs
both halves.

## Testing and release

There is one test module per source module under `tests/`. `test_policy.py` holds the exhaustive
primary-state × policy truth table, which is only possible because `decide()` is pure. Releases
come from python-semantic-release off conventional commits. The notes go in the GitHub release
body, and there is no changelog file.
