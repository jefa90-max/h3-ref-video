"""Windows task recovery must query a process without terminating it."""
import ctypes
from ctypes import wintypes
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from workbench import jobs


class WindowsProcesses(unittest.TestCase):
    def setUp(self):
        self.handle = 0x123456789
        self.kernel = SimpleNamespace(OpenProcess=Mock(return_value=self.handle),
                                      GetExitCodeProcess=Mock(side_effect=self.running), CloseHandle=Mock(return_value=1))
        self.signal = Mock(side_effect=AssertionError("Windows os.kill(pid, 0) would terminate the process"))
        self.patches = [patch.object(jobs, "os", SimpleNamespace(name="nt", kill=self.signal)),
                        patch.object(ctypes, "WinDLL", return_value=self.kernel, create=True),
                        patch.object(ctypes, "get_last_error", return_value=87, create=True),
                        patch.object(ctypes, "WinError", side_effect=lambda code: OSError(code, f"Windows error {code}"), create=True)]
        for mocked in self.patches:
            mocked.start()
            self.addCleanup(mocked.stop)

    @staticmethod
    def running(handle, exit_code):
        exit_code._obj.value = 259
        return 1

    def test_live_process_is_checked_without_sending_a_signal(self):
        self.assertTrue(jobs.process_alive(45394))
        self.signal.assert_not_called()
        self.kernel.OpenProcess.assert_called_once_with(0x1000, False, 45394)
        self.assertIs(self.kernel.OpenProcess.restype, wintypes.HANDLE)
        self.assertEqual(self.kernel.GetExitCodeProcess.argtypes[0], wintypes.HANDLE)
        self.assertEqual(self.kernel.CloseHandle.argtypes, [wintypes.HANDLE])
        self.kernel.CloseHandle.assert_called_once_with(self.handle)

    def test_exited_process_releases_its_handle_and_returns_false(self):
        def exited(handle, exit_code):
            exit_code._obj.value = 0
            return 1

        self.kernel.GetExitCodeProcess.side_effect = exited
        self.assertFalse(jobs.process_alive(45394))
        self.kernel.CloseHandle.assert_called_once_with(self.handle)

    def test_missing_process_returns_false_without_closing_an_invalid_handle(self):
        self.kernel.OpenProcess.return_value = None
        self.assertFalse(jobs.process_alive(45394))
        self.kernel.GetExitCodeProcess.assert_not_called()
        self.kernel.CloseHandle.assert_not_called()

    def test_access_denied_is_not_mistaken_for_a_stopped_process(self):
        self.kernel.OpenProcess.return_value = None
        with patch.object(ctypes, "get_last_error", return_value=5):
            self.assertTrue(jobs.process_alive(45394))
        self.kernel.CloseHandle.assert_not_called()

    def test_unexpected_open_error_is_reported(self):
        self.kernel.OpenProcess.return_value = None
        with patch.object(ctypes, "get_last_error", return_value=8):
            with self.assertRaisesRegex(OSError, "Windows error 8"):
                jobs.process_alive(45394)

    def test_failed_exit_query_reports_error_and_closes_handle(self):
        self.kernel.GetExitCodeProcess.side_effect = None
        self.kernel.GetExitCodeProcess.return_value = 0
        with patch.object(ctypes, "get_last_error", return_value=6):
            with self.assertRaisesRegex(OSError, "Windows error 6"):
                jobs.process_alive(45394)
        self.kernel.CloseHandle.assert_called_once_with(self.handle)

    def test_empty_pid_never_opens_or_signals_a_process(self):
        self.assertFalse(jobs.process_alive(None))
        self.assertFalse(jobs.process_alive(0))
        self.kernel.OpenProcess.assert_not_called()
        self.signal.assert_not_called()


if __name__ == "__main__":
    unittest.main()
