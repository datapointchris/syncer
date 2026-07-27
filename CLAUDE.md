# syncer

A CLI that checks whether local git repos are synced before you switch machines, and
optionally performs the safe sync actions. Report-only by default; `--apply` mutates.

This file covers the architecture and the conventions specific to this repo. Universal
rules (commits, git safety, Python style) live in `~/.claude/CLAUDE.md` — not restated here.

## The pipeline: pure core, impure edges

The whole design is a four-stage pipeline split so the decision logic is a pure function and
therefore exhaustively testable without touching git:

| Stage | Module | Purity | Owns |
|-------|--------|--------|------|
| Classify | `classify.py` | impure (git reads) | Turns real repo state into `BranchState` objects. Runs the read-side remediation (`fetch --prune` + `git remote set-head origin --auto`) so a renamed default resolves before anything is classified. |
| Decide | `policy.py` | **pure** | `decide(state, policy) -> Action`. No git, no FS. A `BranchState` × a `Policy` maps to one action off a pre-vetted safe menu. Also holds the built-in policies. |
| Execute | `execute.py` | impure (git writes) | The only place that mutates. Enforces the hard invariants (below) and refuses rather than forces. |
| Report | `report.py` | impure (concurrency + render) | Runs classify→decide→(execute) per repo on a thread pool, sorts by attention, renders. |

`decide()` being pure is why `tests/test_policy.py` can enumerate the full cartesian product
of primary-states × policies as a no-git truth table. Keep git and FS I/O out of `policy.py`
— that boundary is load-bearing, not incidental.

`decide()` depends only on the primary state plus the branch's role/name. The `dirty`/`stashed`
modifiers are **execute-time gates, never decision inputs** — `decide()` is invariant to them
(asserted in `TestDecideModifierInvariance`).

## Two surfaces, one core

`run_sync` (`sync.py`, the default `syncer` run) and `report_branches` (`report.py`, `syncer
branches`) share `gather_reports` + `render_report`. The difference is a single
`include_lifecycle` flag:

- **default run** (`include_lifecycle=True`): also clones missing repos under `--apply`, flags
  moved/not-git/no-remote repos, emits a run event, prints a summary line, and warns about repos
  left dirty for days.
- **`branches`** (`include_lifecycle=False`): pure per-branch view, skips non-git repos, no events.

There is no second sync path — the old default-branch `sync_repos` loop was deleted, not kept
alongside. If you're tempted to special-case the default run, add it behind `include_lifecycle`.

## Safety invariants (execute.py)

`--apply` is safe by construction. `execute()` re-verifies **every** precondition live,
immediately before each write — it never trusts the (possibly stale) `BranchState` from classify
time — and refuses rather than forces. Guaranteed independent of any policy:

1. Never `--force`/`-f`/`--force-with-lease` (no such argv is ever constructed).
2. Never mutate a branch whose working tree is dirty.
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

- **`repos.json`** (default `~/dev/repos.json`, path set in `config.toml`) — the repo **identity
  registry**: `owner`, `host`, `search_paths`, `exclude_paths`, and per-repo `{name, path, status,
  description, owner?, sync_policy?, toolchain?}`. Portable across machines. **syncer never writes
  to it** — it's shared infrastructure (also read by `forge` and `indy`). `issues` reports drift but
  tells you to fix paths by hand.

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
- **`config.toml`** (`~/.config/syncer/config.toml`) — **machine-local** tool config: `repos_file`
  pointer, `default_policy`, custom `[policies.*]`, and `[repo_overrides]`.

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

The default run appends one `SyncRunEvent` per run to `DATA_DIR/<registry-stem>-events.jsonl`;
`syncer stats -c <registry>` reads the matching stream back. **One stream per registry**, keyed on
the registry file, because two registries are two working sets: a shared file makes `stats` a blend
of both, and `find_stale_repos` scopes to the paths in the most recent run, so alternating a
personal and a work run would make each set's dirty-repo warnings vanish on the other's. The
pre-split global `events.jsonl` is adopted (renamed, so exactly once) by the default registry —
never by one named with `--repos-file`, which never contributed to that history.

The schema (`tracking.py`) evolves **additively** —
`RepoSnapshot` gained `policy` + `branches: list[BranchSnapshot]`, both defaulting empty so
pre-existing event lines still validate. Never make an existing snapshot field required; add new
fields with defaults and keep the legacy-parse test green (`test_tracking.py`).

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
