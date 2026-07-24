# syncer

Check if local git repos are fully synced before switching machines.

Syncer fetches each configured repo, checks for uncommitted changes, unpushed/behind commits, and stashes, then auto-pulls or auto-pushes when safe.

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
syncer                  # sync all repos (auto-detects config)
syncer --dry-run        # show what would happen without making changes
syncer --config name    # use a specific config
syncer issues            # report path mismatches, missing/untracked repos, master branches
syncer branches          # per-branch sync report across all local branches (read-only)
syncer branches -p observe  # override the resolved policy for the report
syncer branches --apply  # execute each policy's decided action (safe actions only)
syncer branches -j 8     # limit concurrency to 8 repos at a time (default 16)
syncer demo             # run against temp repos to show each status state
syncer version          # print installed version
syncer init name        # create a template config file
```

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

`syncer branches` classifies every branch (per-branch `ahead`/`behind`/`gone`/`no_upstream`/…, computed after `fetch --prune` and repointing `origin/HEAD`) and reports the action a policy *would* take. `syncer branches --apply` then executes those actions. Policies are **machine-local** and live in `config.toml`, so the same repo can sync aggressively on an always-on box and report-only on a laptop.

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
