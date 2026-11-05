#!/usr/bin/env python3
"""Trusted config -> local/SSH workflow, with bounded streaming JSON output."""
import argparse
import codecs
import fcntl
import json
import os
from pathlib import Path
import re
import selectors
import shlex
import signal
import stat
import subprocess
import sys
import time
import uuid

from history import RunStore
from notifications import send_result

MAX_CONFIG = 256 * 1024
ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z')
HOST = re.compile(r'(?:[a-zA-Z0-9_.-]+@)?[a-zA-Z0-9][a-zA-Z0-9_.:-]*\Z')


def config_path():
    return Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'omarchy/workflows.json'


def string(value, label, limit=8192):
    if not isinstance(value, str) or not value.strip() or '\x00' in value or len(value) > limit:
        raise ValueError(f'{label} must be a nonempty string (max {limit} characters)')
    return value


def validate_icon(value, label):
    string(value, label, 16)
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(f'{label} must be a single-line glyph or emoji')


def read_config(path):
    with open(path, 'rb') as stream:
        raw = stream.read(MAX_CONFIG + 1)
    if len(raw) > MAX_CONFIG:
        raise ValueError('Configuration exceeds 256 KiB')
    data = json.loads(raw)
    if not isinstance(data, dict) or data.get('version') != 1 or isinstance(data.get('version'), bool):
        raise ValueError('Configuration requires version: 1')
    if set(data) - {'version', 'groups', 'workflows'}:
        raise ValueError('Unknown configuration field')
    groups = data.get('groups', [])
    if not isinstance(groups, list) or len(groups) > 100:
        raise ValueError('groups must be an array with at most 100 entries')
    group_ids = set()
    for group in groups:
        if not isinstance(group, dict) or set(group) - {'id', 'title', 'description', 'icon'}:
            raise ValueError('Unknown group field or invalid group object')
        key = string(group.get('id'), 'group.id', 64)
        if not ID.fullmatch(key) or key in group_ids:
            raise ValueError('Group IDs must be unique and use letters, numbers, underscores or dashes')
        group_ids.add(key)
        string(group.get('title'), f'group.{key}.title', 160)
        if 'description' in group:
            string(group['description'], f'group.{key}.description', 500)
        if 'icon' in group:
            validate_icon(group['icon'], f'group.{key}.icon')
    items = data.get('workflows')
    if not isinstance(items, list) or len(items) > 100:
        raise ValueError('workflows must be an array with at most 100 entries')
    seen = set()
    for item in items:
        if not isinstance(item, dict) or set(item) - {'id', 'name', 'description', 'host', 'cwd', 'steps', 'timeout', 'notifications', 'group', 'icon'}:
            raise ValueError('Unknown workflow field or invalid workflow object')
        key = string(item.get('id'), 'id', 64)
        if not ID.fullmatch(key) or key in seen:
            raise ValueError('Workflow IDs must be unique and use letters, numbers, underscores or dashes')
        seen.add(key)
        string(item.get('name'), f'{key}.name', 160)
        if item.get('group') is not None:
            reference = string(item['group'], f'{key}.group', 64)
            if reference not in group_ids:
                raise ValueError(f'{key}.group refers to an undefined group: {reference}')
        if 'icon' in item:
            validate_icon(item['icon'], f'{key}.icon')
        if 'description' in item:
            string(item['description'], f'{key}.description', 500)
        if 'host' in item:
            host = string(item['host'], f'{key}.host', 255)
            if not HOST.fullmatch(host):
                raise ValueError('host must be an SSH alias or user@hostname; configure ports/keys in ~/.ssh/config')
        if 'cwd' in item:
            string(item['cwd'], f'{key}.cwd', 4096)
        if 'notifications' in item and type(item['notifications']) is not bool:
            raise ValueError('notifications must be true or false')
        timeout = item.get('timeout', 1800)
        if type(timeout) is not int or not 1 <= timeout <= 86400:
            raise ValueError('timeout must be an integer from 1 to 86400 seconds')
        steps = item.get('steps')
        if not isinstance(steps, list) or not 1 <= len(steps) <= 100:
            raise ValueError('steps must contain 1–100 shell strings or argument arrays')
        for step in steps:
            if isinstance(step, list):
                if not 1 <= len(step) <= 100:
                    raise ValueError('Argument arrays must contain 1–100 arguments')
                for arg in step:
                    # Empty arguments are valid argv entries.
                    if not isinstance(arg, str) or '\x00' in arg or len(arg) > 8192:
                        raise ValueError('Invalid command argument')
                string(step[0], 'executable')
            else:
                string(step, 'step')
    return dict(version=1, workflows=items, groups=groups)


def load_config(path):
    """Keep the flat workflow API used by the installer and existing callers."""
    return read_config(path)['workflows']


def script_for(item):
    lines = ['set -e']
    if item.get('host') and item.get('cwd'):
        lines.append('cd -- ' + shlex.quote(item['cwd']))
    for number, step in enumerate(item['steps'], 1):
        command = shlex.join(step) if isinstance(step, list) else step
        label = f'[{number}/{len(item["steps"])}] $ {command}'
        lines.append("printf '%s\\n' " + shlex.quote(label))
        # Each step has its own fail-fast shell; no command substitution/eval around argv.
        lines.append('sh -ec ' + shlex.quote(command))
    return '\n'.join(lines)


def command_for(item):
    script = script_for(item)
    if item.get('host'):
        return ['ssh', '-T', '-o', 'BatchMode=yes', '-o', 'ConnectTimeout=10', '--',
                item['host'], 'sh -c ' + shlex.quote(script)]
    return ['sh', '-c', script]


def emit(event, **fields):
    try:
        print(json.dumps({'event': event, **fields}, ensure_ascii=True), flush=True)
    except BrokenPipeError:
        # A shell shutdown can close stdout before stdin signals cancellation.
        # Still persist and notify once; delivery failure is not a second result.
        fd = os.open(os.devnull, os.O_WRONLY)
        try:
            os.dup2(fd, sys.stdout.fileno())
        finally:
            os.close(fd)


class AlreadyRunning(ValueError):
    pass


def acquire_lock(key):
    base = Path(os.environ.get('XDG_RUNTIME_DIR', '/tmp')) / f'omarchy-workflows-{os.getuid()}'
    base.mkdir(mode=0o700, exist_ok=True)
    fd = os.open(base, os.O_DIRECTORY | os.O_NOFOLLOW)
    info = os.fstat(fd)
    if info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o700:
        os.close(fd)
        raise ValueError('Workflow lock directory must be owned by you with mode 0700')
    try:
        lock = os.open(key + '.lock', os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600, dir_fd=fd)
    finally:
        os.close(fd)
    info = os.fstat(lock)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(lock)
        raise ValueError('Unsafe workflow lock file')
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(lock)
        raise AlreadyRunning('This workflow is already running') from None
    return lock


def latest_run(key):
    store = RunStore()
    record = store.read(key)
    if not record or record['status'] != 'running':
        return record
    try:
        lock = acquire_lock(key)
    except AlreadyRunning:
        return record
    try:
        # Recheck under the execution lock: a new run may have started meanwhile.
        record = store.read(key)
        if record and record['status'] == 'running':
            record = dict(record, status='interrupted')
            store.write(key, record)
        return record
    finally:
        os.close(lock)


def finish_run(item, record, status, code, message):
    record = dict(record, status=status, code=code, completedAt=int(time.time() * 1000))
    warning = ''
    try:
        RunStore().write(item['id'], record)
    except (OSError, ValueError):
        warning = 'Could not save the latest run summary.'
    if item.get('notifications', True):
        try:
            send_result(item['name'], status)
        except Exception:
            # D-Bus exception strings can contain notification payloads.
            warning += (' ' if warning else '') + 'Desktop notification could not be delivered.'
    emit('result', ok=status == 'completed', code=code, message=message,
         lastRun=record, warning=warning)
    return 0 if status == 'completed' else 1


def run_workflow(item, panel=False):
    lock = acquire_lock(item['id'])
    child = None
    stopping = None
    reason = ''
    old_handlers = {}
    record = None
    def request_stop(signum, frame):
        nonlocal reason
        reason = 'Cancelled'
    try:
        for sig in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
            old_handlers[sig] = signal.signal(sig, request_stop)
        record = dict(runId=uuid.uuid4().hex, startedAt=int(time.time() * 1000),
                      completedAt=None, status='running', code=None)
        RunStore().write(item['id'], record)
        emit('started', lastRun=record)
        child = subprocess.Popen(command_for(item), cwd=None if item.get('host') else item.get('cwd'),
                                 stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, start_new_session=True)
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        started = time.monotonic()
        with selectors.DefaultSelector() as selector:
            selector.register(child.stdout, selectors.EVENT_READ, 'output')
            try:
                if panel:
                    selector.register(sys.stdin, selectors.EVENT_READ, 'control')
            except (OSError, ValueError, PermissionError):
                pass
            output_open = True
            while output_open or child.poll() is None:
                now = time.monotonic()
                if not reason and now - started >= item.get('timeout', 1800):
                    reason = 'Timed out'
                if reason and stopping is None:
                    stopping = now
                    try:
                        os.killpg(child.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                if stopping is not None and now - stopping >= 1:
                    try:
                        os.killpg(child.pid, signal.SIGKILL)
                    except ProcessLookupError:
                        pass
                for key, _ in selector.select(0.05):
                    chunk = os.read(key.fileobj.fileno(), 4096)
                    if key.data == 'control':
                        # Any input cancels; EOF also stops jobs when the shell exits.
                        reason = reason or 'Cancelled'
                        selector.unregister(key.fileobj)
                    elif chunk:
                        emit('output', text=decoder.decode(chunk))
                    else:
                        output_open = False
                        selector.unregister(key.fileobj)
            tail = decoder.decode(b'', final=True)
            if tail:
                emit('output', text=tail)
        code = child.wait()
        status = 'cancelled' if reason == 'Cancelled' else 'timeout' if reason else 'completed' if code == 0 else 'failed'
        return finish_run(item, record, status, code,
                          reason or ('Completed' if code == 0 else f'Failed (exit {code})'))
    except (OSError, ValueError) as error:
        if record is None:
            raise
        return finish_run(item, record, 'failed', None, str(error))
    finally:
        if child is not None:
            try:
                os.killpg(child.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            child.wait()
            child.stdout.close()
        for sig, handler in old_handlers.items():
            signal.signal(sig, handler)
        os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, default=config_path())
    parser.add_argument('action', choices=['list', 'run', 'check', 'edit'])
    parser.add_argument('id', nargs='?')
    parser.add_argument('--panel', action='store_true', help='Cancel on stdin input or disconnect')
    args = parser.parse_args()
    try:
        if args.action == 'edit':
            subprocess.Popen(['omarchy', 'launch', 'editor', str(args.config)], start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            emit('result', ok=True)
            return 0
        config = read_config(args.config)
        items = config['workflows']
        if args.action in ('list', 'check'):
            if args.action == 'list':
                items = [dict(item, lastRun=latest_run(item['id'])) for item in items]
            emit('result', ok=True, workflows=items, groups=config['groups'], path=str(args.config))
            return 0
        item = next((w for w in items if w['id'] == args.id), None)
        if item is None:
            raise ValueError('Unknown workflow ID')
        return run_workflow(item, args.panel)
    except (OSError, ValueError) as error:
        emit('result', ok=False, message=str(error))
        return 1


if __name__ == '__main__':
    sys.exit(main())
