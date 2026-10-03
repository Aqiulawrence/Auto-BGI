from __future__ import annotations

import argparse
import ctypes
import getpass
import os
import re
import subprocess
import sys
import threading
import time
import traceback
import uuid
import xml.etree.ElementTree as ET
from contextlib import redirect_stderr, redirect_stdout
from ctypes import wintypes
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

DEFAULT_EXE = Path(r"D:\Tools\BetterGI\BetterGI.exe")
GAME_NAMES = {"yuanshen.exe", "genshinimpact.exe"}
FINISHED_MESSAGE = "一条龙和配置组任务结束"


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
    if error == 1168:  # ERROR_NOT_FOUND: no child session exists.
        return None
    raise ctypes.WinError(error)


def processes() -> list[tuple[int, int, str]]:
    k = ctypes.windll.kernel32
    handle = k.CreateToolhelp32Snapshot(2, 0)  # TH32CS_SNAPPROCESS
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


def wait_for(check, seconds: int, description: str, interval: float = 0.01):
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        result = check()
        if result:
            return result
        time.sleep(interval)
    raise TimeoutError(f"等待{description}超时（{seconds} 秒）")


def find_window(name: str, current_session: int,
                pid: int | None = None) -> tuple[int, int] | None:
    # Native title lookup avoids UI Automation work while a window is loading.
    hwnd = ctypes.windll.user32.FindWindowW(None, name)
    if not hwnd:
        return None
    owner = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
    if (pid is not None and owner.value != pid) or session_of(owner.value) != current_session:
        return None
    return int(hwnd), owner.value


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

    def _available_buttons(self, hwnd: int) -> str:
        try:
            root = self.client.ElementFromHandle(hwnd)
            found = root.FindAll(self.uia.TreeScope_Subtree, self.button_type)
            names = [found.GetElement(i).CurrentName for i in range(min(found.Length, 25))]
            return ", ".join(repr(name) for name in names) or "无"
        except Exception as exc:
            return f"读取失败：{exc}"

    def invoke(self, hwnd: int, names: tuple[str, ...], seconds: int,
               before_invoke=None) -> str:
        from comtypes import COMError

        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                match = self._button(hwnd, names)
            except COMError:  # WPF can replace its UIA tree while loading.
                match = None
            if match:
                pattern, name = match
                if before_invoke is not None:
                    before_invoke()
                pattern.Invoke()
                return name
            time.sleep(0.005)
        raise TimeoutError(
            f"等待按钮 {names} 超时（{seconds} 秒）；窗口中的按钮："
            f"{self._available_buttons(hwnd)}")


class StartupMinimizer:
    """Keep only the startup windows minimized until login finishes and verify it."""

    def __init__(self, *handles: int):
        self.handles = handles
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.started = False

    def start(self) -> None:
        if not self.started:
            self.started = True
            self.thread.start()

    def _run(self) -> None:
        user32 = ctypes.windll.user32
        # Let UI Automation enter the button handler before minimizing it.
        if self.stop_event.wait(0.01):
            return
        last_request = {hwnd: 0.0 for hwnd in self.handles}
        while not self.stop_event.is_set():
            now = time.monotonic()
            for hwnd in self.handles:
                if not user32.IsIconic(hwnd) and now - last_request[hwnd] >= 0.01:
                    user32.ShowWindowAsync(hwnd, 6)
                    last_request[hwnd] = now
            self.stop_event.wait(0.001)

    def confirm_and_stop(self, seconds: int = 30) -> None:
        user32 = ctypes.windll.user32
        wait_for(lambda: all(user32.IsIconic(hwnd) for hwnd in self.handles),
                 seconds, "BetterGI 主窗口和桌面分身窗口最小化", interval=0.001)
        self.close()

    def close(self) -> None:
        self.stop_event.set()
        if self.started:
            self.thread.join(timeout=0.5)


def replace_idle_child_bettergi(child_id: int, old_pid: int) -> None:
    """Make the next CLI invocation the child instance's initial command."""
    if session_of(old_pid) != child_id or not process_running(old_pid, "bettergi.exe"):
        raise RuntimeError("待替换的 BetterGI 已退出或不属于目标分身")
    if named_process_running(child_id, GAME_NAMES):
        raise RuntimeError("分身里已有游戏进程，不能替换正在使用的 BetterGI")
    k = ctypes.windll.kernel32
    handle = k.OpenProcess(0x0001 | 0x00100000, False, old_pid)
    if not handle:
        raise ctypes.WinError()
    try:
        if not k.TerminateProcess(handle, 0):
            raise ctypes.WinError()
        if k.WaitForSingleObject(handle, 5000) != 0:  # WAIT_OBJECT_0
            raise TimeoutError("原分身 BetterGI 未在 5 秒内退出")
    finally:
        k.CloseHandle(handle)


def child_connected_to_main(log_dir: Path, main_pid: int,
                            child_id: int, child_pid: int) -> bool:
    marker = re.compile(
        rf"\[Primary:S\d+:P{main_pid}:T\d+\] .*InstanceService\r?\n"
        rf"桌面分身 BetterGI 已连接根实例：进程 {child_pid}，Session {child_id}")
    for path in log_dir.glob("better-genshin-impact????????.log"):
        if marker.search(path.read_text(encoding="utf-8-sig", errors="replace")):
            return True
    return False


class ScheduledLaunch:
    def __init__(self, folder, name: str, task, running_task):
        self.folder = folder
        self.name = name
        self.task = task
        self.running_task = running_task

    def diagnostics(self) -> str:
        try:
            result = self.task.LastTaskResult
            state = self.running_task.State
            return f"计划任务状态={state}，上次结果=0x{result & 0xFFFFFFFF:08X}"
        except Exception as exc:
            return f"无法读取计划任务状态：{exc}"

    def close(self) -> None:
        self.folder.DeleteTask(self.name, 0)


def send_one_dragon(exe: Path, child_id: int, old_pid: int,
                    config: str) -> ScheduledLaunch:
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
            "startOneDragon", *([config] if config else [])]
    action.Arguments = subprocess.list2cmdline(args)
    command = ET.fromstring(definition.XmlText).find(".//{*}Exec/{*}Command")
    if command is None or command.text != str(exe):
        raise RuntimeError("计划任务的执行动作缺少 BetterGI.exe 路径")
    task_name = f"BetterGI-Child-OneDragon-{uuid.uuid4().hex}"
    registered = False
    try:
        task = folder.RegisterTaskDefinition(task_name, definition, 2, account, None, 3, None)
        registered = True
        # Use an empty VARIANT for "no parameters" and a null BSTR for "no
        # user", matching BetterGI's own RunEx(null, 4, session, null).
        for attempt in range(4):
            try:
                running_task = task.RunEx(VARIANT(), 4, child_id, None)
                if running_task is None:
                    raise RuntimeError("任务计划程序没有返回运行实例")
                return ScheduledLaunch(folder, task_name, task, running_task)
            except COMError as exc:
                if exc.hresult != -2147024809 or attempt == 3:
                    raise RuntimeError(
                        f"无法在分身 Session {child_id} 启动一条龙任务：{exc}"
                    ) from exc
                time.sleep(0.25)  # Login token can lag behind the child process.
    except Exception:
        if registered:
            try:
                folder.DeleteTask(task_name, 0)
            except Exception as cleanup_exc:
                print(f"清理临时计划任务失败：{cleanup_exc}",
                      file=sys.stderr, flush=True)
        raise


class CompletionLog:
    """Read only log lines appended after the One Dragon command is sent."""

    def __init__(self, folder: Path, child_id: int):
        self.folder = folder
        self.marker = re.compile(rf"\[ChildSession:S{child_id}:P\d+:T\d+\]")
        self.positions = {path: path.stat().st_size for path in self._files()}
        self.pending: dict[Path, bytes] = {}
        self.from_child = False
        self.from_dragon = False
        self.started = False
        self.finished = False

    def _files(self) -> list[Path]:
        return sorted(self.folder.glob("better-genshin-impact????????.log"))

    def completed(self) -> bool:
        if self.finished:
            return True
        for path in self._files():
            size = path.stat().st_size
            offset = self.positions.get(path, 0)
            if size < offset:  # A log file was replaced or truncated.
                offset = 0
                self.pending.pop(path, None)
            if size == offset:
                continue
            with path.open("rb") as stream:
                stream.seek(offset)
                chunk = stream.read()
                self.positions[path] = stream.tell()
            lines = (self.pending.pop(path, b"") + chunk).split(b"\n")
            self.pending[path] = lines.pop()
            for raw in lines:
                line = raw.decode("utf-8-sig", errors="replace").strip()
                if re.match(r"^\[\d\d:\d\d:\d\d\.\d+\]", line):
                    self.from_child = bool(self.marker.search(line))
                    self.from_dragon = (self.from_child and
                                        "OneDragonFlowViewModel" in line)
                elif self.from_child and "OneDragonFlowViewModel" in line:
                    self.from_dragon = True
                elif self.from_child and self.from_dragon and FINISHED_MESSAGE in line:
                    self.finished = True
                    return True
                elif self.from_child and self.from_dragon and "启用一条龙配置：" in line:
                    self.started = True
        return False


def monitor_completion(child_id: int, child_pid: int, log: CompletionLog) -> None:
    print("正在等待分身内一条龙完成…", flush=True)
    finished = False
    game_absent_since: float | None = None
    session_absent_since: float | None = None
    bettergi_absent_since: float | None = None
    while True:
        if not finished and log.completed():
            finished = True
            print("已检测到分身的一条龙结束日志；等待游戏关闭…", flush=True)
        current_child_id = child_session()
        if current_child_id != child_id:
            if session_absent_since is None:
                session_absent_since = time.monotonic()
            elif time.monotonic() - session_absent_since >= 5:
                raise RuntimeError(
                    f"桌面分身会话在清理前消失或改变（原 Session {child_id}，"
                    f"当前 {current_child_id}）")
        else:
            session_absent_since = None
        if finished:
            if named_process_running(child_id, GAME_NAMES):
                game_absent_since = None
            elif game_absent_since is None:
                game_absent_since = time.monotonic()
            elif (time.monotonic() - game_absent_since >= 3
                  and current_child_id == child_id):
                print("分身中的游戏已关闭。", flush=True)
                return
        elif process_running(child_pid, "bettergi.exe"):
            bettergi_absent_since = None
        elif bettergi_absent_since is None:
            bettergi_absent_since = time.monotonic()
        elif time.monotonic() - bettergi_absent_since >= 5:
            raise RuntimeError("分身 BetterGI 已退出，但未检测到一条龙结束日志")
        time.sleep(0.5 if finished else 1)


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
    owner = wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(main_hwnd, ctypes.byref(owner))
    if owner.value != main_pid:
        raise RuntimeError("BetterGI 主窗口句柄已变化，未向其他窗口发送关闭消息")
    if not ctypes.windll.user32.PostMessageW(main_hwnd, 0x0010, 0, 0):  # WM_CLOSE
        raise RuntimeError(f"发送关闭消息失败：{ctypes.WinError()}")
    wait_for(lambda: not process_running(main_pid, "bettergi.exe"),
             20, "BetterGI 主进程退出", interval=0.25)


@dataclass(frozen=True)
class DesktopContext:
    main_hwnd: int
    main_pid: int
    child_hwnd: int
    child_id: int
    old_child_pid: int


def open_and_connect(ui: FastUIA, exe: Path, current_session: int,
                     timeout: int) -> DesktopContext:
    # Fast path: wait for native windows and invoke the two WPF buttons.
    main = find_window("更好的原神", current_session)
    if main is None:
        print("启动 BetterGI…", flush=True)
        subprocess.Popen([str(exe)], cwd=str(exe.parent))
        main = wait_for(lambda: find_window("更好的原神", current_session),
                        60, "BetterGI 主窗口", interval=0.005)
    main_hwnd, main_pid = main
    main_window_seen = time.monotonic()
    ui.invoke(main_hwnd, ("打开",), 20)
    open_elapsed = time.monotonic() - main_window_seen
    child = wait_for(lambda: find_window("BetterGI 桌面分身",
                                        current_session, main_pid),
                     20, "桌面分身窗口", interval=0.005)
    child_hwnd, _ = child
    print(f"桌面分身窗口已出现；点击“打开”用了 {open_elapsed:.2f} 秒。", flush=True)
    child_window_seen = time.monotonic()
    minimizer = StartupMinimizer(child_hwnd, main_hwnd)
    try:
        ui.invoke(child_hwnd, ("启动", "启动并连接"), 20, minimizer.start)
        print(f"已调用“启动并连接”（分身窗口出现后 "
              f"{time.monotonic() - child_window_seen:.2f} 秒）；正在确认最小化…",
              flush=True)
        child_id = wait_for(child_session, timeout, "桌面分身会话")
        # BetterGI starts its child process after RDP LoginCompleted.
        old_pid = wait_for(lambda: named_process_pid(child_id, "bettergi.exe"),
                           timeout, "分身内 BetterGI 登录并启动", interval=0.1)
        wait_for(lambda: child_connected_to_main(exe.parent / "log", main_pid,
                                                 child_id, old_pid),
                 30, "分身 BetterGI 连接主实例", interval=0.2)
        minimizer.confirm_and_stop()
    finally:
        minimizer.close()
    print("两个窗口已最小化；后续可手动打开桌面分身。", flush=True)
    return DesktopContext(main_hwnd, main_pid, child_hwnd, child_id, old_pid)


def start_one_dragon(exe: Path, config: str,
                     desktop: DesktopContext) -> tuple[CompletionLog, int]:
    child_id = desktop.child_id
    old_pid = desktop.old_child_pid
    print(f"替换 Session {child_id} 中自动启动的 BetterGI，以便执行一条龙…",
          flush=True)
    replace_idle_child_bettergi(child_id, old_pid)
    log = CompletionLog(exe.parent / "log", child_id)
    launch = send_one_dragon(exe, child_id, old_pid, config)
    try:
        try:
            new_pid = wait_for(lambda: named_process_pid(child_id, "bettergi.exe", old_pid),
                               30, "新分身 BetterGI 进程", interval=0.1)
            print(f"分身 BetterGI 新进程：PID {new_pid}。", flush=True)
            wait_for(lambda: log.completed() or log.started, 60,
                     "分身内一条龙启动日志", interval=0.2)
        except TimeoutError as exc:
            raise RuntimeError(f"{exc}；{launch.diagnostics()}") from exc
    finally:
        try:
            launch.close()
        except Exception as exc:
            print(f"清理临时计划任务失败：{exc}", file=sys.stderr, flush=True)
    print(f"已确认一条龙在分身 Session {child_id} 启动。", flush=True)
    return log, new_pid


def run(exe: Path, config: str, timeout: int) -> None:
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
    desktop = open_and_connect(ui, exe, current_session, timeout)
    log, child_pid = start_one_dragon(exe, config, desktop)
    monitor_completion(desktop.child_id, child_pid, log)
    close_after_completion(desktop.main_hwnd, desktop.main_pid, desktop.child_id)
    print("桌面分身与 BetterGI 主进程已关闭。", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--elevated", action="store_true", help=argparse.SUPPRESS)
    parser.parse_args()
    if sys.platform != "win32":
        parser.error("仅支持 Windows")
    exe = DEFAULT_EXE.expanduser().resolve()
    if not exe.is_file():
        parser.error(f"找不到 BetterGI：{exe}")
    try:
        import comtypes.client  # noqa: F401
    except ImportError:
        print("请先安装 Python 依赖：", file=sys.stderr)
        print(f'"{sys.executable}" -m pip install comtypes', file=sys.stderr)
        return 2
    if not ctypes.windll.shell32.IsUserAnAdmin():
        argv = [str(Path(__file__).resolve()), "--elevated"]
        result = ctypes.windll.shell32.ShellExecuteW(
            None, "runas", sys.executable, subprocess.list2cmdline(argv), None, 1)
        if result <= 32:
            print(f"管理员权限请求已取消或失败（代码 {result}）", file=sys.stderr)
            return 1
        print("已打开管理员 Python 窗口，后续进度会显示在那里。")
        return 0
    try:
        run(exe, "", 120)
        return 0
    except Exception as exc:
        print(f"ERROR: {exc}", file=sys.stderr, flush=True)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    log_path = Path(__file__).with_suffix(".log")
    with log_path.open("a", encoding="utf-8", buffering=1) as log_file:
        with redirect_stdout(log_file), redirect_stderr(log_file):
            print(f"\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 启动 PID {os.getpid()}", flush=True)
            try:
                exit_code = main()
            except Exception:
                traceback.print_exc()
                exit_code = 1
            print(f"[{datetime.now():%Y-%m-%d %H:%M:%S}] 退出，代码 {exit_code}", flush=True)
    raise SystemExit(exit_code)
