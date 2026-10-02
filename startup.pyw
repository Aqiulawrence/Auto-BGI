"""登录时仅在主桌面启动 Steam 和 Clash Verge。"""

import ctypes
import os
import subprocess
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
wts = ctypes.WinDLL("wtsapi32", use_last_error=True)
kernel32.ProcessIdToSessionId.argtypes = [wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
wts.WTSQuerySessionInformationW.argtypes = [
    wintypes.HANDLE, wintypes.DWORD, ctypes.c_int,
    ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(wintypes.DWORD),
]
wts.WTSFreeMemory.argtypes = [ctypes.c_void_p]

session = wintypes.DWORD()
buffer = ctypes.c_void_p()
size = wintypes.DWORD()

if kernel32.ProcessIdToSessionId(os.getpid(), ctypes.byref(session)):
    # WTSClientProtocolType = 16；主桌面为 0，RDP 分身为 2。
    if wts.WTSQuerySessionInformationW(
        None, session.value, 16, ctypes.byref(buffer), ctypes.byref(size)
    ):
        try:
            if size.value >= 2 and ctypes.cast(buffer, ctypes.POINTER(ctypes.c_ushort))[0] == 0:
                steam = r"C:\Program Files (x86)\Steam\steam.exe"
                clash = r"C:\Program Files\Clash Verge\clash-verge.exe"
                subprocess.Popen([steam, "-silent"], cwd=os.path.dirname(steam))
                subprocess.Popen([clash], cwd=os.path.dirname(clash))
        finally:
            wts.WTSFreeMemory(buffer)
