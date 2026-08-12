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

Two modules sit beside the pipeline rather than in it, both about surviving a bad machine rather
than about deciding anything: `breaker.py` (**pure**) answers whether a host is still worth
asking, and `progress.py` draws the live display over the pool. Neither is consulted by
`decide()`, and neither can change what an action does.

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
- **The action arrow renders only for a mutating action, on both surfaces.** `skip`/`report`/`prompt`
  all mean "syncer changes nothing", which the row conveys by existing — `→ skip` after every clean
  repo spent a column on the least informative word in the vocabulary. The rule was implemented in
  `_branch_line` and lost in the apply path, which restated it as an outcome (`→ skip: skipped`, on
  79 of 80 rows), so `_apply_line` now defers to `_branch_line` whenever the action does not mutate.
  A non-mutating action cannot reach any other status — all three are in `PROTECTED_ALLOWED` and
  `dirty_refusal` ignores them — so nothing is hidden. Its `message` is the one exception and is
  kept, because only an executed run can know one.

The summary line splits the same way (`N to sync` in cyan vs `N need you` in yellow), and
`RepoStatus` gained `pending` for it, because a `check` run recording `pulled` would write a
mutation that never happened into the history `stats` reads back as fact.

**Severity is also what the default view renders.** `needs_attention` is one expression —
`report_severity(...) > SYNCED` — so the filter, the sort and the summary counts cannot disagree
about what matters. On the fleet that is 256 lines of report down to 34: a registry is mostly
synced on any ordinary day, and which repos are synced is not information, whereas *how many* are
is. `-v` shows every repo, `render_hidden_note` states the count that was left out so a short
report is never mistaken for a short registry, and `--json` is unaffected — hiding is a rendering
decision, and the event stream still records every repo or `stats` would report on the bad days
only. The single exception is a `watch_remote` branch, which deliberately never affects severity:
it is opt-in, and a branch someone asked to be told about should not then need a flag to appear.

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
  toolchain?}`. Portable across machines. **syncer never modifies one that holds repos** — it is
  shared infrastructure that other tools may read too. `issues` reports drift but tells you to
  fix paths by hand. Exactly two commands write, and both only *create*: `config init` scaffolds
  one that is absent, and `config scan --write` fills one that is absent or still empty. Both
  refuse the moment the file lists repos, and `scan` treats an unparsable file as content too —
  the last one to clobber silently. A tool that can only tell you to hand-write a file whose shape
  it already knows, from data already on disk, has pushed its own job onto the reader.

  A registry can be shared with other tools by pointing `repos_file` at a common location. That
  sharing is an arrangement between those tools, **never a syncer fact** — the default must stay a
  syncer-owned XDG path, so a machine that has never heard of that location still works. Nothing
  shipped in this repo may *recommend* a specific shared path either: the tool-config template used
  to name one and say every machine points here, which reads as an instruction rather than an
  example, and a `config.toml` deployed from a shared source then pointed a machine at a registry
  that only ever existed on another. `config.toml` is machine-local in the operational sense, not
  just by convention: `repos_file` is the one setting whose correct value differs per machine and
  whose wrong value fails every run outright rather than degrading, so that file must never be
  distributed from a shared source.

  Every resolved registry path therefore carries its provenance (`RegistryLocation.source`), and
  every message about a missing registry prints it plus both exits (create one there, or drop the
  pointer). That failure was undiagnosable from syncer's own output, which named the path but never
  what chose it — so the tool looked like it had someone else's layout hard-coded. A resolution
  chain owes the reader which tier won.

  **It is no longer purely identity.** `toolchain` declares a repo's build surface — `components`
  (a `stack` and the `dir` it lives in) and `sql_dialect` — and is owned entirely by a separate
  tool that generates pre-commit configs and CI workflows from it. syncer models it as an opaque
  dict and never reads it. It lives here rather than in that tool's own file, because a separate
  file would have to be keyed by repo name, and repo names are not unique across registries — that
  exact join already misattributed one repo's planning docs to another. Attaching the data to the
  entry avoids the join. Anything added here must be a **portable fact about the repo itself**,
  never machine-local state; that still belongs in `config.toml`.

  Clone URLs resolve as per-repo `clone_url` → registry `url_template` (`{host}`/`{owner}`/`{name}`)
  → the default `{host}/{owner}/{name}`. The template exists because that default path cannot
  express scp-style SSH (no slash after the host) or a required `.git` suffix, and a registry is
  one host — so it belongs on the registry, not repeated on every entry. `url_template` is
  validated at load time; an unknown placeholder fails loudly rather than producing a broken URL.

  **A registry is a self-contained set.** `--repos-file/-c` swaps the entire working set; it never
  merges with the default. `owner` and `host` are optional so an all-third-party registry works —
  a registry of upstream clones has entries that each name their own owner. Repos whose owner
  isn't the registry owner are treated as not ours: the `using master` check is skipped
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
of the sanctioned single write, because this file may be shared with other tools.

Every load path raises `ConfigError` carrying one readable line per problem, rather than letting a
pydantic `ValidationError` escape as a traceback. Policies are constructed one at a time in
`parse_tool_config` so the reported location names *which* policy the bad rule is in — pydantic's
own error says only `rules`, which is no help in a file holding several. `config validate` prints
the same lines it collects; it does not have its own error rendering.

**A `[policies.X]` table is merged onto whatever `X` already is** — the built-in of that name if
one ships, nothing if it does not. So patching a built-in and defining a policy are one syntax
with no mode switch, and changing one decision is a two-line block that names the policy it
changes rather than inventing a name:

```toml
[policies.standard.rules]
"*:gone" = "delete_local"
```

`rules` merges cell by cell rather than replacing the table. The merge is at the **raw-dict**
level in `_build_policies`, and that is load-bearing: constructing a `Policy` from the table first
fills every absent field with a model default and clobbers the base with it, so patching
`[policies.mirror.rules]` would silently reset mirror's `scope` from `all` to `tracked`. `extend`
names a base only when the table's name is not already a built-in's. Unknown keys are rejected
explicitly — pydantic ignores extras, so `extends = "standard"` would otherwise be dropped in
silence, and a policy that quietly did not inherit reads as syncer ignoring the whole block.

Merging is why nothing needs a name it did not already have. A name exists so several repos can
share one rule set and `repos.json` can point at it — not as a wrapper you must invent to change
a cell. `ToolConfig.policy_bases` records what each entry merged onto, purely so `policy show`
can mark which rules a patch actually changed; it is config metadata and never reaches `decide()`.

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

`policy rules [policy]` is the browse-and-pick view and the one to live in: every settable rule
key, grouped by state, with that state's meaning, the actions it will accept, the action this
policy currently decides, and where that decision came from. It exists because the rule *space*
is enumerable — three role selectors × the state enum — so it prints as one flat greppable table
instead of a grammar you assemble in your head from two other commands. Alternatives per group
rather than per row, since they depend only on the state.

`policy actions list` / `policy actions show <action>` are the vocabulary and the drill-down.
`show` renders one action's record from `ACTION_DOCS` (`execute.py`, beside the guards): what it
runs, every precondition that refuses it, what it will never do, and whether protection admits it.
The protection line and `decided_by` are computed, not declared.

**`applies_to` is declared, not derived, and cannot be.** Only `_delete_local` tests
`state.primary`; every other mutator checks live facts — `_push` wants `ahead > 0 ∧ behind == 0`,
`_rebase_push` wants both non-zero, the fast-forward pair wants the inverse of push. Those *are*
the definitions of AHEAD/DIVERGED/BEHIND, so the mapping is real but unreadable from the source,
and adding a `primary` check to make it derivable would break invariant 6 — a guard consulting
classify-time state is trusting a value that may already be stale. So it is declared and *proven*:
`TestActionDocs` drives every mutator against a repo genuinely in each state and asserts
`applies_to` is exactly the set where the action can act. Both directions, because the outside
direction alone would let a doc widen to "any state" and still pass.

**Refusals are keyed, never matched by text.** `Refusal` is a `StrEnum`, `REFUSAL_TEXT` holds the
wording once, and `Outcome.reason` carries the key alongside the display-only `message`. This is
what lets the mirror tests compare key sets — a test joining two tables on an English sentence
fails whenever someone rewrites it, with nothing wrong, and the churn teaches you to loosen the
assertion instead of trusting it. `describe_refusal` fills generic stand-ins for runtime values
when a reason is being documented rather than reported.

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

Each one's all-clear must name what it *measured*, not pronounce on the whole set. `issues`
printed "All repos healthy." for repos `check` was simultaneously calling untidy — a verdict on
sync state, from a command that never looks at sync state.

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
in worker threads; results are collected as each finishes (`submit` + `as_completed`) and sorted
and rendered on the main thread, so output never interleaves. A bounded per-task random jitter
(≤0.3s) staggers the initial fetch burst so N fetches don't hit the remote at once — bounded per
task, so no cumulative N×delay floor on large repo sets.

`as_completed` rather than `pool.map` only so the progress display can count results as they
land; the final sort is total on path, so the rendered order never depends on which repo won a
race. Streaming each result as it arrived was the alternative and it loses the sort, which is the
only reason the rows that matter are the ones nearest the prompt.

Output is sorted by attention ascending (`synced → operation → warning → error`, path-sorted
within each group) so the repos needing action land at the bottom nearest the prompt.

**A worker's exception is that repo's report, never the run's.** Every future is collected before
anything renders, so an exception escaping one worker discarded all the others too — the whole
registry measured, a traceback printed, and not one line about the repos that were fine. Caught
in `run_one`, it becomes an ordinary error report. This is the same failure as a dead fetch
reporting `synced`, in the other direction: one repo's unknown must never be stated as everyone's.

### The wait is legible, and it ends when you say so

`progress.py` draws a live two-line display on **stderr** while the pool runs: how far in, the
tally so far in the summary line's own words, and the in-flight repos with each one's elapsed
seconds, longest-running first. The names are what the line is *for* — a bare bar answers "is it
moving" and the actual question on a slow machine is which repo is slow. It is deliberately not a
`rich.progress.Progress`: those columns are per-task, and a renderable that rebuilds on each
refresh is both smaller and the only way elapsed times advance while nothing is completing. Off
whenever `console.is_terminal` is false or `--json` is set, since Rich repaints in place and into
a pipe that is one line per refresh.

Ctrl-C ends the run, not the current call. `abort_running_commands()` terminates every live git
process and short-circuits every later one; without it, `ThreadPoolExecutor.shutdown` waits for
running tasks and an interrupt during a fetch storm sits there for the remainder of `git_timeout`.
Cancelling the queue alone is not enough — both halves are needed. Nothing is rendered and **no
run event is written**, because a sweep covering some unknown fraction of the registry is not a
measurement and `stats` would read one back as if it were. Exit code 130.

An aborted call *is* recorded as a `GitFailure`, which reads backwards until you take the caller's
view: a fetch that did not happen leaves every branch below it measured against refs nobody
refreshed, so `fetch()` returning None would be the `(0, 0)` reads as SYNCED bug wearing a
different hat. Its stderr matches no pattern in `diagnose`, so it can never be mistaken for a fact
about the remote.

### Nothing may open a window, and a dead host is asked once

Every subprocess goes through `run_command` (`repos.py`), which is what makes that concurrency
safe. It uses `Popen` rather than `subprocess.run` because the abort above needs the handle —
`run()` owns its child privately, which is exactly what left a Ctrl-C with nothing to signal. A
timeout (`git_timeout` in `config.toml`, default 120s; 600s for clones) is returned as an ordinary
non-zero result, never raised — raising out of a worker would lose the whole repo's report instead
of the one wedged call. Never call `subprocess.run` directly for a git or `gh` invocation.

**`GIT_TERMINAL_PROMPT=0` disables git's own terminal prompt and nothing else**, and that gap was
the worst failure this tool had. An askpass program and a credential helper are separate
mechanisms git *prefers* over the terminal, and neither reads that variable — so on a machine with
a GUI credential manager an expired token spawned one helper process per repo, all at once, each
waiting on a window nobody asked for. The machine slowed to a crawl and syncer printed nothing,
because every worker was still blocked. `_noninteractive_env` closes each path: empty `GIT_ASKPASS`
(git takes the first of GIT_ASKPASS / `core.askpass` / SSH_ASKPASS that is *set*, so an empty value
short-circuits the chain rather than falling through it), `SSH_ASKPASS_REQUIRE=never`, and Git
Credential Manager's two spellings of one switch — `GCM_INTERACTIVE=never` and
`credential.interactive=false`. The helper is left **configured**: resetting it with
`credential.helper=` would break every https remote whose stored credential is fine. A stored
credential still answers; only the window is refused.

Config is injected through `GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n` rather than `-c` in argv, and
appended to whatever the user already set. Only some git calls are built in `Repo._git` — a clone
has no repo to run inside and `doctor`'s `ls-remote` probe is assembled in another module — so a
setting threaded through argv reaches whichever call sites remembered it, while one in the
environment reaches all of them, including the ones added later.

**`breaker.py` stops asking a host that has already said no.** A registry is mostly one host, so a
dead credential is one machine problem discovered N times, and attempting every repo is what made
the storm above proportional to the registry. The first *host-wide* cause (`AUTH`, `HOST_KEY`,
`DNS`) closes that host; `NETWORK` and `TIMEOUT` need `FLAKY_THRESHOLD` of them, because a refused
connection can be one bad moment and a timeout is routinely one legitimately enormous repo.
`NOT_FOUND` never trips — it is exactly what a private repo you cannot see reports, and it says
nothing about the machine. The key is `(host, ssh-or-https)`, never the host alone: a loaded ssh
key and an expired https token live on one host every day. A host that has answered successfully
can never be closed afterwards. The window it cannot close is the one already in flight — bounding
the damage at `jobs` instead of at the size of the registry is the whole win.

Skipped repos are `RepoBranchReport.skipped` (a `Trip`), carry **zero rows** for the same reason
an unmeasurable repo does, count as `unverified` rather than `issues` in the run history — nobody
looked at them — and are **never rendered one per repo**. They are folded into the failure summary
block for the cause that closed their host, since that is the whole of their explanation.

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
observable. Carries a `# MIGRATION (v5.0.0)` marker naming the release that introduced it.

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

`.planning/` (gitignored) holds `status.md` (current state + decisions) and
`sync-policy-design.md` (the design doc — a historical artifact; drift from the implementation is
recorded in `status.md`, the design doc itself is left as-written).
