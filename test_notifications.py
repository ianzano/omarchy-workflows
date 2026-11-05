import unittest
from unittest.mock import Mock

import notifications


class NotificationTests(unittest.TestCase):
    def test_dbus_notification_body_is_escaped_and_error_is_urgent(self):
        for status, urgency in [('completed', 1), ('failed', 2), ('timeout', 2), ('cancelled', 1)]:
            with self.subTest(status=status):
                connection = Mock()
                notifications.send_result('Stage <img src="https://invalid"> & build', status, connection)
                connection.call_sync.assert_called_once()
                call = connection.call_sync.call_args
                self.assertEqual(call.args[:4], ('org.freedesktop.Notifications', '/org/freedesktop/Notifications', 'org.freedesktop.Notifications', 'Notify'))
                payload = call.args[4].unpack()
                self.assertEqual(payload[0], 'Workflows')
                self.assertEqual(payload[3], notifications.TITLES[status])
                self.assertEqual(payload[4], 'Stage &lt;img src="https://invalid"&gt; &amp; build')
                self.assertEqual(payload[6]['urgency'], urgency)
                self.assertEqual(call.args[-2], 3000)

    def test_invalid_notifications_never_reach_dbus(self):
        for name, status in [('x' * 161, 'completed'), ('bad\x00name', 'failed'), ('stage', 'unknown')]:
            connection = Mock()
            with self.assertRaises(ValueError):
                notifications.send_result(name, status, connection)
            connection.call_sync.assert_not_called()


if __name__ == '__main__':
    unittest.main()
