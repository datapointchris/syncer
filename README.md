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
syncer doctor           # check for path mismatches, missing/untracked repos, master branches
syncer doctor --fix     # auto-fix issues (update paths, add untracked, rename master to main)
syncer demo             # run against temp repos to show each status state
syncer version          # print installed version
syncer init name        # create a template config file
```

## Config

Config files live at `~/.config/syncer/<name>.json`.

```json
{
  "owner": "your-github-username",
  "host": "https://github.com",
  "search_paths": ["~/code", "~/tools"],
  "repos": [
    {"name": "my-repo", "path": "~/code/my-repo"}
  ]
}
```

If there's only one config file, syncer uses it automatically. With multiple configs, use `--config name` to select one.

`search_paths` are used by `syncer doctor` to find repos that moved or aren't tracked in the config.
