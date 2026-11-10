"""Manage this app's Windows current-user logon startup entry."""

import hashlib
import os
from pathlib import Path
import subprocess
import sys

if sys.platform == "win32":
    import winreg
else:
    winreg = None


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


class StartupSettingsError(Exception):
    pass


class StartupManager:
    def __init__(self, root):
        self.root = Path(root).resolve()
        identity = hashlib.sha256(str(self.root).casefold().encode("utf-8")).hexdigest()[:16]
        self.value_name = f"AzureOpenAILab-{identity}"

    def command(self):
        system_root = os.environ.get("SystemRoot")
        if not system_root:
            raise StartupSettingsError("无法定位 Windows 目录，未修改自启动设置。")
        powershell = Path(system_root) / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
        return subprocess.list2cmdline([
            str(powershell), "-NoProfile", "-NonInteractive", "-WindowStyle", "Hidden",
            "-ExecutionPolicy", "Bypass", "-File", str(self.root / "start.ps1"),
        ])

    def read(self):
        if winreg is None:
            return {"supported": False, "enabled": False, "mode": "current-user-logon"}
        try:
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_READ) as key:
                value, value_type = winreg.QueryValueEx(key, self.value_name)
        except FileNotFoundError:
            enabled = False
        except OSError as exc:
            raise StartupSettingsError(f"无法读取 Windows 自启动设置：{exc}") from exc
        else:
            if value_type != winreg.REG_SZ or value != self.command():
                raise StartupSettingsError("同名自启动项的命令与当前应用不一致，请先检查 Windows 自启动设置。")
            enabled = True
        return {"supported": True, "enabled": enabled, "mode": "current-user-logon"}

    def write(self, enabled):
        if type(enabled) is not bool:
            raise StartupSettingsError("自启动 enabled 必须是布尔值。")
        if winreg is None:
            raise StartupSettingsError("此功能仅支持 Windows 当前用户登录后启动。")
        current = self.read()
        if current["enabled"] == enabled:
            return current
        try:
            if enabled:
                command = self.command()
                if len(command) > 260:
                    raise StartupSettingsError("应用路径过长，超出 Windows 自启动命令的 260 字符限制，请缩短应用路径。")
                for required in (self.root / "start.ps1", self.root / ".venv" / "Scripts" / "python.exe"):
                    if not required.is_file():
                        raise StartupSettingsError(f"自启动依赖不存在：{required}")
                with winreg.CreateKeyEx(
                    winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE,
                ) as key:
                    winreg.SetValueEx(key, self.value_name, 0, winreg.REG_SZ, command)
            else:
                with winreg.OpenKey(
                    winreg.HKEY_CURRENT_USER, RUN_KEY, 0, winreg.KEY_SET_VALUE,
                ) as key:
                    winreg.DeleteValue(key, self.value_name)
        except OSError as exc:
            raise StartupSettingsError(f"无法保存 Windows 自启动设置：{exc}") from exc
        result = self.read()
        if result["enabled"] != enabled:
            raise StartupSettingsError("Windows 自启动设置未按预期保存，请刷新状态后重试。")
        return result
