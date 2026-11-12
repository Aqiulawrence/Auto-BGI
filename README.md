# Auto-BGI

| 文件 | 用途 |
| --- | --- |
| `auto_bgi.pyw` | 启动 BetterGI 桌面分身，最小化窗口并运行“一条龙”，等待任务结束并清理分身。 |
| `startup.pyw` | 创建登录时运行的计划任务；任务仅在主桌面会话中启动 Steam、Clash Verge。 |

运行环境为 Windows、Python 3.10 或更高版本。使用运行脚本的 Python 安装依赖：`python -m pip install comtypes`。

脚本默认使用 `D:\Tools\BetterGI\BetterGI.exe`。如果安装位置不同，先修改 `auto_bgi.pyw` 开头的 `DEFAULT_EXE`。然后双击 `auto_bgi.pyw`，或用 `pythonw.exe` 运行它。脚本会在需要时请求管理员权限；启动后没有控制台窗口。

运行时，脚本依次点击“打开”和“启动并连接”，随后每 10ms 检查 BetterGI 主实例的窗口；两个窗口首次确认最小化后，再持续检测 1 秒，处理重新显示或新建的窗口，然后停止，此后可手动恢复。一条龙确认启动前按 10ms 间隔轮询；确认启动后每 1s 检测任务完成和游戏退出。BetterGI 需设置任务完成后关闭游戏和软件，脚本随后注销桌面分身并关闭 BetterGI 主进程。如果已有桌面分身在运行，脚本会停止，以免接管现有会话。

仅管理员进程记录日志；非管理员进程只负责提权，不打开或写入日志文件。运行记录和错误通过 Python 标准 `logging` 模块追加到脚本旁的 `auto_bgi.log`，每条记录以本地日期时间 `[YYYY-MM-DD HH:MM:SS]` 开头。异常以一条记录输出，时间戳放在错误摘要前，后续堆栈保留原有缩进，不逐行重复时间戳。BetterGI 自己的日志仍在其安装目录的 `log` 文件夹中。如果流程中途报错，先查看 `auto_bgi.log`；此时分身或 BetterGI 可能仍在运行。

脚本正常退出或报错退出时，会将 `HKEY_CURRENT_USER\Software\miHoYo\原神` 下的 `Screenmanager Resolution Height_h2627697771` 写为 `1440`，将 `Screenmanager Resolution Width_h182942802` 写为 `2560`，类型均为 `REG_DWORD`。从控制台运行时，按 `Ctrl+C` 或点击控制台窗口右上角的关闭按钮也会执行恢复。恢复结果写入 `auto_bgi.log`；强制结束进程或断电等情况不在支持范围内。

---

双击 `startup.pyw`，或用 `pythonw.exe` 运行它，会创建或更新名为 `Auto-BGI Startup` 的计划任务。创建成功后会显示提示；下次当前用户登录 Windows 时，任务在交互式桌面中运行脚本。直接运行脚本只负责设置自启，不会立即启动下面的程序。

计划任务运行时，脚本只在主桌面会话中依次启动：

- `C:\Program Files (x86)\Steam\steam.exe`
- `C:\Program Files\Clash Verge\clash-verge.exe`

随后打开 `C:\Users\Admin\Desktop\待办.txt`。这些路径可在 `startup.pyw` 的 `STARTUP_PROGRAMS` 和 `TODO_FILE` 中修改。

开发验证：在项目目录运行 `python -m unittest discover -s tests -v`。测试使用临时日志文件和模拟系统调用，覆盖日志来源隔离、半行读取、文件截断、完成等待、会话保护、任务清理与主桌面判断，不会启动游戏、注册计划任务或注销会话。实际 BetterGI 的 UI Automation 与 COM 调度仍需在目标环境中验证。
