from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from tools import start_standalone as launcher
from tools.start_plan import ProbeResult, StartupError


class StandaloneLauncherTests(unittest.TestCase):
    def setUp(self):
        self.directory = TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        root = Path(self.directory.name)
        executable = root / 'desktop-host/bin/Companion.Desktop.exe'
        executable.parent.mkdir(parents=True)
        executable.touch()
        from server import standalone
        asset_root = root / 'assets'
        for name in standalone.MEDIA:
            target = asset_root / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.touch()
        for target, attr, value in ((launcher, 'ROOT', root), (standalone, 'ASSET_ROOT', asset_root)):
            patcher = patch.object(target, attr, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def test_unknown_port_is_left_alone(self):
        with patch.object(launcher, 'probe_service', return_value=ProbeResult('unknown')), \
             patch.object(launcher.subprocess, 'Popen') as spawn:
            with self.assertRaises(StartupError):
                launcher.launch(simulation=True)
            spawn.assert_not_called()

    def test_mode_conflict_is_reported_without_start_or_stop(self):
        with patch.object(launcher, 'probe_service', return_value=ProbeResult('project', 'cloud')), \
             patch.object(launcher.subprocess, 'Popen') as spawn:
            with self.assertRaises(StartupError):
                launcher.launch(simulation=True)
            spawn.assert_not_called()

    def test_reused_simulation_launches_only_window_and_never_stops_service(self):
        with patch.object(launcher, 'probe_service', return_value=ProbeResult('project', 'simulation')), \
             patch.object(launcher, 'check_entry'), patch.object(launcher.subprocess, 'Popen') as spawn:
            self.assertEqual(launcher.launch(simulation=True), 0)
            spawn.assert_called_once()
            spawn.return_value.wait.assert_called_once()
            spawn.return_value.terminate.assert_not_called()

    def test_desktop_flag_reaches_host_and_retains_service_ownership(self):
        with patch.object(launcher, 'probe_service', return_value=ProbeResult('project', 'simulation')), \
             patch.object(launcher, 'check_entry'), patch.object(launcher.subprocess, 'Popen') as spawn:
            self.assertEqual(launcher.launch(simulation=True, desktop=True), 0)
            self.assertEqual(spawn.call_args.args[0][-1], '--desktop')
            self.assertNotIn('--acceptance', spawn.call_args.args[0])
            spawn.return_value.terminate.assert_not_called()

    def test_acceptance_refuses_existing_service(self):
        with patch.object(launcher, 'probe_service', return_value=ProbeResult('project', 'simulation')), \
             patch.object(launcher.subprocess, 'Popen') as spawn:
            with self.assertRaises(StartupError):
                launcher.launch(simulation=True, acceptance=True)
            spawn.assert_not_called()

    def test_failed_window_returns_failure_without_stopping_reused_service(self):
        with patch.object(launcher, 'probe_service', return_value=ProbeResult('project', 'simulation')), \
             patch.object(launcher, 'check_entry'), patch.object(launcher.subprocess, 'Popen') as spawn:
            spawn.return_value.wait.return_value = 1
            self.assertEqual(launcher.launch(simulation=True), 1)
            spawn.return_value.terminate.assert_not_called()

    def test_owned_simulation_stops_on_window_exit(self):
        from unittest.mock import MagicMock
        service, window = MagicMock(), MagicMock()
        service.poll.return_value = None
        with patch.object(launcher, 'probe_service', side_effect=[ProbeResult('free'), ProbeResult('project', 'simulation')]), \
             patch.object(launcher, 'check_entry'), patch.object(launcher.subprocess, 'Popen', side_effect=[service, window]), \
             patch.dict(launcher.os.environ, {'COMPANION_DATA_DIR': self.directory.name}):
            self.assertEqual(launcher.launch(simulation=True), 0)
            service.terminate.assert_called_once()
            service.wait.assert_called_once()
            window.terminate.assert_not_called()


if __name__ == '__main__':
    unittest.main()
