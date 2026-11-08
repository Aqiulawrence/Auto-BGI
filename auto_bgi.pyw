"""连接 BetterGI 桌面分身，运行默认一条龙，完成后关闭分身。"""
from __future__ import annotations

import argparse
import atexit
import ctypes
import getpass
import os
import re
import subprocess
import sys
import time
import traceback
import uuid
import winreg
import xml.etree.ElementTree as ET
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Callable, TypeVar

DEFAULT_EXE = Path(r"D:\Tools\BetterGI\BetterGI.exe")
# Python 退出时可能已移除 __main__.__file__，退出回调需使用预先保存的路径。
LOG_PATH = Path(__file__).resolve().with_suffix(".log")
GAME_NAMES = {"yuanshen.exe", "genshinimpact.exe"}
LOG_PATTERN = "better-genshin-impact????????.log"
FINISHED_MESSAGE = "一条龙和配置组任务结束"
TH32CS_SNAPPROCESS = 0x00000002
PROCESS_TERMINATE = 0x00000001
SYNCHRONIZE = 0x00100000
WAIT_OBJECT_0 = 0
ERROR_NOT_FOUND = 1168
STARTUP_INTERVAL = 0.01
T = TypeVar("T")
WINDOW_CALLBACK = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
_restore_resolution_on_exit = True


def restore_game_resolution() -> None:
    """普通退出和关闭控制台时，将原神分辨率写回 2560 × 1440。"""
    if not _restore_resolution_on_exit:
        return
    try:
        with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, r"Software\miHoYo\原神",
                                0, winreg.KEY_SET_VALUE) as key:
            winreg.SetValueEx(key, "Screenmanager Resolution Height_h2627697771",
                             0, winreg.REG_DWORD, 1440)
            winreg.SetValueEx(key, "Screenmanager Resolution Width_h182942802",
                             0, winreg.REG_DWORD, 2560)
        message = "已恢复原神分辨率：2560 × 1440"
    except OSError as exc:
        message = f"恢复原神分辨率失败：{exc}"
    # atexit 执行时主日志已关闭，需要单独打开。
    try:
        with LOG_PATH.open("a", encoding="utf-8") as log_file:
            log_file.write(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {message}\n")
    except OSError:
        pass


def handle_console_exit(event: int) -> bool:
    if event == 2:  # CTRL_CLOSE_EVENT：点击控制台窗口右上角的叉。
        restore_game_resolution()
    return False


class ProcessEntry(ctypes.Structure):
    _fields_ = [
        ("size", wintypes.DWORD), ("usage", wintypes.DWORD),
        ("pid", wintypes.DWORD), ("heap", ctypes.c_void_p),
        ("module", wintypes.DWORD), ("threads", wintypes.DWORD),
        ("parent", wintypes.DWORD), ("priority", wintypes.LONG),
        ("flags", wintypes.DWORD), ("name", ctypes.c_wchar * 260),
    ]


def configure_winapi() -> None:
    k = ctypes.windll.kernel32
    k.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
    k.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
    k.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    k.Process32FirstW.restype = wintypes.BOOL
    k.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.POINTER(ProcessEntry)]
    k.Process32NextW.restype = wintypes.BOOL
    k.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    k.ProcessIdToSessionId.restype = wintypes.BOOL
    k.CloseHandle.argtypes = [wintypes.HANDLE]
    k.CloseHandle.restype = wintypes.BOOL
    k.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k.OpenProcess.restype = wintypes.HANDLE
    k.WaitForSingleObject.argtypes = [wintypes.HANDLE, wintypes.DWORD]
    k.WaitForSingleObject.restype = wintypes.DWORD
    k.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    k.TerminateProcess.restype = wintypes.BOOL
    ctypes.windll.wtsapi32.WTSGetChildSessionId.argtypes = [ctypes.POINTER(wintypes.DWORD)]
    ctypes.windll.wtsapi32.WTSGetChildSessionId.restype = wintypes.BOOL
    ctypes.windll.wtsapi32.WTSLogoffSession.argtypes = [wintypes.HANDLE,
        wintypes.DWORD, wintypes.BOOL]
    ctypes.windll.wtsapi32.WTSLogoffSession.restype = wintypes.BOOL
    ctypes.windll.user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
    ctypes.windll.user32.FindWindowW.restype = wintypes.HWND
    ctypes.windll.user32.GetWindowThreadProcessId.argtypes = [
        wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    ctypes.windll.user32.GetWindowThreadProcessId.restype = wintypes.DWORD
    ctypes.windll.user32.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
        wintypes.WPARAM, wintypes.LPARAM]
    ctypes.windll.user32.PostMessageW.restype = wintypes.BOOL
    ctypes.windll.user32.ShowWindowAsync.argtypes = [wintypes.HWND, ctypes.c_int]
    ctypes.windll.user32.ShowWindowAsync.restype = wintypes.BOOL
    ctypes.windll.user32.IsIconic.argtypes = [wintypes.HWND]
    ctypes.windll.user32.IsIconic.restype = wintypes.BOOL
    ctypes.windll.user32.EnumWindows.argtypes = [WINDOW_CALLBACK, wintypes.LPARAM]
    ctypes.windll.user32.EnumWindows.restype = wintypes.BOOL
    ctypes.windll.user32.IsWindowVisible.argtypes = [wintypes.HWND]
    ctypes.windll.user32.IsWindowVisible.restype = wintypes.BOOL


def session_of(pid: int) -> int | None:
    value = wintypes.DWORD()
    if ctypes.windll.kernel32.ProcessIdToSessionId(pid, ctypes.byref(value)):
        return value.value
    return None


def child_session() -> int | None:
    value = wintypes.DWORD()
    if ctypes.windll.wtsapi32.WTSGetChildSessionId(ctypes.byref(value)):
        return value.value if value.value != 0xFFFFFFFF else None
    error = ctypes.windll.kernel32.GetLastError()
    if error == ERROR_NOT_FOUND:
        return None
    raise ctypes.WinError(error)


def processes() -> list[tuple[int, int, str]]:
    k = ctypes.windll.kernel32
    handle = k.CreateToolhelp32Snapshot(TH32CS_SNAPPROCESS, 0)
    if handle == ctypes.c_void_p(-1).value:
        raise ctypes.WinError()
    result: list[tuple[int, int, str]] = []
    try:
        entry = ProcessEntry()
        entry.size = ctypes.sizeof(entry)
        ok = k.Process32FirstW(handle, ctypes.byref(entry))
        while ok:
            session = session_of(entry.pid)
            if session is not None:
                result.append((entry.pid, session, entry.name.lower()))
            ok = k.Process32NextW(handle, ctypes.byref(entry))
        return result
    finally:
        k.CloseHandle(handle)


def named_process_running(session: int, names: set[str]) -> bool:
    return any(sid == session and name in names for _, sid, name in processes())


def named_process_pid(session: int, name: str, exclude_pid: int = 0) -> int | None:
    return next((pid for pid, sid, process_name in processes()
                 if sid == session and process_name == name and pid != exclude_pid), None)


def process_running(pid: int, name: str) -> bool:
    return any(current_pid == pid and current_name == name
               for current_pid, _, current_name in processes())


def window_owner(hwnd: int) -> int:
    owner = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    return owner.value


def find_window(name: str, current_session: int,
                pid: int | None = None) -> tuple[int, int] | None:
    # Native title lookup avoids UI Automation work while a window is loading.
    hwnd = ctypes.windll.user32.FindWindowW(None, name)
    if not hwnd:
        return None
    owner = window_owner(hwnd)
    if (pid is not None and owner != pid) or session_of(owner) != current_session:
        return None
    return int(hwnd), owner


def replace_idle_child_bettergi(child_id: int, old_pid: int) -> None:
    """Make the next CLI invocation the child instance's initial command."""
    if session_of(old_pid) != child_id or not process_running(old_pid, "bettergi.exe"):
        raise RuntimeError("待替换的 BetterGI 已退出或不属于目标分身")
    if named_process_running(child_id, GAME_NAMES):
        raise RuntimeError("分身里已有游戏进程，不能替换正在使用的 BetterGI")
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(PROCESS_TERMINATE | SYNCHRONIZE, False, old_pid)
    if not handle:
        raise ctypes.WinError()
    try:
        if not k.TerminateProcess(handle, 0):
            raise ctypes.WinError()
        wait_for(lambda: k.WaitForSingleObject(handle, 0) == WAIT_OBJECT_0,
                 5, "原分身 BetterGI 退出")
    finally:
        k.CloseHandle(handle)


class FastUIA:
    """Use UI Automation's native FindFirst instead of Python tree walks."""

    def __init__(self):
        import comtypes.client
        from comtypes import COMError
        uia = comtypes.client.GetModule("UIAutomationCore.dll")

        self.COMError = COMError
        self.uia = uia
        self.client = comtypes.client.CreateObject(
            uia.CUIAutomation, interface=uia.IUIAutomation)
        self.button_type = self.client.CreatePropertyCondition(
            uia.UIA_ControlTypePropertyId, uia.UIA_ButtonControlTypeId)
        self.conditions = {}
        for name in ("打开", "启动", "启动并连接"):
            name_condition = self.client.CreatePropertyCondition(
                uia.UIA_NamePropertyId, name)
            self.conditions[name] = self.client.CreateAndCondition(
                self.button_type, name_condition)

    def _button(self, hwnd: int, names: tuple[str, ...]):
        root = self.client.ElementFromHandle(hwnd)
        for name in names:
            try:
                element = root.FindFirst(self.uia.TreeScope_Subtree,
                                         self.conditions[name])
                if element and element.CurrentIsEnabled:
                    raw_pattern = element.GetCurrentPattern(self.uia.UIA_InvokePatternId)
                    if raw_pattern:
                        return raw_pattern.QueryInterface(
                            self.uia.IUIAutomationInvokePattern), name
            except self.COMError:
                continue
        return None

    def invoke(self, hwnd: int, names: tuple[str, ...], seconds: int) -> str:
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                match = self._button(hwnd, names)
            except self.COMError:  # WPF can replace its UIA tree while loading.
                match = None
            if match:
                pattern, name = match
                pattern.Invoke()
                return name
            time.sleep(STARTUP_INTERVAL)
        raise TimeoutError(f"等待按钮 {names} 超时（{seconds} 秒）")


def child_connected_to_main(log_dir: Path, main_pid: int,
                            child_id: int, child_pid: int) -> bool:
    marker = re.compile(
        rf"\[Primary:S\d+:P{main_pid}:T\d+\] .*InstanceService\r?\n"
        rf"桌面分身 BetterGI 已连接根实例：进程 {child_pid}，Session {child_id}(?!\d)")
    for path in log_dir.glob(LOG_PATTERN):
        if marker.search(path.read_text(encoding="utf-8-sig", errors="replace")):
            return True
    return False


@dataclass
class LogCursor:
    """每个文件独立保留读取位置、半行和多行日志的来源。"""

    offset: int = 0
    pending: bytes = b""
    from_child: bool = False
    from_dragon: bool = False


class CompletionLog:
    """Read only log lines appended after the One Dragon command is sent."""

    def __init__(self, folder: Path, child_id: int):
        self.folder = folder
        self.marker = re.compile(rf"\[ChildSession:S{child_id}:P\d+:T\d+\]")
        self.cursors = {path: LogCursor(offset=path.stat().st_size)
                        for path in self._files()}
        self.started = False
        self.finished = False

    def _files(self) -> list[Path]:
        return sorted(self.folder.glob(LOG_PATTERN))

    def _read_lines(self, path: Path, cursor: LogCursor) -> list[str]:
        with path.open("rb") as stream:
            stream.seek(cursor.offset)
            chunk = stream.read()
            cursor.offset = stream.tell()
        lines = (cursor.pending + chunk).split(b"\n")
        cursor.pending = lines.pop()
        return [line.decode("utf-8-sig", errors="replace").strip() for line in lines]

    def _consume(self, line: str, cursor: LogCursor) -> None:
        if re.match(r"^\[\d\d:\d\d:\d\d\.\d+\]", line):
            cursor.from_child = bool(self.marker.search(line))
            cursor.from_dragon = False
        if not cursor.from_child:
            return
        if "OneDragonFlowViewModel" in line:
            cursor.from_dragon = True
        if cursor.from_dragon:
            self.started |= "启用一条龙配置：" in line
            self.finished |= FINISHED_MESSAGE in line

    def completed(self) -> bool:
        if self.finished:
            return True
        for path in self._files():
            size = path.stat().st_size
            cursor = self.cursors.setdefault(path, LogCursor())
            if size < cursor.offset:  # Truncation also invalidates the old source.
                cursor = self.cursors[path] = LogCursor()
            if size == cursor.offset:
                continue
            for line in self._read_lines(path, cursor):
                self._consume(line, cursor)
                if self.finished:
                    return True
        return False


def delete_task(folder, name: str) -> None:
    """尽力删除临时任务，清理失败不能覆盖启动阶段的异常。"""
    try:
        folder.DeleteTask(name, 0)
    except Exception as exc:
        print(f"清理临时计划任务失败：{exc}", file=sys.stderr, flush=True)


@contextmanager
def send_one_dragon(exe: Path, child_id: int, old_pid: int):
    """启动分身任务，离开 with 块时删除临时任务。"""
    import comtypes.client
    from comtypes import COMError
    from comtypes.automation import VARIANT

    if child_session() != child_id:
        raise RuntimeError("目标桌面分身会话已变化，未发送一条龙命令")
    # Same Task Scheduler RunEx(session ID) mechanism used by BetterGI itself.
    scheduler = comtypes.client.CreateObject("Schedule.Service")
    from comtypes.gen import TaskScheduler
    scheduler.Connect()
    folder = scheduler.GetFolder("\\")
    definition = scheduler.NewTask(0)
    definition.RegistrationInfo.Author = "BetterGI Python child launcher"
    definition.Settings.Enabled = True
    definition.Settings.Hidden = True
    definition.Settings.AllowDemandStart = True
    definition.Settings.ExecutionTimeLimit = "PT0S"
    account = f"{os.environ.get('USERDOMAIN', '.')}\\{getpass.getuser()}"
    definition.Principal.UserId = account
    definition.Principal.LogonType = 3  # interactive token
    definition.Principal.RunLevel = 1  # highest privileges
    action = definition.Actions.Create(0).QueryInterface(TaskScheduler.IExecAction)
    action.Path = str(exe)
    action.WorkingDirectory = str(exe.parent)
    args = ["--instance", "childSession", "--restart-from-pid", str(old_pid),
            "startOneDragon"]
    action.Arguments = subprocess.list2cmdline(args)
    command = ET.fromstring(definition.XmlText).find(".//{*}Exec/{*}Command")
    if command is None or command.text != str(exe):
        raise RuntimeError("计划任务的执行动作缺少 BetterGI.exe 路径")
    task_name = f"BetterGI-Child-OneDragon-{uuid.uuid4().hex}"
    task = folder.RegisterTaskDefinition(task_name, definition, 2, account, None, 3, None)
    try:
        # Use an empty VARIANT for "no parameters" and a null BSTR for "no
        # user", matching BetterGI's own RunEx(null, 4, session, null).
        for attempt in range(100):
            try:
                running_task = task.RunEx(VARIANT(), 4, child_id, None)
                if running_task is None:
                    raise RuntimeError("任务计划程序没有返回运行实例")
                break
            except COMError as exc:
                if exc.hresult != -2147024809 or attempt == 99:
                    raise RuntimeError(
                        f"无法在分身 Session {child_id} 启动一条龙任务：{exc}"
                    ) from exc
                time.sleep(STARTUP_INTERVAL)  # Login token can lag behind the child process.
        yield task
    finally:
        delete_task(folder, task_name)


def wait_for(check: Callable[[], T], seconds: float, description: str,
             interval: float = STARTUP_INTERVAL) -> T:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(f"等待{description}超时（{seconds} 秒）")


@dataclass
class StableCondition:
    """只有条件持续成立达到指定时长才返回 True。"""

    seconds: float
    since: float | None = None

    def reached(self, condition: bool, now: float) -> bool:
        if not condition:
            self.since = None
            return False
        if self.since is None:
            self.since = now
        return now - self.since >= self.seconds


def monitor_completion(child_id: int, child_pid: int, log: CompletionLog) -> None:
    print("正在等待分身内一条龙完成…", flush=True)
    finished = False
    game_absent = StableCondition(3)
    session_absent = StableCondition(5)
    bettergi_absent = StableCondition(5)
    while True:
        if not finished and log.completed():
            finished = True
            print("已检测到分身的一条龙结束日志；等待游戏关闭…", flush=True)
        current_child_id = child_session()
        now = time.monotonic()
        if session_absent.reached(current_child_id != child_id, now):
            raise RuntimeError(
                f"桌面分身会话在清理前消失或改变（原 Session {child_id}，"
                f"当前 {current_child_id}）")
        if finished:
            if (game_absent.reached(not named_process_running(child_id, GAME_NAMES), now)
                    and current_child_id == child_id):
                print("分身中的游戏已关闭。", flush=True)
                return
        elif bettergi_absent.reached(not process_running(child_pid, "bettergi.exe"), now):
            raise RuntimeError("分身 BetterGI 已退出，但未检测到一条龙结束日志")
        time.sleep(1)


def close_after_completion(main_hwnd: int, main_pid: int, child_id: int) -> None:
    current_child_id = child_session()
    if current_child_id not in (None, child_id):
        raise RuntimeError(
            f"桌面分身会话已变化（原 Session {child_id}，当前 {current_child_id}），"
            "为避免注销其他会话，已停止清理")
    print("注销桌面分身…", flush=True)
    if current_child_id == child_id:
        if not ctypes.windll.wtsapi32.WTSLogoffSession(None, child_id, True):
            raise RuntimeError(f"注销分身失败：{ctypes.WinError()}")
        wait_for(lambda: child_session() is None, 15,
                 "桌面分身会话注销完成", interval=0.25)
    print("关闭 BetterGI 主进程…", flush=True)
    if not process_running(main_pid, "bettergi.exe"):
        return
    if window_owner(main_hwnd) != main_pid:
        raise RuntimeError("BetterGI 主窗口句柄已变化，未向其他窗口发送关闭消息")
    if not ctypes.windll.user32.PostMessageW(main_hwnd, 0x0010, 0, 0):  # WM_CLOSE
        raise RuntimeError(f"发送关闭消息失败：{ctypes.WinError()}")
    wait_for(lambda: not process_running(main_pid, "bettergi.exe"),
             20, "BetterGI 主进程退出", interval=0.25)


@dataclass(frozen=True)
class DesktopContext:
    main_hwnd: int
    main_pid: int
    child_id: int
    old_child_pid: int


def minimize_windows(main_pid: int, *handles: int) -> None:
    """两个窗口首次最小化后，再检测 1 秒以处理重新弹出的窗口。"""
    user32 = ctypes.windll.user32
    errors: list[Exception] = []

    @WINDOW_CALLBACK
    def minimize(hwnd, _):
        try:
            if (window_owner(hwnd) == main_pid and user32.IsWindowVisible(hwnd)
                    and not user32.IsIconic(hwnd)):
                user32.ShowWindowAsync(hwnd, 6)  # SW_MINIMIZE
        except Exception as exc:
            errors.append(exc)
        return True

    deadline = time.monotonic() + 30
    extra_until: float | None = None
    while True:
        if not user32.EnumWindows(minimize, 0):
            raise ctypes.WinError()
        if errors:
            raise errors[0]
        now = time.monotonic()
        if extra_until is None:
            if any(window_owner(hwnd) != main_pid for hwnd in handles):
                raise RuntimeError("BetterGI 窗口已关闭或句柄已变化，停止最小化")
            if all(user32.IsIconic(hwnd) for hwnd in handles):
                extra_until = now + 1
            elif now >= deadline:
                raise TimeoutError("等待 BetterGI 两个窗口最小化超时（30 秒）")
        elif now >= extra_until:
            return
        time.sleep(STARTUP_INTERVAL)


def open_and_connect(ui: FastUIA, exe: Path, current_session: int,
                     timeout: int) -> DesktopContext:
    # Fast path: wait for native windows and invoke the two WPF buttons.
    main = find_window("更好的原神", current_session)
    if main is None:
        print("启动 BetterGI…", flush=True)
        subprocess.Popen([str(exe)], cwd=str(exe.parent))
        main = wait_for(lambda: find_window("更好的原神", current_session),
                        60, "BetterGI 主窗口")
    main_hwnd, main_pid = main
    ui.invoke(main_hwnd, ("打开",), 20)
    child = wait_for(lambda: find_window("BetterGI 桌面分身",
                                        current_session, main_pid),
                     20, "桌面分身窗口")
    child_hwnd, _ = child
    ui.invoke(child_hwnd, ("启动并连接", "启动"), 20)
    minimize_windows(main_pid, child_hwnd, main_hwnd)
    return connect_child(exe, main_hwnd, main_pid, timeout)


def connect_child(exe: Path, main_hwnd: int, main_pid: int,
                  timeout: int) -> DesktopContext:
    print("窗口最小化完成；等待桌面分身连接和登录…", flush=True)
    child_id = wait_for(child_session, timeout, "桌面分身会话")
    print(f"已发现分身 Session {child_id}；等待分身内 BetterGI 启动…", flush=True)

    def check_connection() -> None:
        if child_session() != child_id:
            raise RuntimeError("桌面分身会话在连接期间消失或改变；请查看 BetterGI 日志中的 RDP 连接错误")
        if not process_running(main_pid, "bettergi.exe"):
            raise RuntimeError("BetterGI 主进程已退出，停止等待分身连接")

    def child_process() -> int | None:
        check_connection()
        return named_process_pid(child_id, "bettergi.exe")

    old_pid = wait_for(child_process, timeout, "分身内 BetterGI 登录并启动")
    print(f"分身 BetterGI PID {old_pid}；等待连接主实例…", flush=True)

    def connected() -> bool:
        check_connection()
        if not process_running(old_pid, "bettergi.exe"):
            raise RuntimeError("分身 BetterGI 在连接主实例前已退出")
        return child_connected_to_main(exe.parent / "log", main_pid, child_id, old_pid)

    wait_for(connected, 30, "分身 BetterGI 连接主实例")
    return DesktopContext(main_hwnd, main_pid, child_id, old_pid)


def start_one_dragon(exe: Path, desktop: DesktopContext) -> tuple[CompletionLog, int]:
    child_id = desktop.child_id
    old_pid = desktop.old_child_pid
    print(f"替换 Session {child_id} 中自动启动的 BetterGI，以便执行一条龙…",
          flush=True)
    replace_idle_child_bettergi(child_id, old_pid)
    log = CompletionLog(exe.parent / "log", child_id)
    with send_one_dragon(exe, child_id, old_pid):
        new_pid = wait_for(lambda: named_process_pid(child_id, "bettergi.exe", old_pid),
                           30, "新分身 BetterGI 进程")
        wait_for(lambda: log.completed() or log.started, 60,
                 "分身内一条龙启动日志")
    print(f"已确认一条龙在分身 Session {child_id} 启动。", flush=True)
    return log, new_pid


def run(exe: Path) -> None:
    configure_winapi()
    existing_child_id = child_session()
    if existing_child_id is not None:
        raise RuntimeError(
            f"桌面分身 Session {existing_child_id} 已在运行；"
            "为避免接管并注销现有会话，已停止启动")
    ui = FastUIA()  # Initialize COM before BetterGI's main window is shown.
    current_session = session_of(os.getpid())
    if current_session is None:
        raise RuntimeError("无法取得当前 Windows 会话")
    desktop = open_and_connect(ui, exe, current_session, 120)
    log, child_pid = start_one_dragon(exe, desktop)
    monitor_completion(desktop.child_id, child_pid, log)
    close_after_completion(desktop.main_hwnd, desktop.main_pid, desktop.child_id)
    print("桌面分身与 BetterGI 主进程已关闭。", flush=True)


def main() -> int:
    global _restore_resolution_on_exit
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if sys.platform != "win32":
        parser.error("仅支持 Windows")
    if not ctypes.windll.shell32.IsUserAnAdmin():
        if args.elevated:
            raise RuntimeError("提升权限后仍未取得管理员权限，已停止重复请求")
        argv = [str(Path(__file__).resolve()), "--elevated"]
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, subprocess.list2cmdline(argv), None, 1)
        if result <= 32:
            print(f"管理员权限请求已取消或失败（代码 {result}）", file=sys.stderr)
            return 1
        # 启动器退出时，管理员进程还在运行，由它负责最终恢复。
        _restore_resolution_on_exit = False
        print("已启动管理员后台进程，后续进度继续写入 auto_bgi.log。")
        return 0
    exe = DEFAULT_EXE.expanduser().resolve()
    if not exe.is_file():
        parser.error(f"找不到 BetterGI：{exe}")
    try:
        import comtypes.client  # noqa: F401
    except ImportError:
        print("请先安装 Python 依赖：", file=sys.stderr)
        print(f'"{sys.executable}" -m pip install comtypes', file=sys.stderr)
        return 2
    run(exe)
    return 0


if __name__ == "__main__":
    atexit.register(restore_game_resolution)
    # Windows 关闭控制台不会执行 atexit，单独接收关闭通知。
    console_callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.DWORD)
    console_callback = console_callback_type(handle_console_exit)
    set_console_handler = ctypes.windll.kernel32.SetConsoleCtrlHandler
    set_console_handler.argtypes = [console_callback_type, wintypes.BOOL]
    set_console_handler.restype = wintypes.BOOL
    set_console_handler(console_callback, True)
    with LOG_PATH.open("a", encoding="utf-8", buffering=1) as log_file:
        with redirect_stdout(log_file), redirect_stderr(log_file):
            print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 启动 PID {os.getpid()}", flush=True)
            try:
                exit_code = main()
            except Exception:
                traceback.print_exc()
                exit_code = 1
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 退出，代码 {exit_code}", flush=True)
    raise SystemExit(exit_code)
