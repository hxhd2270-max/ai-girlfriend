# 独立 Windows 壁纸宿主

2026-10-23 壁纸层已接入：根目录 start-desktop.cmd 为正式壁纸，start-desktop-simulation.cmd 为无云模拟。单个 WebView2 窗口附着 Explorer 主屏桌面图标下；原生聊天面板没有第二个播放器或模型连接。Ctrl+Alt+C、桌面右下按钮、托盘可打开面板。“壁纸”菜单可暂停、重新附着或退出；普通关闭只收起面板。重新附着保持 HWND，并检查快捷键绑定。显示变化、父窗口失效会尝试恢复；外部全屏、锁屏、电源暂停通过同一宿主状态处理。真实锁屏、Explorer 重启和多屏尚未验收。完整证据见 [壁纸层报告](../docs/DESKTOP-WALLPAPER-ACCEPTANCE-2026-10-23.md)。

本目录用 C# WinForms + WebView2 打开本机 `/standalone` 页面。推荐壁纸入口是项目根目录的 `start-desktop.cmd`；`start-standalone.cmd` 保留普通窗口。无需运行 Wallpaper Engine、Steam 或旧壁纸配对入口。两种模式复用已有聊天和动作播放器，素材来自 `assets/companion/`。

聊天面板采用暖紫酒红与奶白配色、无系统标题栏和输入描边、圆角及 89% 不透明度。可拖动顶栏、拖拽边缘调整大小；右上 × 或 Escape 收起。用量、重试、静音、音量、设置及“壁纸”控制位于右上“更多”。配色对应当前素材，未实现自动取色。样式验证见 [当前状态](../docs/STATUS-CURRENT.md)。

## 运行

- 正式模式：双击根目录的 `start-standalone.cmd`，沿用已配置账号、相邻 `neko-lab` 与现有 Python 环境。关闭窗口只释放播放；停止后台使用 `stop-neko.cmd`。
- 本地模拟：双击 `start-standalone-simulation.cmd`，使用模拟回复和测试提示音，无云调用。新建的模拟服务随窗口退出停止，已有服务归原启动器管理。
- 模式共用 8765 端口；遇到模式冲突应先停止原模式。未知服务不会被接管或终止。

独立 ZIP 已附带 Python、依赖库、固定 N.E.K.O. 后端、窗口和素材，不需要相邻开发环境。原开发目录仍沿用上面的现有环境。系统要求 Windows x64、.NET Framework 4.6.2+ 与 WebView2 Runtime；宿主已在 WebView2 155.0.4283.62 验收。整包迁移和重新解压后的本机检查见 [发行验收报告](../docs/PORTABLE-ACCEPTANCE-2026-10-23.md)，全新 Windows 安装尚未验证。

文字框使用 Enter 发送、Shift+Enter 换行；支持停止、语音重试、静音、音量与设置窗口。最小化暂停并停止当前播放，恢复后重连且不自动重播旧回复。实际锁屏/休眠及独立窗口的麦克风、真实拼音候选尚需补验。

## 构建

构建使用固定 WebView2 SDK `1.0.4258.31`，应放在 `dependencies/webview2-1.0.4258.31/`，包含 `lib/net462/` 与 `runtimes/win-x64/native/`；当前开发机已具备。SDK 用于构建，WebView2 Runtime 用于运行，二者不同。

在项目根目录执行：

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\desktop-host\build.ps1
```

输出为 `bin/Companion.Desktop.exe`、两份 WebView2 .NET DLL 和 `WebView2Loader.dll`。用户通过根启动器运行；直接双击 EXE 不会自动启动服务。独立素材缺失或存在联接时，启动器拒绝运行；开发者可在完整源素材存在时运行 `python tools/prepare_standalone_assets.py` 重建，不能将该命令当成发行版素材下载器。

## 本地复核

保持 8765 空闲，在项目根目录运行：

```powershell
.\start-standalone-simulation.cmd --acceptance
```

该入口在真实 WebView2 中验证本地模拟、视频解码、播放/取消、音量、重连和生命周期事件，结果写入 `evidence/acceptance-<UTC时间>/`。所有输入与回复均为测试内容，云调用应为 0。锁屏与电源处理函数采用事件注入；实际最小化与恢复另有原生窗口检查。

`live-acceptance.js`、`tools/standalone_live_acceptance.py` 和 `tools/desktop_live_acceptance.py` 是显式真实验收工具，会执行模型与 TTS 调用。普通窗口工具还会重启其拥有的四个服务。它们不由日常或模拟入口自动运行；普通窗口两轮及新增壁纸层一轮的授权均已用完，再次执行需要新的调用授权。不要为了复核文档重跑真实验收。

最新 [验收报告](../docs/STANDALONE-HOST-ACCEPTANCE-2026-10-23.md) 记录证据、真实调用数及未验证范围。浏览器 profile 可能保留会话 Cookie 和历史，分享时只选择报告与合成测试截图，不要打包整个 profile。

## 独立发行

tools/build_portable.py 从明确选定的源码、固定后端和现有运行依赖构建全新目录，再以 portable.json 清单生成 ZIP。打包入口只选择固定文件和初始零限额配置，即使验收目录产生数据，也不会把 data、账号或窗口 profile 加入 ZIP。tools/verify_portable_archive.py 校验归档清单、全体固定文件 SHA-256 和全部 CRC，再解压复核。启动时以 -I -B -X utf8 运行包内 Python，关闭用户扩展目录并使用 UTF-8。

tools/portable_acceptance.py 是外置离线验收工具，不随用户 ZIP 发布；它给四个真实服务安装外网阻止层，使用内存假凭据及零限额，检查原生角色会话的连接和释放，不发送真实聊天或合成人声。不能把这项检查说成迁移包已经重新完成真实云对话。发行包中文说明见 [PORTABLE-README](../docs/PORTABLE-README.md)。
