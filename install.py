#!/usr/bin/env python3
"""Install user-owned files; let Omarchy enable the widget in its current bar."""
import argparse
from datetime import datetime
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time

from backend import load_config

SOURCE = Path(__file__).resolve().parent
FILES = ('manifest.json', 'Panel.qml', 'backend.py', 'history.py', 'notifications.py', 'workflows.example.json', 'i18n/en.json')
PLUGIN = 'ianzano.workflows'


def atomic_copy(source, target):
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_symlink():
        raise ValueError(f'Refusing to replace symlink: {target}')
    fd, name = tempfile.mkstemp(prefix='.workflows-', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(source.read_bytes())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--no-enable', action='store_true', help='Copy files without contacting the desktop shell')
    args = parser.parse_args()
    home = Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'omarchy'
    destination = home / 'plugins' / PLUGIN
    config = home / 'workflows.json'
    stamp = datetime.now().strftime('%Y%m%d-%H%M%S-%f')
    for name in FILES:
        if not (SOURCE / name).is_file():
            raise ValueError(f'Missing source file: {name}')
    load_config(config if config.exists() else SOURCE / 'workflows.example.json')
    subprocess.run(['omarchy', 'plugin', 'validate', str(SOURCE)], check=True)
    if destination.is_symlink():
        raise ValueError('Plugin installation directory must not be a symlink')
    if destination.exists() and destination.resolve() != SOURCE:
        backup = home / 'plugin-backups' / (PLUGIN + '-' + stamp)
        backup.mkdir(mode=0o700, parents=True)
        shutil.copytree(destination, backup / 'files', symlinks=True)
        print('Plugin backup:', backup)
    if destination.resolve() != SOURCE:
        for name in FILES:
            atomic_copy(SOURCE / name, destination / name)
    if not config.exists():
        # Exclusive creation preserves user config on subsequent installs.
        fd = os.open(config, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, 'wb') as stream:
            stream.write((SOURCE / 'workflows.example.json').read_bytes())
    print('Installed:', destination)
    print('Workflow config:', config)
    if not args.no_enable:
        shell_config = home / 'shell.json'
        if shell_config.exists():
            fd, backup = tempfile.mkstemp(prefix='shell.json.bak.workflows-' + stamp + '-', dir=home)
            with os.fdopen(fd, 'wb') as stream:
                stream.write(shell_config.read_bytes())
            print('Bar config backup:', backup)
        subprocess.run(['omarchy-shell', 'shell', 'rescanPlugins'], check=True)
        # Without placement flags, enabling preserves any existing slot/settings.
        # Registry scans are asynchronous; IPC can return before discovery finishes.
        for attempt in range(20):
            result = subprocess.run(['omarchy', 'plugin', 'enable', PLUGIN], capture_output=True, text=True)
            if result.returncode == 0:
                print(result.stdout.strip())
                break
            if not any(message in result.stderr for message in ('not known', 'not ready')):
                raise RuntimeError(result.stderr.strip())
            time.sleep(0.1)
        else:
            raise RuntimeError('Plugin files installed, but Omarchy discovery did not finish: ' + result.stderr.strip())


if __name__ == '__main__':
    main()
