# syncer

Check if local git repos are fully synced before switching machines.

Syncer fetches every configured repo concurrently, classifies each branch (ahead/behind/gone/…), and shows what a per-machine [sync policy](#sync-policies) would do. `syncer check` reports and never writes; `syncer apply` executes the safe actions (fast-forward, push, clone, prune). Output is ordered so anything needing attention lands at the bottom, nearest the prompt.

## Installing

```bash
uv tool install "syncer @ git+https://github.com/datapointchris/syncer.git@$(gh release view --repo datapointchris/syncer --json tagName -q .tagName)"
```

Install a **tag**, not the default branch — substitute one explicitly (`@v6.0.0`) if you would rather
not shell out to `gh`. `syncer update` reads uv's receipt to find out what it may do, and refuses to
reinstall over a branch install, whose version says nothing about how far behind it is.

## Updating

```bash
syncer update
```

This fetches the latest GitHub release and reinstalls via `uv tool install`.

## Starting from scratch

On a machine that has never run syncer:

```bash
syncer config init                     # minimal config + empty registry, at the paths syncer reads
syncer config scan ~/code ~/tools      # build registry entries from the repos already on disk
syncer config edit registry            # or name them by hand
syncer doctor                          # can this machine actually run syncer? names what is wrong
syncer check                           # report only, never writes
syncer apply                           # execute the safe actions
```

`config init` writes a **minimal** starter — nothing in either file needs deleting. The fully annotated versions, with every option exercised, are reference material: `syncer config example config` and `syncer config example registry`.

`config scan` reads each repo's real `origin` to work out its owner and host, so a directory holding both your own repos and third-party clones scans correctly. It prints to stdout for review; `--write` puts it at the path syncer reads, and only when no registry is there.

**When something does not work, run `syncer doctor` first.** It reports git, the resolved config
and registry paths *and what chose each of them*, whether the remotes can actually be reached
(with git's own error and what to do about it), and how many repos are cloned — in prerequisite
order, so the first failure is the one to act on. It exits 1 on a real problem, so
`syncer doctor && syncer apply` stops on a box that was never going to work.

`config init` creates whichever of the two files is missing and **never rewrites one that exists** — the registry is shared infrastructure that `forge` and `indy` also read, so syncer scaffolds one that is absent and modifies no existing one. `syncer config init registry` does just that file; `syncer config path` says where both landed.

## Usage

```bash
syncer check              # classify every repo/branch and show what would happen; never writes
syncer apply              # execute each policy's safe actions (pull/push/ff/clone)
syncer check --per-branch # per-branch view: no lifecycle, cloning, or run history
syncer check --json       # emit the run as JSON on stdout instead of a report
syncer apply -p observe   # override the resolved policy for this run
syncer apply -j 8         # limit concurrency to 8 repos at a time (default 16)
syncer check -c work.json # use a different registry; replaces the default set entirely
syncer doctor            # can this machine run syncer? git, paths, reachability, clones
syncer issues            # report path mismatches, missing/untracked repos, master branches
syncer apply --per-branch # execute the decided action per branch
syncer stats             # run history and repo insights (commits, age, dirty, stale)
syncer config            # inspect, edit, scan, and validate the config and registry
syncer policy            # list policies and show what each one decides
syncer demo              # run against temp repos to show each status state
syncer version           # print installed version
```

The repo-level view and `--per-branch` share the same policy engine and concurrency; the difference is that the repo-level one also handles repo lifecycle (clone missing repos, flag moved/untracked/no-remote repos), records a run in the history (`syncer stats`), and warns about repos left dirty for days.

### Exit codes

| Code | Meaning |
| --- | --- |
| `0` | nothing reached an error — including repos that merely need a push |
| `1` | at least one repo reached an error state (`syncer`, `branches`, `doctor`), or at least one issue was found (`issues`) |
| `2` | usage error — an unknown flag or argument |

**A repo that is `ahead` exits 0.** That is the normal state of a machine somebody works on, and an exit code that is non-zero every day is one nobody can automate against. `1` means something is genuinely wrong: a clone that failed, a repo whose state could not be verified, an action that was refused at write time.

`--json` on either verb writes the whole run to **stdout** as JSON with nothing else mixed in — the report, the per-repo detail, and the grouped failure causes with git's own stderr. The sync JSON is built from the same snapshots the event stream records, so it cannot disagree with `syncer stats` about what happened.

## Config

Two files, deliberately split. `syncer config path` prints where both resolve to.

**`~/.config/syncer/config.toml`** — machine-local tool config: which registry to read, the default policy, custom policies, per-repo overrides, and the git timeout. `syncer config example` prints a fully annotated one showing every option. The scaffolded file carries a worked example policy named `laptop` — yours to edit or delete, not a built-in, however much `syncer policy list` makes it look like one on a fresh machine.

```toml
# repos_file = "~/shared/repos.json"  # defaults to ~/.config/syncer/repos.json
default_policy = "standard"
git_timeout = 120                     # ceiling on a single git call; clones get 5x this
```

`repos_file` is worth setting only when another tool reads the same registry file, and then only on the machines that have it. Because it is machine-local, this file is the one thing that must **not** be shared between machines: a config naming a path only some of them have makes every run there fail on a registry that was never going to exist. Any message about a missing registry names what chose the path — `(from repos_file in ~/.config/syncer/config.toml)` — and offers both exits, creating one there or dropping the pointer.

**`~/.config/syncer/repos.json`** — the repo registry, portable between machines. `syncer config init registry` writes an annotated one; `syncer config example registry` prints it without writing.

```json
{
  "owner": "your-github-username",
  "host": "https://github.com",
  "search_paths": ["~/code", "~/tools"],
  "exclude_paths": ["~/code/refs"],
  "repos": [
    {"name": "my-repo", "path": "~/code/my-repo", "status": "active"}
  ]
}
```

Each repo has a `status`: `active` (default), `dormant`, or `retired`. Retired repos are skipped during sync.

`search_paths` are what `syncer issues` scans for repos that moved or aren't tracked; `exclude_paths` disclaims a subtree inside them, for directories another registry owns. **Syncer never modifies a registry that holds repos** — `issues` reports drift and you fix the paths by hand. Only `config init` and `config scan --write` write one, and both refuse the moment the file lists any.

`issues` also flags a clone whose `origin` disagrees with what the registry declares. `gh repo clone <bare-name>` resolves to the authenticated user, so a reference repo that also exists under your own account silently gets your fork as its upstream and pulls from it forever — which went unnoticed for three months on two repos. Comparison normalises https, scp-style SSH and `ssh://` with a port to the same thing, so cloning over SSH against an https registry isn't a false positive. Report-only: a deliberate fork is indistinguishable from a mistake without asking, so the remote is never rewritten.

A registry is a self-contained set: `-c/--repos-file` swaps the entire working set rather than merging with the default, and each registry gets its own run history, so `syncer stats -c work.json` reports only on that set.

`owns_branch_naming` (default `true`) controls whether `syncer issues` flags repos still defaulting to `master`. Turn it off for a registry whose default-branch naming isn't yours to change — at a company that is the org's decision, so the check would report forever with nothing to do about it.

Clone URLs default to `{host}/{owner}/{name}`. Hosts that path can't express — scp-style SSH has no slash after the host, Bitbucket Data Center wants a `/scm` prefix and a `.git` suffix — set `url_template` on the registry, or `clone_url` on a single repo that doesn't follow its host's convention:

```json
{
  "owner": "myworkspace",
  "host": "bitbucket.org",
  "url_template": "git@{host}:{owner}/{name}.git",
  "owns_branch_naming": false,
  "repos": [
    {"name": "payments", "path": "~/code/work/payments"},
    {"name": "odd-one", "path": "~/code/work/odd-one", "clone_url": "ssh://git@other:7999/x/odd-one.git"}
  ]
}
```

Run history goes to `$XDG_STATE_HOME/syncer/<registry>-events.jsonl` — state rather than data, since nothing authors it and deleting it only resets what `syncer stats` can see.

## Sync policies

Both views classify every branch (per-branch `ahead`/`behind`/`gone`/`no_upstream`/…, computed after `fetch --prune` and repointing `origin/HEAD`) and report the action a policy *would* take; `syncer apply` executes those actions. Policies are **machine-local** and live in `config.toml`, so the same repo can sync aggressively on an always-on box and report-only on a laptop.

`--apply` is safe by construction: it enforces hard invariants no policy can override — never `--force`, never mutate a dirty working tree, fast-forward only under strict ancestry, `rebase_push` aborts cleanly on conflict, and any precondition that fails at write time is refused (never forced) rather than mutated.

Repos are fetched and processed **concurrently** (default 16 at a time, `-j` to tune), so a single run over many repos takes roughly as long as the slowest repo rather than the sum. A small random jitter staggers the initial fetches so they don't hit the remote all at once. Output is sorted by attention, so anything needing action lands nearest the prompt.

Three policies are built in: `standard` (default-branch auto-sync, feature branches report-only), `observe` (report everything, mutate nothing), and `mirror` (auto everything safe, opt-in). Define your own under `[policies.<name>]` with a `scope` (`default`/`current`/`tracked`/`all`) and a rule table keyed by `<selector>:<state>`:

```toml
default_policy = "standard"

[policies.laptop]
scope = "all"
fallback = "report"
merge_target = "develop"                    # branch a gone branch must be integrated into before delete_local
protected = ["develop", "dev", "uat", "prod", "release/*"]
watch_remote = ["develop", "dev", "uat", "prod"]
[policies.laptop.rules]
"release/*:ahead" = "report"
"*:behind"        = "fast_forward"
"*:diverged"      = "report"

[repo_overrides]
"some-shared-repo" = "observe"   # per-repo, per-machine
```

### Protected branches

`protected` names branches (fnmatch patterns, the same grammar the selectors use) that nothing may publish to or destroy. It is enforced centrally in the executor before any action runs, so it's a **hard guard** rather than a matter of writing the right exact-name rule for every branch — under a fallback like `"*:ahead" = "push"`, one branch you forgot is one stray local commit on a shared branch.

`push`, `rebase_push`, `set_upstream_push` and `delete_local` are refused. `fast_forward` still runs: advancing a branch to what its upstream already contains publishes nothing and destroys nothing, and refusing it would make the setting useless for exactly the long-lived branches it exists to protect. The permitted set is an allowlist, so any action added later is refused on a protected branch by default.

Like every other policy setting, it is **machine-local** — it lives on a policy in `config.toml`, none of the built-ins protect anything, and the portable registry carries no policy settings at all. A report-only run marks the actions protection would refuse, so you don't have to run `--apply` to see the guard working:

```bash
syncer policy show laptop --branch develop   # marks every action this stops
```

### Watched remote branches

A fetch already brings down every branch on the remote, but syncer only classifies branches you have *locally* — so a long-lived branch you deliberately never check out is invisible, and nothing tells you `origin/prod` moved. `watch_remote` names patterns to report on anyway:

```text
  origin/develop — remote only, last commit 2 hours ago
  origin/uat     — remote only, last commit 3 days ago
```

Purely informational, and it never affects a repo's severity: there is no local branch to sync, and a repo isn't unhealthy for having branches you deliberately don't keep. Opt-in and empty by default, because every repo has remote branches you'll never care about and a check that fires on all of them is one you learn to ignore.

**Don't create local copies of these to read them.** `git log origin/uat`, `git show origin/uat:file` and `git diff origin/develop...HEAD` all work offline against the fetched ref. A local branch you never check out is pinned at whenever you made it, so it silently shows stale history while looking like a normal branch.

Selectors resolve exact branch name → glob → role (`default`, then `current`) → `*`, and the first one with a rule for that state wins. Rules should name **intents, not mechanisms**: `fast_forward` dispatches to `merge --ff-only` when the branch is checked out and `update-ref` when it isn't, so naming either mechanism directly (`pull_ff`, `ff_ref`) is refused for half of all checkout states.

Rather than reading a rule table and working out what it produces, ask:

```bash
syncer policy list                            # every policy this machine resolves
syncer policy show laptop                     # its rules, plus the decision for every state
syncer policy show laptop --branch release/2  # ...resolved for a real branch name
```

That matrix is computed by calling the rules engine, so it is the policy rather than a description of one — it cannot drift from what `--apply` will do.

Policy per repo resolves in order: CLI `--policy` → machine `repo_overrides` → optional `sync_policy` hint in `repos.json` → machine `default_policy` → built-in `standard`. `syncer config show` prints which one won for every repo. The `sync_policy` hint must name a **built-in**, since the registry travels between machines and `config.toml` doesn't; `syncer config validate` enforces that.

See `.planning/sync-policy-design.md` for the full state taxonomy, action catalog, and safety invariants.
