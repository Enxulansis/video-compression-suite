---
_file:
  created: "2026-11-22 19:57"
  updated: "2026-11-22 20:15"
  status: final
---

# 第三方组件

项目根目录的 MIT 许可适用于本项目自有源码。以下组件保留原作者的版权和许可，安装包中的第三方文件不因本项目采用 MIT 而改变许可。

本项目主要代码、功能实现与迭代由 OpenAI Codex 和 DeepSeek 完成，项目维护者负责需求、方向选择与使用验收。项目自有源码的版权与许可见随附的 MIT LICENSE。

| 组件 | 在项目中的用途 | 许可与来源 |
|---|---|---|
| FFmpeg／FFprobe 8.0.1 full shared | 通过独立进程执行媒体探测、编码、解码与滤镜分析 | Gyan 的 GPL v3 构建，见 [上游发布](https://github.com/GyanD/codexffmpeg/releases/tag/8.0.1)及 [FFmpeg 许可说明](https://ffmpeg.org/legal.html) |
| Python 3.13.15 | 安装版解释器和标准库 | [PSF License](https://docs.python.org/3.13/license.html) |
| PyWebView 6.2.1 | 桌面窗口与网页桥接 | [BSD 3-Clause](https://github.com/r0x0r/pywebview/blob/master/LICENSE) |
| pythonnet 3.2.0 | Python 与 .NET 桌面宿主互操作 | [MIT](https://github.com/pythonnet/pythonnet/blob/master/LICENSE) |
| clr_loader、proxy_tools、bottle、typing_extensions、cffi、pycparser | 桌面宿主及其运行依赖 | 安装目录各组件的 METADATA 和许可证文件 |
| Microsoft VC 运行库、WebView2 | Windows 本地运行依赖及网页渲染 | Microsoft 各组件许可；WebView2 使用微软原始签名的完整离线安装程序 |
| PyInstaller | 制作冻结程序 | [GPL 及 bootloader 分发例外](https://pyinstaller.org/en/stable/license.html) |

FFmpeg 的两个工具与七个共享 DLL 一并提供，保留原始名称。该构建启用 `--enable-gpl`、`--enable-version3`、`--enable-shared`，包含 libx264、libx265、libsvtav1、libvmaf 等外部组件。构建的实际配置可运行包内 `ffmpeg.exe -version` 查看，各外部组件保留各自许可。

安装目录 `_internal/licenses/ffmpeg/` 包含构建方的 `LICENSE`、`README.txt` 及 FFmpeg 修订 `894da5ca7d` 的源码归档。第三方分发来源和文件 SHA256 记录在 `_internal/release-info.json`。

**安装包公开状态**：现有归档包含 FFmpeg 主体源码；该次构建所用全部外部库的准确源码、补丁及构建脚本尚待取得和核对。因此 Windows 安装包暂存发布草稿，当前公开源码仓库不包含这些第三方二进制。

- 构建方：[Gyan FFmpeg builds](https://www.gyan.dev/ffmpeg/builds/)
- FFmpeg 对应修订：[894da5ca7d](https://github.com/FFmpeg/FFmpeg/commit/894da5ca7d)
- FFmpeg full shared 归档 SHA256：`8030dc469fbde247b84cfc21a5c421f3965ffe779bc35de08d78966e0c4a272c`
- WebView2 分发说明：[Microsoft 文档](https://learn.microsoft.com/en-us/microsoft-edge/webview2/concepts/distribution)

重新分发或修改安装包时，应保留随包许可证、版权声明、第三方源码与构建来源信息。
