---
_file:
  created: "2026-11-22 19:57"
  updated: "2026-11-22 19:57"
  status: final
---

# 视频压缩套件 · Video Compression Suite

面向 Windows 的本地批量视频压缩工具，提供桌面窗口、压缩目标选择、硬件编码检测、拖动进度条兼容性校验和修复。

**本项目的主要代码、功能实现与迭代由 OpenAI Codex 和 DeepSeek 完成，项目维护者负责需求、方向选择与使用验收。**

## 下载与安装

当前公开项目源码。Windows 离线安装包已完成本机验证，暂存于发布草稿；正在补齐所含 FFmpeg 外部库的对应源码和构建材料，完成后会在 [Releases](https://github.com/Enxulansis/video-compression-suite/releases) 开放下载。

安装包为 Windows x64 离线安装 EXE，公开后下载并双击安装，从开始菜单打开即可。

- 安装包约 **297.4 MiB**，程序本体约 **267.0 MiB**。
- 内置 Python、桌面依赖、FFmpeg／FFprobe、应用本地 VC 运行库和 WebView2 完整离线安装程序，无需配置开发环境或 PATH。
- 支持 Intel／AMD x64 Windows 10 2004（19041）及以上、Windows 11；系统的 .NET Framework 4.8 需保持可用。
- 缺少 WebView2 的电脑会安装随包运行环境，其额外磁盘占用另计。
- 安装包尚未签名，Windows 可能显示“未知发布者”。发布页同时提供 SHA256 校验文件。

## 能做什么

- **批量压缩**：支持 H.264、HEVC、AV1 等常用输出，提供简单目标和高级参数。
- **显卡与软件编码**：检测实际可用的硬件编码器；设备不支持时使用适用的软件编码方案。
- **压缩后交付校验**：检查解码、时长、轨道和关键帧间隔，帮助确认成品可播放与定位。
- **修复拖动卡顿**：诊断长 GOP 等定位问题并提供修复策略。
- **体积与画质分析**：提供样片体积测算、目标体积、VMAF、取帧及差值图。
- **任务管理**：支持队列、历史记录、暂停继续和内存保护。

编码速度、压缩率和硬件能力取决于输入内容、参数及电脑配置。默认保留源文件；启用源文件处理时以界面确认的本次操作为准。

## 使用

1. 拖入视频或选择文件。
2. 选择压缩目标及成品保存位置。
3. 开始处理，查看任务完成后的体积和校验结果。

个人设置、队列、历史、日志和收件箱保存在 `%LOCALAPPDATA%\VideoCompressionSuite`，卸载会保留个人数据和媒体文件。更新前等待当前任务完成并关闭程序；安装器会拒绝覆盖仍在使用的程序或媒体工具。

## 从源码运行

源码使用 Python 标准库后端、HTML／CSS／JavaScript 页面、PyWebView 桌面宿主和外部 FFmpeg 进程。

**浏览器方式**：准备 Python（本项目验证版本为 3.13.15）及支持所需编码器的 FFmpeg／FFprobe，并使媒体工具可从 PATH 找到，然后运行：

```powershell
python app/backend/app.py --port 8787
```

打开 <http://127.0.0.1:8787>。

**桌面方式**：在已安装 Conda 和 PowerShell 7 的 Windows 上运行 `app\setup-env.bat` 建立独立的 `vcs` 环境，再运行 `app\start.bat`。源码版也需要可用的 FFmpeg／FFprobe；Python 环境安装脚本不安装媒体工具。桌面窗口支持原生文件拖入。

源码版状态保存在本地 `app/backend/store`，与安装版数据目录分开。

## 目录

```text
app/backend/    本地 HTTP 后端、编码计划、队列和校验
app/web/        页面和交互
app/packaging/  Windows 冻结、安装器和受限验证入口
app/tests/      单元测试与合成媒体验证脚本
docs/          开发与构建说明
LICENSE        项目自有源码的 MIT 许可
```

开发与构建见 [docs/DEVELOPMENT.md](docs/DEVELOPMENT.md)。安装包通过 Releases 分发，仓库不收录个人数据、真实视频、开发历史、构建缓存或运行库二进制。

## 验证范围与已知限制

0.7.1 已在维护者的 Windows 11 x64 电脑验证安装、旧版升级、仅 Windows PATH 的独立运行、软件和本机 NVENC 编码、AAC 与文本字幕、修复、深度解码与关键帧、VMAF、差值图、桌面桥接、占用保护和卸载。

- 另一台空白电脑、Windows 10 实机及完全缺失 WebView2 后的离线补装尚待验证。
- 重复启动会复用现有实例；窗口未自动置顶时可点击任务栏图标。
- 其他显卡、驱动、特殊轨道和高分辨率场景需依据设备实测。

反馈问题时请附 Windows 版本、显卡与驱动、操作步骤及脱敏后的日志。视频内容和完整个人路径可按需要遮去。

## 协议与第三方组件

本项目自有源码按 [MIT License](LICENSE) 发布。

FFmpeg、Python、PyWebView、pythonnet、VC 运行库和 WebView2 等第三方组件适用各自许可。0.7.1 安装包使用启用 GPL 的 FFmpeg 8.0.1 full shared 构建，其许可、构建配置说明及对应 FFmpeg 源码归档随安装包提供；位置为 `_internal/licenses`。第三方组件的许可范围见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
