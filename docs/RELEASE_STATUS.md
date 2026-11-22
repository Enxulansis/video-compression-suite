---
_file:
  created: "2026-11-22 20:00"
  updated: "2026-11-22 20:00"
  status: final
---

# 0.7.1 安装包状态

## 已完成的本机验证

安装包为 311,852,339 字节（297.4 MiB）；程序本体为 280,006,370 字节（267.0 MiB）。安装后的 164 个文件与冻结目录版逐字节一致。

70 项运行检查和 6 项原生桌面检查通过，包括软件 x264／x265／AV1、本机 NVENC、音轨和文本字幕、深度解码与关键帧、长 GOP 修复、VMAF 与差值图。临时目录中的旧版升级、重复实例保护、转码占用保护和卸载已通过。未在 Windows 10／另一台空白电脑或缺少 WebView2 的系统验证。

原安装 EXE 的 SHA256 为：

```text
D37589B8E7D5D0F435C7F8DA0ABEECA300EDBD7990172ACBCCD478ABDA2FE111
```

## 当前公开范围

项目自有源码已按 MIT 准备公开，第三方库与 Windows 安装 EXE 不进入源码 Git 历史。安装包暂存 Release 草稿，尚未开放公共下载。

安装包包含 Gyan 的 FFmpeg 8.0.1 full shared GPL v3 构建。现有材料有 FFmpeg 主体修订 `894da5ca7d` 的源码、实际配置、许可和依赖版本清单；全部外部依赖的准确源码、补丁及本次构建脚本尚未取得和核对。取得并整理完整对应材料后，再公开二进制下载。Gyan 发布页自动生成的 Source code 归档不包含这套完整构建材料。

核对入口：

- [8.0.1 发布与构建配置](https://github.com/GyanD/codexffmpeg/releases/tag/8.0.1)
- [Gyan 关于构建源码请求的置顶回复](https://github.com/GyanD/codexffmpeg/issues/260#issuecomment-7057531903)
- [同版源码请求讨论](https://github.com/GyanD/codexffmpeg/issues/250#issuecomment-6380032174)
- [FFmpeg 许可说明](https://ffmpeg.org/legal.html)

这项材料核对不影响项目自有源码的 MIT 许可，亦不改变已经完成的本机功能验证结果。
