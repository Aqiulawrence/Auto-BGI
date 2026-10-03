# Auto-BGI

| 文件 | 用途 |
| --- | --- |
| `auto_bgi.pyw` | 最小化启动 BetterGI 桌面分身并运行“一条龙”，等待任务结束并清理分身。 |
| `startup.pyw` | 创建登录时运行的计划任务；任务仅在主桌面会话中启动 Steam、Clash Verge。 |

脚本默认使用 `D:\Tools\BetterGI\BetterGI.exe`。如果安装位置不同，先修改 `auto_bgi.pyw` 开头的 `DEFAULT_EXE`。然后双击 `auto_bgi.pyw`，或用 `pythonw.exe` 运行它。脚本会在需要时请求管理员权限；启动后没有控制台窗口。

运行时，脚本会最小化打开 BetterGI 的桌面分身，启动默认的一条龙配置，等待 BetterGI 日志报告任务结束及分身中的游戏退出（需BetterGI设置任务完成后关闭游戏和软件），随后注销桌面分身并关闭 BetterGI 主进程。如果已有桌面分身在运行，脚本会停止，以免接管现有会话。

运行记录和错误会追加到脚本旁的 `auto_bgi.log`。BetterGI 自己的日志仍在其安装目录的 `log` 文件夹中。如果流程中途报错，先查看 `auto_bgi.log`；此时分身或 BetterGI 可能仍在运行。

---

双击 `startup.pyw`，或用 `pythonw.exe` 运行它，会创建或更新名为 `Auto-BGI Startup` 的计划任务。创建成功后会显示提示；下次当前用户登录 Windows 时，任务在交互式桌面中运行脚本。直接运行脚本只负责设置自启，不会立即启动下面的程序。

计划任务运行时，脚本只在主桌面会话中依次启动：

- `C:\Program Files (x86)\Steam\steam.exe`
- `C:\Program Files\Clash Verge\clash-verge.exe`