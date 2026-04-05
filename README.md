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
