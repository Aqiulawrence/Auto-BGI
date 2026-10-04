"""安装登录任务，或在主桌面中启动常用程序。"""

import base64
import ctypes
import os
import subprocess
import sys
from pathlib import Path
from ctypes import wintypes

TASK_NAME = "Auto-BGI Startup"
STARTUP_PROGRAMS = (
    (Path(r"C:\Program Files (x86)\Steam\steam.exe"), ("-silent",)),
    (Path(r"C:\Program Files\Clash Verge\clash-verge.exe"), ()),
)
TODO_FILE = Path(r"C:\Users\Admin\Desktop\待办.txt")


def ps_string(value: str) -> str:
    """把值编码为 PowerShell 单引号字符串。"""
    return "'" + value.replace("'", "''") + "'"

def install_task() -> None:
    script = Path(__file__).resolve()
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    if not pythonw.is_file():
        raise FileNotFoundError(f"找不到 pythonw.exe：{pythonw}")

    command = f"""
$ErrorActionPreference = 'Stop'
$account = [Security.Principal.WindowsIdentity]::GetCurrent().Name
$action = New-ScheduledTaskAction -Execute {ps_string(str(pythonw))} -Argument {ps_string(subprocess.list2cmdline([str(script), '--run']))} -WorkingDirectory {ps_string(str(script.parent))}
$trigger = New-ScheduledTaskTrigger -AtLogOn -User $account
$principal = New-ScheduledTaskPrincipal -UserId $account -LogonType Interactive -RunLevel Limited
$settings = New-ScheduledTaskSettingsSet -MultipleInstances IgnoreNew -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries
Register-ScheduledTask -TaskName {ps_string(TASK_NAME)} -Action $action -Trigger $trigger -Principal $principal -Settings $settings -Force | Out-Null
"""
    result = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-EncodedCommand",
         base64.b64encode(command.encode("utf-16le")).decode("ascii")],
        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW,
    )
    if result.returncode:
        raise RuntimeError((result.stderr or result.stdout).strip()
                           or f"计划任务创建失败，退出代码 {result.returncode}")


def is_main_desktop() -> bool:
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
    kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    kernel32.ProcessIdToSessionId.restype = wintypes.BOOL
    wts.WTSQuerySessionInformationW.argtypes = [
        wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
        ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
    ]
    wts.WTSQuerySessionInformationW.restype = wintypes.BOOL
    wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]
    wts.WTSFreeMemory.restype = None

    session = wintypes.DWORD()
    buffer = ctypes.c_void_p()
    size = wintypes.DWORD()
    if not kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
        return False
    # WTSClientProtocolType = 16；主桌面为 0，RDP 分身为 2。
    if not wts.WTSQuerySessionInformationW(
        None, session.value, 16, ctypes.byref(buffer), ctypes.byref(size)
    ):
        return False
    try:
        return (bool(buffer) and size.value >= ctypes.sizeof(ctypes.c_ushort)
                and ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ushort))[0] == 0)
    finally:
        wts.WTSFreeMemory(buffer)


def run_startup() -> None:
    if not is_main_desktop():
        return
    for executable, arguments in STARTUP_PROGRAMS:
        subprocess.Popen([str(executable), *arguments], cwd=str(executable.parent))
    os.startfile(str(TODO_FILE))


if __name__ == "__main__":
    if sys.argv[1:] == ["--run"]:
        run_startup()
    elif not sys.argv[1:]:
        try:
            install_task()
        except Exception as exc:
            ctypes.windll.user32.MessageBoxW(None, str(exc), "创建开机自启任务失败", 0x10)
            raise
        ctypes.windll.user32.MessageBoxW(None, f"已创建计划任务：{TASK_NAME}", "开机自启已设置", 0x40)
    else:
        ctypes.windll.user32.MessageBoxW(None, "不支持的参数", "Auto-BGI Startup", 0x10)
