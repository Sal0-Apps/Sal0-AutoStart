import tempfile
import unittest
from unittest.mock import Mock,patch
import app

class IdleScheduleTests(unittest.TestCase):
    def test_monitor_sleeps_until_the_first_real_deadline(self):
        for initial,expected in [(90,30),(10,10)]:
            with self.subTest(initial=initial),tempfile.TemporaryDirectory() as folder:
                store=app.Store(folder);store.data['settings'].update(initial_delay=initial,interval=30)
                with patch('app.time.monotonic',return_value=100),patch('app.storage.log'):
                    monitor=app.Monitor(store,docker=Mock(),catalog=Mock())
                    monitor.discover=Mock();monitor.scan=Mock()
                    observed=[]
                    def wait(timeout=None):observed.append(timeout);monitor.stop.set()
                    monitor.wake.wait=wait
                    try:monitor.run()
                    finally:monitor.probes.close()
                self.assertEqual(observed,[expected]);monitor.discover.assert_called_once();monitor.scan.assert_not_called()

    def test_shutdown_during_wakeup_reset_exits_without_an_extra_scan(self):
        with tempfile.TemporaryDirectory() as folder,patch('app.storage.log'):
            monitor=app.Monitor(app.Store(folder),docker=Mock(),catalog=Mock())
            monitor.wake.clear=monitor.stop.set
            monitor.wake.wait=Mock();monitor.discover=Mock()
            try:monitor.run()
            finally:monitor.probes.close()
            monitor.wake.wait.assert_not_called();monitor.discover.assert_not_called()
