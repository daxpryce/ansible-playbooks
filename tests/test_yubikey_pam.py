import ctypes
import os
from pathlib import Path
import runpy
import subprocess
import tempfile
import unittest
from unittest.mock import patch


HELPER = (
    Path(__file__).resolve().parents[1]
    / "roles/yubikey_pam/files/yubikey-pam-is-ssh"
)
IS_SSH_SESSION = runpy.run_path(str(HELPER))["is_ssh_session"]


class SshSessionTests(unittest.TestCase):
    def test_only_remote_sshd_bypasses_yubikey(self):
        for remote, service, expected in (
            ("yes", "sshd", True),
            ("no", "lightdm", False),
            ("no", "login", False),
            ("no", "sshd", False),
            ("yes", "login", False),
            ("yes", "other-remote-service", False),
        ):
            with self.subTest(remote=remote, service=service):
                result = subprocess.CompletedProcess(
                    [], 0, f"Service={service}\nRemote={remote}\n", ""
                )
                with (
                    patch("subprocess.run", return_value=result),
                    patch("syslog.syslog") as log,
                ):
                    self.assertEqual(IS_SSH_SESSION(), expected)
                    log.assert_not_called()

    def test_untrusted_environment_is_not_forwarded(self):
        result = subprocess.CompletedProcess(
            [], 0, "Remote=no\nService=lightdm\n", ""
        )
        with (
            patch.dict(
                os.environ,
                {
                    "SSH_CONNECTION": "spoofed",
                    "SSH_TTY": "/dev/pts/1",
                    "XDG_SESSION_ID": "spoofed",
                    "SYSTEMD_BUS_ADDRESS": "unix:path=/tmp/spoofed",
                    "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=/tmp/spoofed",
                },
            ),
            patch("subprocess.run", return_value=result) as run,
        ):
            self.assertFalse(IS_SSH_SESSION())
            args, kwargs = run.call_args
            self.assertEqual(
                args[0],
                [
                    "/usr/bin/loginctl",
                    "--no-pager",
                    "show-session",
                    "self",
                    "--property=Remote",
                    "--property=Service",
                ],
            )
            self.assertEqual(kwargs["env"], {"PATH": "/usr/bin:/usr/sbin", "LANG": "C"})
            self.assertEqual(kwargs["timeout"], 5)

    def test_failed_detection_is_logged_and_keeps_yubikey(self):
        for result in (
            subprocess.CompletedProcess([], 1, "", "No session"),
            subprocess.CompletedProcess([], 0, "", ""),
            subprocess.CompletedProcess([], 0, "Remote=yes\n", ""),
            subprocess.CompletedProcess([], 0, "Remote=maybe\nService=sshd\n", ""),
            subprocess.CompletedProcess([], 0, "Remote=yes\nService=\n", ""),
            subprocess.CompletedProcess([], 0, "Remote=yes\nService=sshd\njunk\n", ""),
            subprocess.CompletedProcess(
                [], 0, "Remote=no\nRemote=yes\nService=sshd\n", ""
            ),
        ):
            with self.subTest(result=result):
                with (
                    patch("subprocess.run", return_value=result),
                    patch("syslog.syslog") as log,
                ):
                    self.assertFalse(IS_SSH_SESSION())
                    log.assert_called_once()
                    self.assertIn("retaining YubiKey authentication", log.call_args.args[1])

    def test_unavailable_or_timed_out_logind_keeps_yubikey(self):
        for error in (FileNotFoundError("loginctl"), subprocess.TimeoutExpired([], 5)):
            with self.subTest(error=error):
                with (
                    patch("subprocess.run", side_effect=error),
                    patch("syslog.syslog") as log,
                ):
                    self.assertFalse(IS_SSH_SESSION())
                    log.assert_called_once()


class PamControlTests(unittest.TestCase):
    def test_guard_skips_only_fido_and_preserves_password_fallback(self):
        pam_tasks = HELPER.parents[1] / "tasks/pam.yml"
        guard = next(
            line.strip()
            for line in pam_tasks.read_text().splitlines()
            if "pam_exec.so" in line
        )

        class Conversation(ctypes.Structure):
            _fields_ = [("conv", ctypes.c_void_p), ("appdata_ptr", ctypes.c_void_p)]

        pam = ctypes.CDLL("libpam.so.0")
        pam.pam_start_confdir.argtypes = [
            ctypes.c_char_p,
            ctypes.c_char_p,
            ctypes.POINTER(Conversation),
            ctypes.c_char_p,
            ctypes.POINTER(ctypes.c_void_p),
        ]
        pam.pam_authenticate.argtypes = [ctypes.c_void_p, ctypes.c_int]
        pam.pam_end.argtypes = [ctypes.c_void_p, ctypes.c_int]

        for ssh, fido_accepted, password_accepted, expected in (
            (True, True, False, False),
            (True, False, True, True),
            (False, True, False, True),
            (False, False, True, True),
            (False, False, False, False),
        ):
            with self.subTest(
                ssh=ssh, fido=fido_accepted, password=password_accepted
            ), tempfile.TemporaryDirectory(prefix="yubikey-pam-control-") as temporary:
                root = Path(temporary)
                fido_marker = root / "fido"
                password_marker = root / "password"
                session_guard = guard.rsplit(" ", 1)[0] + (
                    " /usr/bin/true" if ssh else " /usr/bin/false"
                )
                # Substitute deterministic FIDO/password results, but execute
                # the actual guard control through libpam in an isolated stack.
                fido = (
                    f"pam_exec.so quiet quiet_log /usr/bin/touch {fido_marker}"
                    if fido_accepted
                    else "pam_deny.so"
                )
                password = "pam_permit.so" if password_accepted else "pam_deny.so"
                (root / "yubikey-test").write_text(
                    f"{session_guard}\n"
                    f"auth sufficient {fido}\n"
                    "auth required pam_exec.so quiet quiet_log "
                    f"/usr/bin/touch {password_marker}\n"
                    f"auth required {password}\n"
                )
                handle = ctypes.c_void_p()
                conversation = Conversation()
                status = pam.pam_start_confdir(
                    b"yubikey-test",
                    b"yubikey-test",
                    ctypes.byref(conversation),
                    os.fsencode(root),
                    ctypes.byref(handle),
                )
                self.assertEqual(status, 0)
                try:
                    status = pam.pam_authenticate(handle, 0)
                    self.assertEqual(status == 0, expected)
                    self.assertEqual(fido_marker.exists(), not ssh and fido_accepted)
                    self.assertEqual(password_marker.exists(), ssh or not fido_accepted)
                finally:
                    pam.pam_end(handle, status)


if __name__ == "__main__":
    unittest.main()
