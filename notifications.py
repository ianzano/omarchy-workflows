"""Deliver one run result over the session D-Bus without exposing output or commands."""
import html
import json
import os
from pathlib import Path
import re

TITLES = {
    'completed': 'Workflow completed',
    'failed': 'Workflow failed',
    'timeout': 'Workflow timed out',
    'cancelled': 'Workflow cancelled',
}


def notification_title(status):
    language_file = Path(os.environ.get('OMARCHY_LANGUAGE_FILE',
        str(Path(os.environ.get('XDG_CONFIG_HOME', Path.home() / '.config')) / 'omarchy/language')))
    try:
        with language_file.open() as stream:
            language = stream.read(64).strip()
    except OSError:
        language = 'en'
    if not re.fullmatch(r'[a-z]{2,3}(?:-[A-Za-z0-9]{2,8})*', language):
        language = 'en'
    title = TITLES[status]
    for code in dict.fromkeys(['en', language.split('-')[0], language]):
        try:
            with (Path(__file__).parent / 'i18n' / (code + '.json')).open() as stream:
                raw = stream.read(65537)
            if len(raw) > 65536:
                continue
            value = json.loads(raw).get('notification.' + status)
            if isinstance(value, str) and 0 < len(value) <= 200 and '\x00' not in value:
                title = value
        except (OSError, ValueError, AttributeError):
            pass
    return title


def notification_fields(name, status):
    if not isinstance(name, str) or not 1 <= len(name) <= 160 or '\x00' in name:
        raise ValueError('Invalid notification name')
    if status not in TITLES:
        raise ValueError('Invalid notification status')
    # Notification bodies use markup, even though the panel uses plain text.
    return notification_title(status), html.escape(name, quote=False), 2 if status in ('failed', 'timeout') else 1


def send_result(name, status, connection=None):
    title, body, urgency = notification_fields(name, status)
    from gi.repository import Gio, GLib
    connection = connection or Gio.bus_get_sync(Gio.BusType.SESSION, None)
    connection.call_sync(
        'org.freedesktop.Notifications', '/org/freedesktop/Notifications',
        'org.freedesktop.Notifications', 'Notify',
        GLib.Variant('(susssasa{sv}i)', ('Workflows', 0, '', title, body, [],
                                      {'urgency': GLib.Variant('y', urgency)}, -1)),
        GLib.VariantType.new('(u)'), Gio.DBusCallFlags.NONE, 3000, None)
