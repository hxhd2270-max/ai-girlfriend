# AI女友

AI女友是一个 Windows 桌面陪伴项目，包含本地网页界面、文本生成、按句语音合成、
角色动作衔接，以及可选的 N.E.K.O. 记忆后端。公开展示名是 AI女友，当前默认角色
名仍然是知夏。

仓库中的源码、演示图片和 Word 使用说明可以直接查看。完整的 Windows 可运行包放在
GitHub Releases 中，因为它包含隔离 Python 运行时和依赖，体积超过普通仓库适合直接
存放的范围。下载 Release 中的 `AI女友-Windows-20261025-r2.zip` 后，解压并运行包内的
`安装入口.cmd` 即可安装；安装后可在离线模拟模式下先体验界面，再配置自己的模型服务。

公开候选明确排除 API Key、对话记录、记忆数据库、本机日志、浏览器配置、开发台账和
用户本地配置。真实模型调用需要使用者自己的 API 服务和密钥，不包含项目作者的密钥。

`demo/` 中的图片来自项目作者本机屏幕录制的授权素材，已经裁剪为角色画面，演示视频
放在 Windows Release 包内，并且已去除录音和桌面私密区域。原始屏幕录制不发布。

First-party source in this repository is released under the MIT License. See
`THIRD_PARTY_NOTICES.md` for upstream and dependency boundaries. Character
media and provider services have separate rights and terms.

## 仓库结构

- `desktop-companion-ai/`: 陪伴服务、网页端、Windows 宿主源码和启动脚本。
- `neko-lab/`: 固定 N.E.K.O. 版本的本地集成适配器。
- `scripts/`: 公开构建和上游项目准备脚本。
- `demo/`: 经过裁剪的公开演示图片。
- `docs/AI女友使用说明.docx`: 面向不懂代码用户的 Word 使用说明。

The N.E.K.O. upstream source is not vendored. The expected revision is recorded
in `neko-lab/PINNED-UPSTREAM.txt`. Run `scripts/setup-neko-upstream.ps1`
after reviewing that script to clone the official upstream source at the pinned
commit.

## 源码开发

1. Install Python 3.11 and create a virtual environment.
2. Install `desktop-companion-ai/requirements.txt` and the dependencies
   required by the pinned N.E.K.O. project.
3. Run `scripts/setup-neko-upstream.ps1`.
4. Supply your own provider connection through the local API settings page.
   Credentials are stored for the current Windows user with DPAPI and are never
   part of the repository.
5. Add character action media under
   `desktop-companion-ai/assets/companion/actions/`, or use the simulation
   path while developing without distributable media.

The current Windows host build also needs the WebView2 SDK described in
`desktop-companion-ai/desktop-host/README.md`. Binary SDK copies and system
installers are intentionally excluded from this source repository.

## 当前能力

The companion starts TTS from complete sentence boundaries while the remaining
text is still streaming. A short preparation action can be interrupted as soon
as the first audio segment is decoded; audio playback and the speaking action
then start together. Later TTS requests are bounded and delivered in sentence
order.

## 发布与使用说明

本项目第一方源码采用 MIT License。发布前请阅读 `OPEN_SOURCE_READINESS.md`、
`PRIVACY.md` 和 `SECURITY.md`。完整包包含可运行资源，Release 页面会同时提供
`AI女友使用说明.docx`。

当前任务路由支持 Chat Completions、Anthropic Messages、OpenAI Responses、Gemini
Generate Content、语音合成、语音转写和已有的 Token Plan 适配器。服务商凭据只在本机
配置，不进入公开仓库。人物设定支持背景故事、价值观、习惯、自我模型、边界和互动方式；
这表示可配置的角色设定，不宣称真实意识。

## 致谢

感谢以下 GitHub 开源项目的作者和维护者：

- [N.E.K.O.](https://github.com/Project-N-E-K-O/N.E.K.O)：角色动作和可选记忆后端。
- [FastAPI](https://github.com/fastapi/fastapi)、[Uvicorn](https://github.com/encode/uvicorn)、
  [httpx](https://github.com/encode/httpx)、[websockets](https://github.com/python-websockets/websockets)：
  本地服务与网络通信。
准确版本和许可证以仓库中的 `THIRD_PARTY_NOTICES.md`、Release 包中的
`THIRD-PARTY-NOTICES.txt` 以及各依赖自身的许可证文件为准。

## 项目说明

作者完全不懂代码，这个项目主要由 GPT 协助完成。项目还会持续优化和更新，欢迎大家
试用、提意见、报告问题，也希望大家喜欢这个项目。

## Data safety

Read `PRIVACY.md` and `SECURITY.md`. Build public copies only with the
allowlist script in the private development tree, then run the fail-closed audit
before creating a Git repository or archive.

