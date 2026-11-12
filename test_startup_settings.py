"""Offline tests for current-user startup settings; never modify the real registry."""

from contextlib import nullcontext, redirect_stderr, redirect_stdout
import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from startup_settings import RUN_KEY, StartupManager, StartupSettingsError, main


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

    def test_read_is_side_effect_free_before_setup_initializes_default(self):
        self.assertEqual(self.manager.read(), {
            "supported": True, "enabled": False, "mode": "current-user-logon",
            "manual_start_command": self.manager.manual_start_command(),
        })
        self.assertFalse(self.registry.exists)
        self.assertFalse(self.registry.values)

    def test_first_setup_or_upgrade_enables_startup_once(self):
        self.assertTrue(self.manager.initialize_default()["enabled"])
        marker = self.root / ".runtime" / "startup-default.json"
        self.assertEqual(json.loads(marker.read_text("utf-8")), {"version": 1})
        self.assertTrue(StartupManager(self.root).initialize_default()["enabled"])
        self.assertFalse(list(marker.parent.glob("startup-settings-*")))

    def test_disabled_choice_survives_future_setup_and_new_manager_instances(self):
        self.manager.initialize_default()
        self.manager.write(False)
        self.assertFalse(StartupManager(self.root).initialize_default()["enabled"])
        self.assertNotIn(self.manager.value_name, self.registry.values)

    def test_explicit_disabled_choice_before_initialization_is_respected(self):
        self.manager.write(False)
        self.assertFalse(self.manager.initialize_default()["enabled"])
        self.assertFalse(self.registry.values)

    def test_preexisting_enabled_entry_is_preserved_and_marked_initialized(self):
        self.registry.exists = True
        self.registry.values[self.manager.value_name] = (self.manager.command(), self.registry.REG_SZ)
        self.assertTrue(self.manager.initialize_default()["enabled"])
        self.assertTrue((self.root / ".runtime" / "startup-default.json").is_file())

    def test_invalid_initialization_record_does_not_silently_change_startup(self):
        marker = self.root / ".runtime" / "startup-default.json"
        marker.parent.mkdir()
        for value in ("not json", "[]", '{"version":true}', '{"version":0}'):
            marker.write_text(value, encoding="utf-8")
            with self.assertRaises(StartupSettingsError):
                self.manager.initialize_default()
            self.assertFalse(self.registry.values)

    def test_initialization_and_preference_storage_failures_are_explicit(self):
        self.registry.failure = PermissionError("denied")
        with self.assertRaisesRegex(StartupSettingsError, "denied"):
            self.manager.initialize_default()
        self.assertFalse((self.root / ".runtime" / "startup-default.json").exists())
        self.registry.failure = None
        with patch("startup_settings.tempfile.TemporaryDirectory", side_effect=PermissionError("record denied")):
            with self.assertRaisesRegex(StartupSettingsError, "record denied"):
                self.manager.write(False)

    def test_manual_start_command_uses_the_actual_directory_and_quotes_powershell_literals(self):
        command = self.manager.read()["manual_start_command"]
        self.assertIn("powershell.exe -NoProfile -ExecutionPolicy Bypass -File", command)
        self.assertIn(f"'{self.root / 'start.ps1'}'", command)
        special = StartupManager(self.root / "user's $app")
        self.assertIn("user''s $app", special.manual_start_command())

    def test_initializer_cli_preserves_disabled_choice_and_prints_manual_command(self):
        state = self.manager.write(False)
        output = io.StringIO()
        with (
            patch("startup_settings.sys.argv", ["startup_settings.py", "--initialize-default"]),
            patch("startup_settings.StartupManager") as factory,
            redirect_stdout(output),
        ):
            factory.return_value.initialize_default.return_value = state
            self.assertEqual(main(), 0)
        self.assertIn("preserving your choice", output.getvalue())
        self.assertIn(state["manual_start_command"], output.getvalue())
        factory.return_value.initialize_default.assert_called_once_with()

    def test_initializer_cli_reports_errors_instead_of_success(self):
        output = io.StringIO()
        with (
            patch("startup_settings.sys.argv", ["startup_settings.py", "--initialize-default"]),
            patch("startup_settings.StartupManager") as factory,
            redirect_stderr(output),
        ):
            factory.return_value.initialize_default.side_effect = StartupSettingsError("denied")
            self.assertEqual(main(), 1)
        self.assertIn("denied", output.getvalue())

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
            self.assertIsNone(self.manager.read()["manual_start_command"])
            self.assertFalse(self.manager.initialize_default()["supported"])
            self.assertFalse((self.root / ".runtime" / "startup-default.json").exists())
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
