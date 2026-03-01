# CHANGELOG


## v1.5.1 (2026-03-01)

### Bug Fixes

- Sync uv.lock with updated dependencies
  ([`93d7bd1`](https://github.com/datapointchris/syncer/commit/93d7bd1863e44cbfa14fe6679f12a6ad4b4cdf5b))

Updates virtualenv 20.37.0 -> 21.1.0 and adds python-discovery 1.1.0 as a new transitive dependency
  of virtualenv.


## v1.5.0 (2026-03-01)

### Features

- Show clone counts in sync summary bar
  ([`d880c02`](https://github.com/datapointchris/syncer/commit/d880c02c1213b24012622c29aa229a74926f487c))

Repos needing cloning were handled per-repo but invisible in the summary. Dry-run now shows "N to
  clone" and real runs show "N cloned".

- Add clonable/cloned counters to sync_repos - Append clone entries to summary_parts in correct
  order - Add cloned field to RunSummary (default 0 for backwards compat) - Show cloned count in
  stats recent runs display - Add missing-repo scenario to demo setup


## v1.4.1 (2026-02-17)

### Bug Fixes

- Align stats bar charts to longest repo path
  ([`b9c8ece`](https://github.com/datapointchris/syncer/commit/b9c8ece79d2138bd7ba9892d39c0f243695d3885))

Replaced hardcoded 30-character label width with dynamic sizing based on the longest label in each
  section (commits, repo age, frequently dirty). This prevents misalignment when repo paths exceed
  30 characters.

- Sync uv.lock with 1.4.0 release
  ([`66124c4`](https://github.com/datapointchris/syncer/commit/66124c4f00478233ae2f73972ba2d541cf7a6dc1))

This updates uv.lock to reflect the 1.4.0 version. Last manual sync needed since the build_command
  now handles this during releases.


## v1.4.0 (2026-02-17)

### Bug Fixes

- Update uv.lock during semantic-release
  ([`149a23f`](https://github.com/datapointchris/syncer/commit/149a23ff57f99bae7265eebb8e67b199dc7a31b7))

Add build_command to semantic_release config to run 'uv lock' during release commits, ensuring the
  lock file stays in sync with version bumps. Also updates uv.lock to current 1.3.1 version.

### Build System

- Use build(release) prefix for semantic-release commits
  ([`caa946f`](https://github.com/datapointchris/syncer/commit/caa946fdf97b469e553141c2199ba4e2f3edfd00))

Changes the commit message template for semantic-release from chore(release) to build(release) to
  better reflect that releases are build system changes.

### Continuous Integration

- Install uv in release workflow for build_command
  ([`608c465`](https://github.com/datapointchris/syncer/commit/608c465f73a755745496be3947cde8743f60d3d9))

The semantic-release build_command runs `uv lock`, which requires uv to be available in the CI
  environment. This adds the setup-uv action to install it before the release step.

- Install uv inside semantic-release build command
  ([`10bf654`](https://github.com/datapointchris/syncer/commit/10bf6540404f50ea77bac11e3dcb1d3a6e7ee6ef))

The semantic-release action runs in a Docker container, so the setup-uv step on the runner doesn't
  help. Changed build_command to install uv via pip inside the container before running uv lock.
  Removed the now-unnecessary setup-uv workflow step.

- Use recommended uv lock integration for semantic-release
  ([`e583e31`](https://github.com/datapointchris/syncer/commit/e583e31fd52e8e95953de174df42c003c1a0468c))

Updated the semantic-release build_command to follow the official uv integration guide: uses
  --upgrade-package to only update the package version in the lock (not all deps), and stages
  uv.lock for inclusion in the release commit.

### Features

- Add commit and repo age graphs, sort repos by activity
  ([`29fff61`](https://github.com/datapointchris/syncer/commit/29fff61113aa8a13835a0c94c2fa05a1defded53))

Adds two new bar chart visualizations to the stats command: - Commits by Repo: shows total commit
  count per repository - Repo Age: displays time since first commit with duration formatting

Also sorts the All Repos table by last active date (most recent first) and adds comprehensive test
  coverage for all new functionality.


## v1.3.1 (2026-02-17)

### Bug Fixes

- Skip update when already at latest release
  ([`a4aa784`](https://github.com/datapointchris/syncer/commit/a4aa7840556dfa0cdd476c930b33a9a74aba31d3))

The update command now compares the installed version against the latest GitHub release tag and
  skips reinstallation if already up to date, providing better user feedback about the current
  version status.

### Chores

- **release**: 1.3.1
  ([`8943168`](https://github.com/datapointchris/syncer/commit/8943168fd0e7a85ea17ca8ee5c68954b303a6a85))


## v1.3.0 (2026-02-17)

### Chores

- **release**: 1.3.0
  ([`747b73a`](https://github.com/datapointchris/syncer/commit/747b73a601bf429dc78f8ba9ea3d7de049a82539))

### Documentation

- Rewrite README for current architecture
  ([`7c49a2d`](https://github.com/datapointchris/syncer/commit/7c49a2dabe3904da5d128bdd1a24e289ef7c19a7))

The old README documented obsolete features (pipx install, create-release script, plugins, manual
  update workflow). This complete rewrite reflects the current tool:

- uv-based installation and updates - All CLI commands (sync, doctor, demo, init, version, update) -
  Config file format and location - Doctor auto-fix capabilities - Simplified troubleshooting
  (removed obsolete plugin notes)

The documentation now matches what the tool actually does as of v1.2.0.

### Features

- Add git stats properties to Repo class
  ([`46db748`](https://github.com/datapointchris/syncer/commit/46db7487e4314afab6667e5cf44951361a195037))

Add last_commit_date, first_commit_date, and total_commits properties to query git repository
  statistics on-demand. These complement existing status properties (ahead/behind) for tracking
  repository activity.

- Add syncer stats command
  ([`a96a4c0`](https://github.com/datapointchris/syncer/commit/a96a4c085d2a6829c737fac93e268367c16b1ad5))

Adds a comprehensive stats dashboard showing: - Summary of last 30 days (total runs, last run, avg
  issues) - Frequently dirty repos with visual bar charts - Stale repo warnings (uncommitted > 3
  days) - All repos table with live git stats - Recent run history (last 10 runs)

Includes stats.py module and full test coverage in test_stats.py.

- Add tracking data models and JSONL storage
  ([`f340dab`](https://github.com/datapointchris/syncer/commit/f340dabba775e920f8678859ce9ff6c7abe8b58a))

This introduces a tracking module to record sync run data for future analytics and stale repo
  detection. Adds DATA_DIR constant pointing to ~/.local/share/syncer/ for XDG-compliant data
  storage.

New models: - RepoSnapshot: captures repo state (status, branch, counts) - RunSummary: aggregates
  results of a sync run - SyncRunEvent: timestamped record of a complete sync operation

Storage functions emit/read events to JSONL file. Includes find_stale_repos() to identify repos with
  uncommitted changes persisting across multiple runs over a threshold period.

- Emit tracking events and warn about stale repos
  ([`7281df1`](https://github.com/datapointchris/syncer/commit/7281df1ab11421220650cba629e04c05c428d496))

sync_repos now builds RepoSnapshot for each repo (across all status paths), times the sync run, and
  emits a SyncRunEvent to JSONL after each non-dry-run sync. After emitting, it reads back events to
  detect and warn about repos with long-standing uncommitted changes.

main.py passes the resolved config name through to enable tracking.

### Testing

- Expand repos test suite and fix stale ref handling
  ([`6664be3`](https://github.com/datapointchris/syncer/commit/6664be38ce226ac41fc76055e93c374fea006efe))

Improve default_branch to validate that origin/HEAD tracking refs point to existing branches,
  falling back to local detection if the ref is stale (points to deleted branch).

Add rename_master_to_main method that handles partial states from previous attempts idempotently -
  checks each step (local rename, remote push, GitHub default, remote master deletion, origin/HEAD
  update) and only performs needed actions.

Expand test suite from 24 to 48 tests covering: - Display width calculations for icons and text -
  Status line formatting with/without branch names - Stale origin/HEAD ref handling and fallback
  logic - Rename idempotency in various partial states - Fork detection via gh CLI - Behind/unpushed
  commit tracking - Stash count


## v1.2.0 (2026-02-17)

### Chores

- **release**: 1.2.0
  ([`e8b41b1`](https://github.com/datapointchris/syncer/commit/e8b41b18fa4b11059165280b21d2ab4da6c452e0))

### Features

- Improve doctor output and add fork detection
  ([`9d22f01`](https://github.com/datapointchris/syncer/commit/9d22f01f8dcd5600841e12f41c2bf8c5344b0a6b))

Doctor now streams output as it goes with formatted status lines matching sync output. Config paths
  (e.g. ~/tools/syncer) are shown instead of repo names for clarity. Fork detection via gh repo view
  prevents attempting to rename master→main on forks. Warnings are yellow, local-changes errors are
  red. Replaced [dim] markup with [white] for readability.


## v1.1.0 (2026-02-17)

### Chores

- **release**: 1.1.0
  ([`6b0917b`](https://github.com/datapointchris/syncer/commit/6b0917bc2145bab87c0d57d7ac01d6609e6f7b85))

### Features

- Add nerd font icons, auto-pull/push, demo command, and doctor master detection
  ([`268d266`](https://github.com/datapointchris/syncer/commit/268d266fc115f89d873a908ea9d1602f5776a07a))

Enhances syncer with visual improvements and automation:

- Add nerd font icons (✓, ⚠, ✗, etc.) for better visual status - Column-aligned output with
  underscore padding for consistent layout - Auto-pull for repos cleanly behind remote (no local
  changes) - Auto-push for repos with unpushed commits (no uncommitted changes) - New `syncer demo`
  command that creates real temp git repos in various states - Doctor command now detects repos
  still on master branch with --fix to rename to main - Detailed file/commit listing shown under
  repos with issues - Summary line shows counts: synced, pulled, pushed, attention needed


## v1.0.0 (2026-02-17)

### Bug Fixes

- Add .profile back in and include poetry lock
  ([`3c02fbb`](https://github.com/datapointchris/syncer/commit/3c02fbb3fce014c79a4a9280c61bd6176c984295))

- Exiting in too far outer block for create-release
  ([`24a9f36`](https://github.com/datapointchris/syncer/commit/24a9f3685c529b24a599b1453ade91bd243ec43e))

- Github version command can now be run from any directory
  ([`3ff2fb1`](https://github.com/datapointchris/syncer/commit/3ff2fb19a5f9518739c9d8dbc9c7d51d6393cc4f))

- Handle case when target is exisiting directory
  ([`e7c22f1`](https://github.com/datapointchris/syncer/commit/e7c22f156aeeedeef9c9a443de66649b6dd0cd9c))

In the case that the target isn't a symlink already (meaning an update) then the target is renamed
  with suffix '_bak' and the symlink is created. Avoiding period '.' in the name since some symlinks
  are directories.

- Remove -a from git tag command
  ([`123cc43`](https://github.com/datapointchris/syncer/commit/123cc43e15d9ef06399cb0f4a40f39d8f511f173))

- Remove /etc/hosts symlink for permission errors
  ([`1984342`](https://github.com/datapointchris/syncer/commit/1984342852c170da6bb9316bca27cdfdc9dcd522))

- Remove zsh-autosuggestions from plugins sync, they were awful and distracting
  ([`a146965`](https://github.com/datapointchris/syncer/commit/a146965a4c67e68c156846ca71e2b0d2355c18df))

- Reverse string quotes in git tag for shell command
  ([`614f70a`](https://github.com/datapointchris/syncer/commit/614f70ac27cef3b9ab6842ab7b7ce54b69835093))

- Shell=true without split for subprocess.call
  ([`7f5bd09`](https://github.com/datapointchris/syncer/commit/7f5bd09d1f2a6240b4b05f4e136d7bc5bf898002))

- Subprocess.call does not need commands split
  ([`f1949a1`](https://github.com/datapointchris/syncer/commit/f1949a1a621755f8bf711f2bddf32d71a7700a39))

- Update github version command to return exact version
  ([`45c4e4a`](https://github.com/datapointchris/syncer/commit/45c4e4a2fb28f8ac080a0b5b65d9a1f75f3cca8d))

- Use root logger in main
  ([`36266a6`](https://github.com/datapointchris/syncer/commit/36266a6d44e53f672a604bd557b02ce868706d02))

- Use shell=True for git tag shell command
  ([`cc1e344`](https://github.com/datapointchris/syncer/commit/cc1e344209591c85aa73d06f96224b774e2dac99))

### Build System

- Create release 0.6.1 - Add logging when creating a release
  ([`e944b66`](https://github.com/datapointchris/syncer/commit/e944b663e99a7728f96557966195884a5db929dc))

- Create release 0.6.3 - Sync .profile dotfile
  ([`b221ead`](https://github.com/datapointchris/syncer/commit/b221ead4d8057eed44f95d5aaf82320b81a09bdd))

- Create release 0.6.4 - Get latest version functionality
  ([`ce2e916`](https://github.com/datapointchris/syncer/commit/ce2e916a6d8a394815ccd2cad475cc7908b6af7a))

- Create release 0.6.5 - Add --force to update command
  ([`da62d51`](https://github.com/datapointchris/syncer/commit/da62d511a01d195369de81cbc81f82deab2d0dc3))

- Create release 0.7.1 - Update dependencies
  ([`4c1593d`](https://github.com/datapointchris/syncer/commit/4c1593d88a968e4b1164fe40e3d3386640c36fef))

- Create release 0.8.0 - Add help text
  ([`5914174`](https://github.com/datapointchris/syncer/commit/591417455093b30e2cddf36134c43c16b294e126))

- Create release 0.8.1 - Remove old github projects
  ([`5eef84a`](https://github.com/datapointchris/syncer/commit/5eef84a58b051f55fa3faf6fe98723ac4da0aeb1))

- Create release 0.8.2 - Remove misc projects
  ([`5125b9b`](https://github.com/datapointchris/syncer/commit/5125b9b3ac467074fe7c4954929ed3487f91a00e))

- Create release 0.8.4 - Minor fix: poetry lock update and re-adding .profile
  ([`17a74d6`](https://github.com/datapointchris/syncer/commit/17a74d6d08b77bc43f227a1d619c6c53b657cfec))

- Create release 0.9.0 - streamlined create-release
  ([`e89e153`](https://github.com/datapointchris/syncer/commit/e89e153a69ea10cf9ba1c5e112d0492db36ee455))

- Create release 0.9.1 - run update from any local directory
  ([`c5a6726`](https://github.com/datapointchris/syncer/commit/c5a6726dacef1a7b65838b4eb3c429afe456abba))

- Create release 0.9.2 - Add chatter to repos
  ([`c9359f1`](https://github.com/datapointchris/syncer/commit/c9359f1b18c118e25844533796b3117de477c0d7))

- Create release 0.9.3 - Move applicable dotfile configs to XDG config dir
  ([`dae320e`](https://github.com/datapointchris/syncer/commit/dae320effddec1b48f210c918add56208ed2de7f))

- Create release 0.9.4 - Handle existing directories in dotfiles
  ([`2d7852f`](https://github.com/datapointchris/syncer/commit/2d7852f08a89a0698a73761dc47ade7f797b8f23))

- Create release 0.9.5 - Add youtube-playlists to synced repos
  ([`ef4cb05`](https://github.com/datapointchris/syncer/commit/ef4cb056b0c2a2b8200db883d5c3863a733c2f6b))

- Create release 0.9.6 - Add more dotfiles to sync
  ([`e945a1f`](https://github.com/datapointchris/syncer/commit/e945a1f9ab83be767b8f5e5169413c4400ddc50b))

- Create release 0.9.7 - Add /etc/hosts to symlinks
  ([`67c225f`](https://github.com/datapointchris/syncer/commit/67c225f33ca6738de435abfbdd83fd80d4d4680a))

- Create release 0.9.8 - Remove /etc/hosts symlink
  ([`e545374`](https://github.com/datapointchris/syncer/commit/e545374d42c77a83a147fb4710f388e0bf154615))

- Create release 0.9.9 - Remove ichrisbirch from synced bin
  ([`ec28544`](https://github.com/datapointchris/syncer/commit/ec28544be1afb86841faeba8c4aadeb510f84da5))

- Update dependencies
  ([`28047c2`](https://github.com/datapointchris/syncer/commit/28047c2fd237d7ffeb88fcc815892ebcee22c427))

### Chores

- Move data folder inside package to be included with install
  ([`57e72e7`](https://github.com/datapointchris/syncer/commit/57e72e7624d02a6b25a00fd1f8042f64b0a8828b))

- Remove dead python projects
  ([`71bfc7b`](https://github.com/datapointchris/syncer/commit/71bfc7bd5803f981c3aa18c4c5e4b40e8e8f080c))

- Remove unused main command
  ([`42282fa`](https://github.com/datapointchris/syncer/commit/42282faae2bda08a876b9fcc1d2eb914a5745319))

- Update deps
  ([`e442ffe`](https://github.com/datapointchris/syncer/commit/e442ffe40db90b7a0cbcbb80e7db0e55e2b1bcc9))

- Update gitignore to remove __pycache__
  ([`875e577`](https://github.com/datapointchris/syncer/commit/875e57713a739c0c9abc9e1ce8592f2cffc49a56))

- Update python version
  ([`e1f9ac9`](https://github.com/datapointchris/syncer/commit/e1f9ac948c69978950700de6fcddf427fada591e))

- **release**: 1.0.0
  ([`94ec373`](https://github.com/datapointchris/syncer/commit/94ec373337e8e2c669af4d484ad9a5871089216b))

### Documentation

- Add auto generated docs to README.md
  ([`7258f2e`](https://github.com/datapointchris/syncer/commit/7258f2e9bd2b4ea7115a258ebe545e504c598e52))

- Add warning for running syncer plugins inside of tmux
  ([`cd1cf28`](https://github.com/datapointchris/syncer/commit/cd1cf28695b72f5835a630cb24f7557c5de0134d))

- Correct readme with push instructions
  ([`bd9b6ad`](https://github.com/datapointchris/syncer/commit/bd9b6adfa0b72b4f2c19f1fe63e15a33105ecf99))

- Update README with install and update instructions
  ([`fed9e6b`](https://github.com/datapointchris/syncer/commit/fed9e6b5acd8ffbee5d49f6f91b5cdd5908f8c1a))

### Features

- Add --force flag to update command
  ([`b8e36b6`](https://github.com/datapointchris/syncer/commit/b8e36b6ea23ee6a333259e88765db1272f1b01f2))

- Add .profile to universal dotfiles to sync for rust installation
  ([`03318a4`](https://github.com/datapointchris/syncer/commit/03318a46d747581f9359bdc6a44d1a8fa30fa505))

- Add /etc/hosts symlink for macos
  ([`79414b6`](https://github.com/datapointchris/syncer/commit/79414b67523ef6950bf6da3f5cfdeab9c5050d30))

- Add 1904labs projects to sync
  ([`917402f`](https://github.com/datapointchris/syncer/commit/917402f6d48ced4e098176eb252e1b13f0e5c7f9))

- Add aerospace, eza, zellij to synced dotfiles
  ([`0b72ec7`](https://github.com/datapointchris/syncer/commit/0b72ec7d7cf587e44fafddae23b8b1acc00f0623))

- Add capability to check for main branch instead of master branch
  ([`03b10eb`](https://github.com/datapointchris/syncer/commit/03b10eb693bd9d950ecbb384450c4674c96343b7))

- Add chatter to repo list
  ([`841939d`](https://github.com/datapointchris/syncer/commit/841939d82dc0187bd5979263630f1ccafe127883))

- Add commit to create-release and inline logging for Repo
  ([`848c4de`](https://github.com/datapointchris/syncer/commit/848c4de5cf6a283e88a80fce01f5c7234439267c))

- Add extra help text to syncer for install and update
  ([`24b52a8`](https://github.com/datapointchris/syncer/commit/24b52a85c9ca4fed48547688c5c16b2805d611bd))

- Add logging to create_release
  ([`e244bbc`](https://github.com/datapointchris/syncer/commit/e244bbcc3bf54806ecc49aa533378138269c7b57))

- Add plugin type to plugins sync
  ([`a948e36`](https://github.com/datapointchris/syncer/commit/a948e36b14efd2e1efb540c2ce63fefb14429378))

- Add readme command to syncer to display help text
  ([`b1b4625`](https://github.com/datapointchris/syncer/commit/b1b4625c36ad5a86032bf5bbb81ed76c687b3d1b))

- Add repo type to repos sync
  ([`94e3dd7`](https://github.com/datapointchris/syncer/commit/94e3dd7b1a624fc8faac24b3ca3c6556915b68e2))

- Add spacing and use long form of --quiet for pip install
  ([`d5a95e1`](https://github.com/datapointchris/syncer/commit/d5a95e14f91c2f7d9123f9c384e0d65b722d76bf))

- Add testpaths command for testing pathlib
  ([`0907a56`](https://github.com/datapointchris/syncer/commit/0907a567429dc6e04bcee79c238879e82922bec9))

- Add update command to update the user installed syncer package
  ([`9156544`](https://github.com/datapointchris/syncer/commit/91565444b1f2348e7eee11fffdce1b214d1463df))

- Add version command
  ([`3ebce5b`](https://github.com/datapointchris/syncer/commit/3ebce5bdc898152589205f6706ce37db4cc6821c))

- Add youtube-playlists to synced repos
  ([`645b8e2`](https://github.com/datapointchris/syncer/commit/645b8e2cc551f33ba9408cad9072d05fa4a403e0))

- Create convert_readme_to_help_text for adding readme help to command line
  ([`8a8c5a6`](https://github.com/datapointchris/syncer/commit/8a8c5a6d4d5902c841175f97b174d34010cb1065))

- Create create-release function
  ([`ce844e1`](https://github.com/datapointchris/syncer/commit/ce844e1bba41b5c2b27ac32e07e23e8874b858f2))

- Move some dotfiles into XDG_CONFIG_HOME (~.config)
  ([`35df261`](https://github.com/datapointchris/syncer/commit/35df2611f0336c7c11bc9f471b9aebae0194e35c))

- Redesign syncer as repo-sync-only tool with uv build system
  ([`d6126f8`](https://github.com/datapointchris/syncer/commit/d6126f84d5dc2b50a19019f64a8eab238ff9ef71))

Complete redesign focusing on repository synchronization:

Build system changes: - Migrated from Poetry to uv package manager - Adopted src/ layout
  (src/syncer/ instead of syncer/) - Updated to Python 3.13 - Added GitHub Actions release workflow
  with python-semantic-release

Config system rewrite: - Moved to JSON-based config at ~/.config/syncer/ - Added auto-detection for
  GitHub repos - New 'doctor' command for diagnostics - New 'init' command for setup

Core functionality changes: - Rewrote repo sync using subprocess.run with cwd= (removed os.chdir) -
  Replaced colorama with rich for better output - Rewrote CLI with sync/doctor/version/update/init
  commands - Added comprehensive test suite (24 passing tests)

Removed features: - Deleted dotfiles sync (breaking change) - Deleted plugins system (breaking
  change) - Removed create_release, update, utilities, testpaths modules

Infrastructure updates: - Updated pre-commit config for uv workflow - Added .python-version,
  .shellcheckrc - Cleaned up .gitignore for new structure

- Remove deactivate from update function, did not work
  ([`6c46ebc`](https://github.com/datapointchris/syncer/commit/6c46ebc5ba8bc40e7e235d19515ac0f7c3803ffd))

- Remove ichrisbirch from synced bin
  ([`fc1881c`](https://github.com/datapointchris/syncer/commit/fc1881c4f0c457c1b5e9c52df589f8661a33d91e))

- Separate dotfiles config and add syncer config
  ([`520f544`](https://github.com/datapointchris/syncer/commit/520f54486784779755aa2ece27bb0025574fa6b9))

- Update and remove old projects
  ([`4960a2d`](https://github.com/datapointchris/syncer/commit/4960a2d3e353f32d6f4f03e9213430b43019d730))

- Update project to work in any directory
  ([`6154487`](https://github.com/datapointchris/syncer/commit/6154487f8d1d7cd1acb15ea391881fe84265885a))

- Upgrade update to use github release instead of manual install of wheel
  ([`903008c`](https://github.com/datapointchris/syncer/commit/903008cddbfe5b7f9ca0bf78e5be2bc944ceee21))

- Use dynamic wheel path instead of hardcoded version
  ([`22ebf6b`](https://github.com/datapointchris/syncer/commit/22ebf6bda2ad7929e9e1f9bfbb57e3a841c58c1d))

### Refactoring

- Change pathlib.Path to Path
  ([`f62a514`](https://github.com/datapointchris/syncer/commit/f62a514fe70eb0215a8893c101ee8c9a10f5cc28))

- Create repo class to encapsulate state
  ([`b4b826f`](https://github.com/datapointchris/syncer/commit/b4b826f0ef77be425c1137ff142e19c970f3b280))

- Get latest version functionality to separate function
  ([`9a2d565`](https://github.com/datapointchris/syncer/commit/9a2d565b21cc911413bbc0b3b7095680691eb2ee))

- Move tmux plugins into XDG_DATA_DIRECTORY
  ([`85c7597`](https://github.com/datapointchris/syncer/commit/85c7597abe7b57050de3252fee809c6789bed217))

- Remove source and target config subclasses
  ([`6d6a7bf`](https://github.com/datapointchris/syncer/commit/6d6a7bfce0c1abc317299917261130571743a98d))

- Rename projects to repos
  ([`d27f875`](https://github.com/datapointchris/syncer/commit/d27f875e8a6cdf60ae572cb76140b0b7eb4f2245))

- Restructure 1904labs repos
  ([`dd50894`](https://github.com/datapointchris/syncer/commit/dd508942d6bfb258ad98d9d1cc4e62c4bd268403))

- Restructure datapointchris repos
  ([`7b520b2`](https://github.com/datapointchris/syncer/commit/7b520b201ab622c6468756306228d567a6f96b1b))

- Split plugins and projects by type
  ([`27a9166`](https://github.com/datapointchris/syncer/commit/27a9166bf4a8bc41522e7566a3fd88b230fc1869))
