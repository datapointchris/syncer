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
(asserted in `TestDecideModifierInvariance`). `protected` is the same kind of gate and is likewise
invisible to `decide()`. Because it is *static config* rather than live repo state, though,
`protection_refusal()` is shared with the reporter so a report-only run marks the actions that
would be refused — rendering the decided `push` alone would promise a push `--apply` never makes.

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
  toolchain?}`. Portable across machines. **syncer never modifies it** — it's shared
  infrastructure (also read by `forge` and `indy`). `issues` reports drift but tells you to fix
  paths by hand. `config init` *creates* one that is absent, which is the single write and not a
  modification: it refuses the moment a file is there. A tool that can only tell you to hand-write
  a file whose shape it already knows has pushed its own job onto the reader.

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

`TEMPLATE_TOOL_CONFIG` and `TEMPLATE_REGISTRY` in `config.py` are the **single source** for both
`config init` and `config example` — changing a config model means changing the template, and a
round-trip test (`test_config_cmd.py`) parses each back into its model so they cannot drift. A
second test asserts every `PrimaryState` and `Action` appears in the tool-config template, so a
new member cannot ship undiscoverable.

`init` **writes** those templates, `example` **prints** them, and both take the same optional
positional naming which file — `config`, `registry`, or omitted for both — reusing the vocabulary
`config path` already established. That positional replaced a `--registry` boolean: a flag whose
name is a noun answers "which file" in a grammar built for "on or off", and it read as an unrelated
mode rather than a target. `example` on a terminal also names the path `init` would write to,
because a template on screen with no path is the half of the answer that cannot be acted on.

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
only possible because `decide()` is pure, and it means the table cannot drift from what `--apply`
does. Never replace it with a written-down table, and iterate the enums rather than listing their
members — `test_policy_cmd.py` asserts the matrix agrees with `decide()` cell for cell.

`config validate` checks **structure**; `syncer issues` checks **reality** (do the paths exist).
Both help texts say so. Blurring them means neither gets trusted.

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
