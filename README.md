# syncer

## Installing

Use `pipx` to install, so that it is globally available.  
Install from the github repo to get the latest stable release
`pipx install git+https://www.github.com/datapointchris/syncer.git@latest`
(if `@latest` doesn't work then go find the latest release and use that)

## Updating

### 1) Make Changes in the Repo

Changes should be made inside of the repo and committed as regular.
Changes must be `PUSHED`

### 2) Create Release

***IMPORTANT:*** Run `syncer create-release` from INSIDE `.venv` locally.  
This is necessary so the most recent version of the project that was previously committed in Step 1 will be the version that the new release will be created from.

### 3) Update `syncer`

**EXIT** the virtual environment, in order to use the global `syncer`  
Run `syncer update` to get the newest version.
> [!NOTE]
> If the newest version is not working, wait a few minutes for Github to update
> If it does not work after a while, you can use `syncer update --force`

## Troubleshooting

### Plugins

`syncer plugins [tmux|zsh]` does *not* work inside of `tmux`. It points the plugins dir to `~/.local/share/tmux/plugins/` instead of the plugins director in syncer.
Syncing plugins must be done in a terminal outside of `tmux`.
