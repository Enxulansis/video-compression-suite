---
_file:
  created: "2026-11-22 20:00"
  updated: "2026-11-23 20:57"
  status: final
---

# 0.7.1 安装包状态

## 已完成的本机验证

安装包为 311,852,889 字节（297.4 MiB）；程序本体为 280,010,839 字节（267.0 MiB）。安装后的 166 个文件与冻结目录版逐字节一致。

补充许可声明后的候选包完成 70 项运行检查和 6 项原生桌面检查，包括软件 x264／x265／AV1、本机 NVENC、音轨和文本字幕、深度解码与关键帧、长 GOP 修复、VMAF 与差值图。临时安装、重复实例保护、转码占用保护和卸载通过；既有旧版升级验证记录保留。未在 Windows 10／另一台空白电脑或缺少 WebView2 的系统验证。

当前候选安装 EXE 的 SHA256 为：

```text
7F91530FE8BDDFC80FB55A16290707C03756C35C5F072F4BF889FEA9E21EA8D8
```

## 当前公开范围

项目自有源码按 MIT 公开，Windows 离线安装 EXE 通过 [v0.7.1 Release](https://github.com/Enxulansis/video-compression-suite/releases/tag/v0.7.1) 分发；第三方源码附件与安装包放在同一发布页，不进入源码 Git 历史。

候选安装包的 `_internal/licenses/application/` 已包含本项目 MIT LICENSE、第三方声明与 Codex／DeepSeek 开发贡献说明，两个文件的字节内容和清单 SHA256 均已验证。

安装包包含 Gyan 的 FFmpeg 8.0.1 full shared GPL v3 构建。同页 `FFmpeg-8.0.1-full-shared-sources.tar` 提供 FFmpeg 主体修订 `894da5ca7d`、66 个固定 Git 条目及子模块、MSYS2 固定版本源码／配方、原始许可和 Shaderc／rav1e 锁定依赖。实际 configure 输出与编译器信息随材料提供。

构建方已提供固定提交和工具链清单，并表示未应用功能性补丁、采用上游标准构建说明。本项目未重新编译媒体内核或验证逐字节可复现。`simd_helpers` 原始 MIT 声明保留，并配标准许可文本和作者署名。

核对入口：

- [8.0.1 发布与构建配置](https://github.com/GyanD/codexffmpeg/releases/tag/8.0.1)
- [Gyan 关于构建源码请求的置顶回复](https://github.com/GyanD/codexffmpeg/issues/260#issuecomment-7057531903)
- [同版源码请求讨论](https://github.com/GyanD/codexffmpeg/issues/250#issuecomment-6380032174)
- [FFmpeg 许可说明](https://ffmpeg.org/legal.html)

这项材料核对不影响项目自有源码的 MIT 许可，亦不改变已经完成的本机功能验证结果。
