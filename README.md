# syncer

Check if local git repos are fully synced before switching machines.

Syncer fetches every configured repo concurrently, classifies each branch (ahead/behind/gone/…), and shows what a per-machine [sync policy](#sync-policies) would do. It's **report-only by default**; `syncer --apply` executes the safe actions (fast-forward, push, clone, prune). Output is ordered so anything needing attention lands at the bottom, nearest the prompt.

## Installing

```bash
uv tool install git+https://github.com/datapointchris/syncer.git@latest
```

## Updating

```bash
syncer update
```

This fetches the latest GitHub release and reinstalls via `uv tool install`.

## Starting from scratch

On a machine that has never run syncer:

```bash
syncer config init                                            # annotated ~/.config/syncer/config.toml
syncer config edit                                            # adjust the default policy, add your own
syncer config example --registry > ~/.config/syncer/repos.json  # scaffold the repo registry
syncer config validate                                        # both files, plus their cross-references
syncer                                                        # report-only
syncer --apply                                                # execute the safe actions
```

`config init` never writes the registry — that file is shared infrastructure, so `config example --registry` scaffolds it and you own it from there.

## Usage

```bash
syncer                   # report-only: classify every repo/branch, show what would happen
syncer --apply           # execute each policy's safe actions (pull/push/ff/clone)
syncer --dry-run         # force report-only, even with --apply
syncer -p observe        # override the resolved policy for this run
syncer -j 8              # limit concurrency to 8 repos at a time (default 16)
syncer -c work.json      # use a different registry; replaces the default set entirely
syncer issues            # report path mismatches, missing/untracked repos, master branches
syncer branches          # per-branch report only (no lifecycle/clone, no event tracking)
syncer branches --apply  # execute the decided action per branch
syncer stats             # run history and repo insights (commits, age, dirty, stale)
syncer config            # inspect, edit, and validate the config and registry
syncer policy            # list policies and show what each one decides
syncer demo              # run against temp repos to show each status state
syncer version           # print installed version
```

The default `syncer` run and `syncer branches` share the same policy engine and concurrency; the difference is that the default run also handles repo lifecycle (clone missing repos, flag moved/untracked/no-remote repos), records a run in the history (`syncer stats`), and warns about repos left dirty for days.

## Config

Two files, deliberately split. `syncer config path` prints where both resolve to.

**`~/.config/syncer/config.toml`** — machine-local tool config: which registry to read, the default policy, custom policies, per-repo overrides, and the git timeout. `syncer config example` prints a fully annotated one showing every option.

```toml
# repos_file = "~/dev/repos.json"   # defaults to ~/.config/syncer/repos.json
default_policy = "standard"
git_timeout = 120                   # ceiling on a single git call; clones get 5x this
```

**`~/.config/syncer/repos.json`** — the repo registry, portable between machines. `syncer config example --registry` prints an annotated one.

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

`search_paths` are what `syncer issues` scans for repos that moved or aren't tracked; `exclude_paths` disclaims a subtree inside them, for directories another registry owns. **Syncer never writes to the registry** — `issues` reports drift and you fix the paths by hand.

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

Both `syncer` and `syncer branches` classify every branch (per-branch `ahead`/`behind`/`gone`/`no_upstream`/…, computed after `fetch --prune` and repointing `origin/HEAD`) and report the action a policy *would* take; `--apply` executes those actions. Policies are **machine-local** and live in `config.toml`, so the same repo can sync aggressively on an always-on box and report-only on a laptop.

`--apply` is safe by construction: it enforces hard invariants no policy can override — never `--force`, never mutate a dirty working tree, fast-forward only under strict ancestry, `rebase_push` aborts cleanly on conflict, and any precondition that fails at write time is refused (never forced) rather than mutated.

Repos are fetched and processed **concurrently** (default 16 at a time, `-j` to tune), so a single run over many repos takes roughly as long as the slowest repo rather than the sum. A small random jitter staggers the initial fetches so they don't hit the remote all at once. Output is sorted by attention, so anything needing action lands nearest the prompt.

Three policies are built in: `standard` (default-branch auto-sync, feature branches report-only), `observe` (report everything, mutate nothing), and `mirror` (auto everything safe, opt-in). Define your own under `[policies.<name>]` with a `scope` (`default`/`current`/`tracked`/`all`) and a rule table keyed by `<selector>:<state>`:

```toml
default_policy = "standard"

[policies.laptop]
scope = "all"
fallback = "report"
merge_target = "develop"   # branch a gone branch must be integrated into before delete_local
[policies.laptop.rules]
"release/*:ahead" = "report"
"*:behind"        = "fast_forward"
"*:diverged"      = "report"

[repo_overrides]
"some-shared-repo" = "observe"   # per-repo, per-machine
```

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
