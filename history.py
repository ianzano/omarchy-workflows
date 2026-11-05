"""Private, atomic latest-run summaries; no workflow commands or output on disk."""
import json
import os
from pathlib import Path
import re
import stat
import uuid

ID = re.compile(r'[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}\Z')
STATUSES = {'running', 'completed', 'failed', 'cancelled', 'timeout', 'interrupted'}


def state_path():
    return Path(os.environ.get('XDG_STATE_HOME', Path.home() / '.local/state')) / 'omarchy/workflows'


def valid_record(data):
    if not isinstance(data, dict) or set(data) != {'runId', 'startedAt', 'completedAt', 'status', 'code'}:
        return False
    if not isinstance(data['runId'], str) or not re.fullmatch(r'[0-9a-f]{32}', data['runId']):
        return False
    if not isinstance(data['status'], str) or data['status'] not in STATUSES:
        return False
    for key in ('startedAt', 'completedAt'):
        value = data[key]
        if key == 'completedAt' and value is None:
            continue
        if type(value) is not int or not 0 < value < 8640000000000000:
            return False
    if data['status'] in ('running', 'interrupted') and data['completedAt'] is not None:
        return False
    if data['status'] not in ('running', 'interrupted') and data['completedAt'] is None:
        return False
    return data['code'] is None or type(data['code']) is int


class RunStore:
    def __init__(self):
        self.path = state_path()

    def directory(self, create=False):
        if create:
            self.path.mkdir(mode=0o700, parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_DIRECTORY | os.O_NOFOLLOW)
        info = os.fstat(fd)
        if info.st_uid != os.getuid() or info.st_mode & 0o077:
            os.close(fd)
            raise ValueError('Workflow history directory must be private and owned by you (0700)')
        return fd

    @staticmethod
    def filename(key):
        if not isinstance(key, str) or not ID.fullmatch(key):
            raise ValueError('Invalid workflow history ID')
        return key + '.json'

    def read(self, key):
        filename = self.filename(key)
        try:
            directory = self.directory()
        except FileNotFoundError:
            return None
        try:
            try:
                fd = os.open(filename, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
            except FileNotFoundError:
                return None
            with os.fdopen(fd, 'rb') as stream:
                info = os.fstat(stream.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                    raise ValueError('Unsafe workflow history file')
                raw = stream.read(4097)
            if len(raw) > 4096:
                raise ValueError('Workflow history record exceeds 4 KiB')
            data = json.loads(raw)
            if not valid_record(data):
                raise ValueError('Invalid workflow history record')
            return data
        finally:
            os.close(directory)

    def write(self, key, record):
        filename = self.filename(key)
        if not valid_record(record):
            raise ValueError('Invalid workflow history record')
        directory = self.directory(create=True)
        temporary = '.' + key + '-' + uuid.uuid4().hex
        try:
            fd = os.open(temporary, os.O_CREAT | os.O_EXCL | os.O_WRONLY | os.O_NOFOLLOW, 0o600, dir_fd=directory)
            with os.fdopen(fd, 'w') as stream:
                json.dump(record, stream)
                stream.write('\n')
                stream.flush()
                os.fsync(stream.fileno())
            # Replace the directory entry rather than following a destination symlink.
            os.replace(temporary, filename, src_dir_fd=directory, dst_dir_fd=directory)
            os.fsync(directory)
        finally:
            try:
                os.unlink(temporary, dir_fd=directory)
            except FileNotFoundError:
                pass
            os.close(directory)
