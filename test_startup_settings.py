"""Offline tests for current-user startup settings; never modify the real registry."""

from contextlib import nullcontext
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from startup_settings import RUN_KEY, StartupManager, StartupSettingsError


class FakeRegistry:
    HKEY_CURRENT_USER = "HKCU"
    KEY_READ = 1
    KEY_SET_VALUE = 2
    REG_SZ = 1

    def __init__(self):
        self.values = {}
        self.exists = False
        self.failure = None

    def OpenKey(self, hive, path, reserved, access):
        if self.failure:
            raise self.failure
        if not self.exists:
            raise FileNotFoundError(path)
        return nullcontext((hive, path))

    def CreateKeyEx(self, hive, path, reserved, access):
        if self.failure:
            raise self.failure
        self.exists = True
        return nullcontext((hive, path))

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name]

    def SetValueEx(self, key, name, reserved, value_type, value):
        self.values[name] = (value, value_type)

    def DeleteValue(self, key, name):
        del self.values[name]


class StartupSettingsTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory(prefix="startup test ")
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        (self.root / "start.ps1").write_text("", encoding="utf-8")
        python = self.root / ".venv" / "Scripts" / "python.exe"
        python.parent.mkdir(parents=True)
        python.touch()
        self.manager = StartupManager(self.root)
        self.registry = FakeRegistry()
        self.registry_patch = patch("startup_settings.winreg", self.registry)
        self.registry_patch.start()
        self.addCleanup(self.registry_patch.stop)
        self.environment_patch = patch.dict("os.environ", {"SystemRoot": r"C:\Windows"})
        self.environment_patch.start()
        self.addCleanup(self.environment_patch.stop)

    def test_read_does_not_register_startup_by_default(self):
        self.assertEqual(self.manager.read(), {
            "supported": True, "enabled": False, "mode": "current-user-logon",
        })
        self.assertFalse(self.registry.exists)
        self.assertFalse(self.registry.values)

    def test_enable_persists_and_disable_preserves_other_startup_entries(self):
        self.registry.exists = True
        self.registry.values["OtherApp"] = ("another-command", self.registry.REG_SZ)
        self.assertTrue(self.manager.write(True)["enabled"])
        command, value_type = self.registry.values[self.manager.value_name]
        self.assertIn('-WindowStyle Hidden', command)
        self.assertIn(f'"{self.root / "start.ps1"}"', command)
        self.assertEqual(value_type, self.registry.REG_SZ)
        self.assertLessEqual(len(command), 260)
        self.assertTrue(StartupManager(self.root).read()["enabled"])
        self.assertTrue(self.manager.write(True)["enabled"])
        self.assertFalse(self.manager.write(False)["enabled"])
        self.assertFalse(self.manager.write(False)["enabled"])
        self.assertEqual(self.registry.values, {
            "OtherApp": ("another-command", self.registry.REG_SZ),
        })
        self.assertEqual(RUN_KEY, r"Software\Microsoft\Windows\CurrentVersion\Run")

    def test_startup_identity_is_stable_and_scoped_to_app_directory(self):
        self.assertEqual(self.manager.value_name, StartupManager(self.root).value_name)
        self.assertNotEqual(self.manager.value_name, StartupManager(self.root / "other").value_name)

    def test_existing_conflicting_command_is_not_overwritten_or_deleted(self):
        self.registry.exists = True
        self.registry.values[self.manager.value_name] = ("unexpected-command", self.registry.REG_SZ)
        for operation in (self.manager.read, lambda: self.manager.write(True), lambda: self.manager.write(False)):
            with self.assertRaises(StartupSettingsError):
                operation()
        self.assertEqual(self.registry.values[self.manager.value_name][0], "unexpected-command")

    def test_access_denied_is_reported(self):
        self.registry.failure = PermissionError("denied")
        with self.assertRaisesRegex(StartupSettingsError, "denied"):
            self.manager.read()
        with self.assertRaisesRegex(StartupSettingsError, "denied"):
            self.manager.write(True)

    def test_missing_dependency_prevents_registration(self):
        (self.root / "start.ps1").unlink()
        with self.assertRaises(StartupSettingsError):
            self.manager.write(True)
        self.assertFalse(self.registry.values)

    def test_long_command_prevents_registration(self):
        with patch.object(self.manager, "command", return_value="x" * 261):
            with self.assertRaises(StartupSettingsError):
                self.manager.write(True)
        self.assertFalse(self.registry.values)

    def test_only_boolean_values_are_accepted(self):
        for value in (None, 0, 1, "true", [], {}):
            with self.assertRaises(StartupSettingsError):
                self.manager.write(value)
        self.assertFalse(self.registry.values)

    def test_unsupported_platform_is_explicit(self):
        with patch("startup_settings.winreg", None):
            self.assertFalse(self.manager.read()["supported"])
            with self.assertRaises(StartupSettingsError):
                self.manager.write(True)

    def test_write_failure_and_readback_mismatch_are_reported(self):
        with patch.object(self.registry, "CreateKeyEx", side_effect=PermissionError("write denied")):
            with self.assertRaisesRegex(StartupSettingsError, "write denied"):
                self.manager.write(True)
        with patch.object(self.registry, "SetValueEx"):
            with self.assertRaises(StartupSettingsError):
                self.manager.write(True)


if __name__ == "__main__":
    unittest.main()
