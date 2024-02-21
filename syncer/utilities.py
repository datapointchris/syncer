import subprocess


def get_installed_version():
    output = subprocess.run('pipx list'.split(), capture_output=True, text=True).stdout
    syncer_info = output[output.find('syncer') :].split('\n')[0]
    current_version = syncer_info.split(',')[0].split(' ')[1]
    return current_version
