"""Collect an explicit release payload, licenses and public provenance."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone, timedelta
import hashlib
import importlib.metadata as md
import json
from pathlib import Path
import shutil
import subprocess
import sys

MEDIA_FILES = (
    'ffmpeg.exe', 'ffprobe.exe', 'avcodec-62.dll', 'avdevice-62.dll',
    'avfilter-11.dll', 'avformat-62.dll', 'avutil-60.dll',
    'swresample-6.dll', 'swscale-9.dll',
)


def media_runtime_files(directory: Path):
    missing = [name for name in MEDIA_FILES if not (directory / name).is_file()]
    if missing:
        raise FileNotFoundError('Incomplete FFmpeg 8.0.1 full shared runtime: ' + ', '.join(missing))
    return [directory / name for name in MEDIA_FILES]


def sha256(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--downloads', required=True, type=Path)
    parser.add_argument('--media-dir', required=True, type=Path)
    args = parser.parse_args()
    source_root = Path(__file__).resolve().parents[1]
    application_license_inputs = [source_root.parent / name for name in ('LICENSE', 'THIRD_PARTY_NOTICES.md')]
    missing_licenses = [str(path) for path in application_license_inputs if not path.is_file()]
    if missing_licenses:
        raise FileNotFoundError('Missing project license files: ' + ', '.join(missing_licenses))

    media_inputs = media_runtime_files(args.media_dir)
    version = subprocess.run([str(args.media_dir / 'ffmpeg.exe'), '-version'],
                             capture_output=True, encoding='utf-8', errors='replace', timeout=10,
                             creationflags=subprocess.CREATE_NO_WINDOW)
    banner = version.stdout.splitlines()[0] if version.stdout else ''
    configuration = next((line for line in version.stdout.splitlines()
                          if line.startswith('configuration:')), '')
    if (version.returncode or '8.0.1-full_build-www.gyan.dev' not in banner
            or '--enable-shared' not in configuration.split()):
        raise RuntimeError('Expected the pinned FFmpeg 8.0.1 full shared build: ' + banner)
    args.output.mkdir(parents=True, exist_ok=False)
    licenses = args.output / 'licenses'
    licenses.mkdir()
    media = args.output / 'bin'
    media.mkdir()
    manifest = {'application': 'Video Compression Suite', 'version': '0.7.1',
                'architecture': 'AMD64', 'minimum_windows_build': 19041,
                'created': datetime.now(timezone(timedelta(hours=8))).isoformat(timespec='seconds'),
                'python': sys.version.split()[0], 'packages': {}, 'files': {},
                'media_runtime_files': list(MEDIA_FILES), 'ffmpeg_linkage': 'shared',
                'ffmpeg_banner': banner,
                'ffmpeg_archive_sha256': '8030dc469fbde247b84cfc21a5c421f3965ffe779bc35de08d78966e0c4a272c'}

    application_licenses = licenses / 'application'
    application_licenses.mkdir()
    for source in application_license_inputs:
        target = application_licenses / source.name
        shutil.copy2(source, target)
        manifest['files'][target.relative_to(args.output).as_posix()] = sha256(target)

    for source in media_inputs:
        target = media / source.name
        shutil.copy2(source, target)
        manifest['files']['bin/' + target.name] = sha256(target)
    for filename in ('LICENSE', 'README.txt'):
        dest = licenses / 'ffmpeg' / filename
        dest.parent.mkdir(exist_ok=True)
        shutil.copy2(args.media_dir.parent / filename, dest)
    archive = args.downloads / 'ffmpeg-894da5ca7d-source.tar.gz'
    shutil.copy2(archive, licenses / 'ffmpeg' / archive.name)
    manifest['files']['licenses/ffmpeg/' + archive.name] = sha256(archive)
    manifest['ffmpeg_source'] = 'https://github.com/FFmpeg/FFmpeg/commit/894da5ca7d'
    manifest['ffmpeg_binary_origin'] = 'https://github.com/GyanD/codexffmpeg/releases/tag/8.0.1'
    manifest['webview2'] = {'source': 'https://go.microsoft.com/fwlink/?linkid=2124701',
                           'sha256': sha256(args.downloads / 'MicrosoftEdgeWebView2RuntimeInstallerX64.exe')}
    package_names = ('pywebview', 'pythonnet', 'clr_loader', 'proxy_tools', 'bottle',
                     'typing_extensions', 'cffi', 'pycparser')
    for name in package_names:
        dist = md.distribution(name)
        manifest['packages'][name] = dist.version
        folder = licenses / name
        folder.mkdir()
        (folder / 'METADATA.txt').write_text(dist.read_text('METADATA') or '', encoding='utf-8')
        for f in dist.files or []:
            if f.name.upper().startswith(('LICENSE', 'LICENCE', 'COPYING', 'NOTICE')):
                source = Path(dist.locate_file(f))
                if source.is_file():
                    shutil.copy2(source, folder / f.name)
    shutil.copy2(Path(sys.prefix) / 'LICENSE_PYTHON.txt', licenses / 'LICENSE_PYTHON.txt')
    # Preserve license texts of the Conda runtime libraries, including Microsoft's
    # app-local VC runtime. Do not ship package caches or environment metadata.
    for metadata in (Path(sys.prefix) / 'conda-meta').glob('*.json'):
        record = json.loads(metadata.read_text(encoding='utf-8'))
        cached = Path(record.get('extracted_package_dir') or record.get('link', {}).get('source', ''))
        source = cached / 'info/licenses'
        if source.is_dir():
            shutil.copytree(source, licenses / 'runtime' / record['name'])

    source_files = list((source_root / 'backend').glob('*.py'))
    source_files += list((source_root / 'backend/core').glob('*.py'))
    source_files += list((source_root / 'packaging').glob('*.py'))
    source_files += [source_root / 'web' / name for name in ('index.html', 'app.js', 'style.css')]
    source_files += [source_root / 'backend/profiles/profiles.json']
    source_files += [source_root / 'packaging' / name for name in
                     ('video_compression.spec', 'installer.iss', 'version_info.txt', 'build_release.ps1')]
    manifest['application_source_hashes'] = {str(f.relative_to(source_root)).replace('\\', '/'): sha256(f) for f in source_files}
    (args.output / 'release-info.json').write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding='utf-8')
    (licenses / 'THIRD_PARTY_NOTICES.txt').write_text('''Third-party components

FFmpeg 8.0.1 full shared build by Gyan Doshi, GPL v3. Executed as separate programs.
License, upstream build configuration, and the exact FFmpeg source archive are in
ffmpeg/. External library names are listed in ffmpeg/README.txt. The source tree
contains COPYING and LICENSE.md with component license information.
Binary distribution: https://github.com/GyanD/codexffmpeg/releases/tag/8.0.1
Source revision: https://github.com/FFmpeg/FFmpeg/commit/894da5ca7d
Build project and source information: https://www.gyan.dev/ffmpeg/builds/

Python is distributed under the PSF license. See LICENSE_PYTHON.txt.
PyWebView, pythonnet, clr_loader, proxy_tools, bottle, typing_extensions, cffi and
pycparser notices are in their named folders. Additional runtime licenses are in
runtime/. The Microsoft VC runtime is distributed app-local with the executable.

Microsoft Edge WebView2 Runtime is redistributed using Microsoft's unmodified,
signed Evergreen Standalone x64 installer. It is installed only when missing or
older than the application's minimum runtime. Its own license and update terms
apply. Distribution: https://developer.microsoft.com/microsoft-edge/webview2/

The application uses PyInstaller's bootloader; its license permits distributing
the resulting applications under their own terms.
https://pyinstaller.org/en/stable/license.html
''', encoding='utf-8')
    (args.output / '使用说明.txt').write_text('''视频压缩套件 0.7.1 · Windows 64 位安装版

安装后使用桌面或开始菜单中的“视频压缩套件”打开。
拖入视频或点“选择文件”，选择压缩目标和保存位置，再开始处理。
没有支持的显卡时可以使用软件编码；速度取决于电脑性能。

Python、桌面程序依赖、FFmpeg、FFprobe 和 WebView2 离线安装器均已包含。
两个媒体工具共用随包的底层 DLL；请保留完整安装目录。
无需安装开发环境、配置 PATH 或额外下载编码工具。
支持 Intel/AMD x64 Windows 10 2004（19041）及更新版本、Windows 11。
需要 Windows 自带的 .NET Framework 4.8 系统组件。

程序仅在本机处理视频。默认保留源文件；成品保存位置以界面所选目录为准。
个人设置、队列、历史、日志与收件箱位于：
%LOCALAPPDATA%\\VideoCompressionSuite
卸载会保留这些个人数据及视频文件。

第三方组件说明与许可证位于安装目录的 _internal\\licenses；
FFmpeg 的对应版本原始源码也在该目录中。
项目 MIT 许可与开发贡献说明位于 _internal\\licenses\\application。
本项目主要由 OpenAI Codex 与 DeepSeek 完成；第三方组件保留各自许可。
''', encoding='utf-8')
    print(json.dumps({'payload': str(args.output), 'manifest_files': len(manifest['files'])}, ensure_ascii=False))


if __name__ == '__main__':
    main()
