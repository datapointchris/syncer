"""The working clone of a local bare remote that most tests start from.

A real remote on disk lets fetch, push and ls-remote succeed with no network, so a test exercises
the git calls syncer makes rather than a stand-in for them.
"""

import subprocess
from pathlib import Path


def _git(path: Path, *args: str) -> None:
    subprocess.run(['git', *args], cwd=path, capture_output=True, text=True)


def make_cloned_repo(tmp_path: Path, name: str, *, remote: str | None = None) -> Path:
    """Clone `<remote or name>.git` under tmp_path to tmp_path/name, with one commit pushed to main.

    `remote` names the bare apart from the clone, for a test that later clones the same remote
    again under another name.
    """
    bare = tmp_path / f'{remote or name}.git'
    subprocess.run(['git', 'init', '--bare', '-b', 'main', str(bare)], capture_output=True)
    repo_path = tmp_path / name
    subprocess.run(['git', 'clone', str(bare), str(repo_path)], capture_output=True)
    _git(repo_path, 'config', 'user.email', 'test@test.com')
    _git(repo_path, 'config', 'user.name', 'Test')
    (repo_path / 'README.md').write_text('# Test\n')
    _git(repo_path, 'add', 'README.md')
    _git(repo_path, 'commit', '-m', 'init')
    _git(repo_path, 'push', '-u', 'origin', 'main')
    return repo_path
