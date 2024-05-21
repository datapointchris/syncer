import subprocess
from pathlib import Path


def get_installed_version():
    output = subprocess.run('pipx list'.split(), capture_output=True, text=True).stdout
    syncer_info = output[output.find('syncer') :].split('\n')[0]
    current_version = syncer_info.split(',')[0].split(' ')[1]
    return current_version


def get_latest_github_version():
    github_version_command = (
        'gh release --repo https://www.github.com/datapointchris/syncer view --json tagName --jq .tagName'.split()
    )
    github_version = subprocess.run(github_version_command, capture_output=True, text=True).stdout.strip()
    return github_version


def convert_readme_to_help_text(readme: Path):
    text = readme.read_text()
    print(text)
