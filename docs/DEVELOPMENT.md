---
_file:
  created: "2026-11-22 20:00"
  updated: "2026-11-22 20:00"
  status: final
---

# 开发与构建

## 开发环境

已验证 Windows 11 x64、Python 3.13.15、PyWebView 6.2.1、pythonnet 3.2.0 和 FFmpeg 8.0.1 full build。浏览器后端使用 Python 标准库；桌面窗口需要 PyWebView 和系统 .NET Framework 4.8／WebView2。

安装 FFmpeg／FFprobe 并加入 PATH 后，可从仓库根目录运行：

```powershell
python app/backend/app.py --port 8787
```

桌面方式可使用 Conda 与 PowerShell 7 执行 `app/setup-env.ps1`，然后运行 `app/start.bat`。桌面运行保持普通用户权限，以便接收资源管理器拖入的文件。

## 数据隔离与测试

测试时设置 `VCS_DATA_DIR` 为新的临时目录，避免访问实际队列和设置。需要单独覆盖 store 时使用 `VCS_STORE_DIR`。媒体测试只使用自己生成的小样片，保持内存余量并设置超时。

例如在 PowerShell 中运行包装清单回归：

```powershell
python -B -m unittest -v app.tests.test_packaging_media app.tests.test_packaging_licenses
```

其他测试按脚本分别运行；部分媒体、桌面、浏览器和真实回收站测试需要对应环境与隔离前提，不应直接对个人数据运行全部测试。使用独立于真实用户数据的新测试目录。

依赖开发期私有 runner 的 `test_memory_peak.py` 峰值观察脚本未纳入此公开快照；它不影响产品运行。使用共享合成样本的测试需先运行 `test_e2e.py` 生成夹具；音轨矩阵、位图字幕和回收站测试还要求相应隔离环境变量，不属于上述快速回归。

## Windows 安装包构建

`app/packaging/build_release.ps1` 先生成 PyInstaller 目录版，再调用 Inno Setup 编译安装器。构建工具和缓存不随源码发布；脚本不会自动下载全部构建依赖。复建时需准备：

1. Python 3.13 环境及桌面依赖，默认路径可用 `-Python <python.exe路径>` 覆盖。
2. `_temp/packaging/tools/python/` 中的 PyInstaller（发布构建使用 6.22.3）与 hooks。
3. `_temp/packaging/tools/inno/ISCC.exe` 及其配套编译器文件（发布构建使用 Inno Setup 7.1.1）。
4. `_temp/packaging/downloads/ffmpeg-8.0.1-full_build-shared/` 中的官方 bin、LICENSE 和 README.txt。
5. `_temp/packaging/downloads/ffmpeg-894da5ca7d-source.tar.gz`，为对应 FFmpeg 源码归档。
6. `_temp/packaging/downloads/MicrosoftEdgeWebView2RuntimeInstallerX64.exe`，须为微软有效签名的 x64 完整离线安装器。

也可用 `-BuildRoot` 指定另一个缓存根目录、用 `-MediaDir` 指定共享媒体工具目录。

```powershell
./app/packaging/build_release.ps1 -Stage Bundle -Python '<python.exe路径>'
./app/packaging/build_release.ps1 -Stage Installer -Python '<python.exe路径>' -BundleDir '<已验证的目录版路径>'
```

媒体运行文件采用九项明确清单：两个 EXE 和七个版本化 DLL。脚本在准备阶段检查文件完整和共享构建配置，记录 SHA256，PyInstaller 构建时去除重复的根层 DLL。

项目根目录的 `LICENSE` 与 `THIRD_PARTY_NOTICES.md` 也是打包必需输入，缺失时不会创建 payload；它们进入安装目录 `_internal/licenses/application/` 并记录 SHA256。MIT 适用于自有源码，第三方组件保留各自许可。

`app/packaging/run_bounded.py` 为验证提供超时、进程树回收与内存余量保护。被冻结的 `--self-test`、`--test-window` 入口要求全新私有 `VCS_DATA_DIR`，并拒绝指向已有 store；原生窗口验证需要正常 Windows 桌面会话。构建和测试的日志保存在对应运行目录。

第三方二进制公开前还需准备准确对应的第三方源码、许可证及构建材料，详见 [第三方组件说明](../THIRD_PARTY_NOTICES.md)。当前源码仓库不提供自动完成所有依赖获取的构建流水线。

## 公开源码快照

本仓库从经过筛选的当前产品源码建立独立的首次公开提交，包含产品模块、页面、预设、打包脚本和可公开测试。开发过程中的本机日志、内部交接材料、用户媒体、队列、个人设置及原本地 Git 历史保留在维护者本机。

自有源码为 MIT；开发贡献说明见根目录 README。安装版 0.7.1 的既有功能验证记录用于说明该二进制的测试范围，新的代码变更仍需对应验证。
