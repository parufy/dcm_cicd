import sys
import base64
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "scripts"))
try:
    import iperf_test
except ModuleNotFoundError as exc:
    if exc.name != "paramiko":
        raise
    # These unit tests mock the SSH boundary and do not require paramiko.
    with patch.dict(sys.modules, {"ssh_client": MagicMock()}):
        import iperf_test


class ServerTests(unittest.TestCase):
    @staticmethod
    def decode_command(cmd):
        return base64.b64decode(cmd.split()[-1]).decode("utf-16le")

    def test_windows_lifecycle(self):
        run = MagicMock(return_value=(0, "", ""))
        with iperf_test._windows_iperf_server(run, "C:\\test tools\\it's iperf3.exe", 5301, 0):
            self.assertEqual(run.call_count, 2)
        scripts = [self.decode_command(c.args[0]) for c in run.call_args_list]
        self.assertIn("'C:\\test tools\\it''s iperf3.exe'", scripts[0])
        self.assertIn("-WindowStyle Hidden", scripts[0])
        self.assertIn("'-s -p 5301 -J'", scripts[0])
        self.assertIn("OwningProcess", scripts[1])
        self.assertIn("StartTime", scripts[2])
        self.assertIn("Stop-Process", scripts[2])
        self.assertNotIn(" -D", scripts[0])
        if os.name == "nt":
            # Parse the actual generated scripts with Windows PowerShell, without executing them.
            for script in scripts:
                quoted = "'" + script.replace("'", "''") + "'"
                parse = ("$parseErrors = $null; $parseTokens = $null; "
                         f"[System.Management.Automation.Language.Parser]::ParseInput({quoted}, "
                         "[ref]$parseTokens, [ref]$parseErrors) | Out-Null; "
                         "if ($parseErrors.Count) { $parseErrors | Out-String | Write-Output; exit 1 }")
                result = subprocess.run(iperf_test._powershell_command(parse).split(),
                                        capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_windows_failures_cleanup(self):
        for failure in ("start", "check", "client"):
            with self.subTest(failure=failure):
                replies = [(1, "", "failed")] if failure == "start" else [
                    (0, "", ""), (1 if failure == "check" else 0, "", "failed")]
                run = MagicMock(side_effect=replies + [(0, "", "")])
                with self.assertRaises(RuntimeError):
                    with iperf_test._windows_iperf_server(run, "iperf3.exe", 5201, 0):
                        if failure != "client":
                            self.fail("client must not run")
                        raise RuntimeError("client failed")
                self.assertIn("Stop-Process", self.decode_command(run.call_args.args[0]))

    def test_lifecycle_and_quoted_path(self):
        run = MagicMock(return_value=(0, "", ""))
        with iperf_test._ssh_iperf_server(run, "/opt/test tools/iperf3", 5301, 0):
            self.assertEqual(run.call_count, 2)
        commands = [c.args[0] for c in run.call_args_list]
        self.assertIn("'/opt/test tools/iperf3' -s -p 5301", commands[0])
        self.assertIn("kill -0", commands[1])
        self.assertIn("rm -f", commands[2])
        self.assertNotIn("lsof", commands[2])

    def test_failed_start_and_failed_client_cleanup(self):
        for fail_start in (True, False):
            with self.subTest(fail_start=fail_start):
                run = MagicMock(side_effect=(
                    [(1, "", "busy"), (0, "", "")] if fail_start else
                    [(0, "", ""), (0, "", ""), (0, "", "")]
                ))
                with self.assertRaises(RuntimeError):
                    with iperf_test._ssh_iperf_server(run, "iperf3", 5201, 0):
                        raise RuntimeError("client failed")
                self.assertIn("rm -f", run.call_args.args[0])

    def test_dead_daemon_prevents_client(self):
        run = MagicMock(side_effect=[(0, "", ""), (1, "", ""),
                                     (0, "port busy", ""), (0, "", "")])
        with self.assertRaisesRegex(RuntimeError, "port busy"):
            with iperf_test._ssh_iperf_server(run, "iperf3", 5201, 0):
                self.fail("client must not run")

    def test_windows_ssh_server_and_adb_client_routing(self):
        self.test_ssh_server_and_adb_client_routing(server_os="windows")

    def test_ssh_server_and_adb_client_routing(self, server_os="linux"):
        events = []
        client, server = MagicMock(), MagicMock()
        client.__enter__.return_value = client
        server.__enter__.return_value = server
        def client_run(cmd, timeout):
            events.append(("client", cmd))
            return (0, "List of devices attached\nUE1\tdevice\n", "") if "devices" in cmd else (0, "{}", "")
        def server_run(cmd, timeout):
            if server_os == "windows":
                cmd = self.decode_command(cmd)
            events.append(("server", cmd))
            return 0, "", ""
        client.run.side_effect = client_run
        server.run.side_effect = server_run
        with tempfile.TemporaryDirectory() as tmp, patch.object(
            iperf_test, "make_client", side_effect=[client, server]
        ) as connect:
            self.assertTrue(iperf_test.run_iperf(
                server="192.0.2.1", port=5301, protocol="udp", duration=1,
                parallel=1, bandwidth="1M", output_file=Path(tmp) / "result.json",
                direction="ul", ssh_host="ue-control", ssh_user="ue", ssh_password="",
                ssh_port=22, adb_serial="UE1", adb_path="adb",
                adb_iperf_path="/data/local/tmp/iperf3", server_adb_serial=None,
                server_adb_iperf_path="unused", server_ssh_host="uplane-control",
                server_ssh_user="test", server_ssh_password="", server_ssh_port=2222,
                server_startup_wait=0, server_auto_start=True,
                server_iperf_path="/opt/iperf3",
                server_os=server_os,
            ))
            self.assertEqual(connect.call_args.args, ("uplane-control", "test", "", 2222))
        self.assertFalse(any("adb" in cmd for who, cmd in events if who == "server"))
        start = next(i for i, (_, cmd) in enumerate(events) if "-s -p 5301" in cmd)
        send = next(i for i, (_, cmd) in enumerate(events) if " -c 192.0.2.1" in cmd)
        self.assertLess(start, send)
        self.assertIn("adb -s UE1 shell /data/local/tmp/iperf3", events[send][1])
        self.assertIn(" -p 5301", events[send][1])
        self.assertIn("Stop-Process" if server_os == "windows" else "rm -f", events[-1][1])


if __name__ == "__main__":
    unittest.main()
