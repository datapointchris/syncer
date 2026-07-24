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

## Usage

```bash
syncer                  # report-only: classify every repo/branch, show what would happen
syncer --apply          # execute each policy's safe actions (pull/push/ff/clone)
syncer --dry-run        # force report-only, even with --apply
syncer -p observe       # override the resolved policy for this run
syncer -j 8             # limit concurrency to 8 repos at a time (default 16)
syncer issues            # report path mismatches, missing/untracked repos, master branches
syncer branches          # per-branch report only (no lifecycle/clone, no event tracking)
syncer branches --apply  # execute the decided action per branch
syncer stats             # run history and repo insights (commits, age, dirty, stale)
syncer demo             # run against temp repos to show each status state
syncer version          # print installed version
syncer init             # create the tool config (~/.config/syncer/config.toml)
```

The default `syncer` run and `syncer branches` share the same policy engine and concurrency; the difference is that the default run also handles repo lifecycle (clone missing repos, flag moved/untracked/no-remote repos), records a run in the history (`syncer stats`), and warns about repos left dirty for days.

## Config

Syncer reads its tool config from `~/.config/syncer/config.toml`, which points to the repo registry:

```toml
repos_file = "~/dev/repos.json"
```

The repo registry is a JSON file listing all repos:

```json
{
  "owner": "your-github-username",
  "host": "https://github.com",
  "search_paths": ["~/code", "~/tools"],
  "repos": [
    {"name": "my-repo", "path": "~/code/my-repo", "status": "active"}
  ]
}
```

Each repo has a `status` field: `active` (default), `dormant`, or `retired`. Retired repos are skipped during sync.

`search_paths` are used by `syncer issues` to find repos that moved or aren't tracked in the config. The repo registry is the source of truth — syncer never writes to it.

## Sync policies

Both `syncer` and `syncer branches` classify every branch (per-branch `ahead`/`behind`/`gone`/`no_upstream`/…, computed after `fetch --prune` and repointing `origin/HEAD`) and report the action a policy *would* take; `--apply` executes those actions. Policies are **machine-local** and live in `config.toml`, so the same repo can sync aggressively on an always-on box and report-only on a laptop.

`--apply` is safe by construction: it enforces hard invariants no policy can override — never `--force`, never mutate a dirty working tree, fast-forward only under strict ancestry, `rebase_push` aborts cleanly on conflict, and any precondition that fails at write time is refused (never forced) rather than mutated.

Repos are fetched and processed **concurrently** (default 16 at a time, `-j` to tune), so a single run over many repos takes roughly as long as the slowest repo rather than the sum. A small random jitter staggers the initial fetches so they don't hit the remote all at once. Output is rendered in directory order regardless of which repo finishes first.

Three policies are built in: `standard` (default-branch auto-sync, feature branches report-only), `observe` (report everything, mutate nothing), and `mirror` (auto everything safe, opt-in). Define your own under `[policies.<name>]` with a `scope` (`default`/`current`/`tracked`/`all`) and a rule table keyed by `<selector>:<state>`:

```toml
default_policy = "standard"

[policies.laptop]
scope = "all"
fallback = "report"
[policies.laptop.rules]
"default:behind" = "pull_ff"
"*:behind"       = "ff_ref"    # advance non-current branch refs, no checkout

[repo_overrides]
"some-shared-repo" = "observe"   # per-repo, per-machine
```

Policy per repo resolves in order: CLI `--policy` → machine `repo_overrides` → optional `sync_policy` hint in `repos.json` → machine `default_policy` → built-in `standard`. See `.planning/sync-policy-design.md` for the full state taxonomy, action catalog, and safety invariants.
