"""Test the native-dialog plumbing without showing any dialog.

The dialog itself must be clicked by a human, but everything around it -- the
PowerShell script, the JSON protocol, the escaping of paths containing quotes or
non-ASCII characters -- can be verified automatically. That is what this does.

Run:  D:\\anaconda3\\python.exe tests\\test_winapi.py
"""

from __future__ import annotations

import io
import sys
from pathlib import Path

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "backend"))

from core import winapi  # noqa: E402

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append(ok)
    print("  [{}] {}{}".format(PASS if ok else FAIL, name, " -- " + detail if detail else ""))


def main() -> int:
    print("\nwinapi self-test (no dialog is shown)\n")

    check("platform supported", winapi.available(),
          "powershell: {}".format(winapi._find_powershell()))
    check("video filter covers the common formats",
          all(ext in winapi.VIDEO_FILTER for ext in ("*.mp4", "*.mkv", "*.rmvb", "*.m2ts")))
    check("quote escaping doubles single quotes",
          winapi._ps("E:\\a'b") == "'E:\\a''b'", winapi._ps("E:\\a'b"))
    check("non-ASCII survives escaping",
          winapi._ps("E:\\新建文件夹") == "'E:\\新建文件夹'")

    # The file dialog, configured exactly as pick_files() does, but not shown.
    script = "\n".join([
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null",
        "$d = New-Object System.Windows.Forms.OpenFileDialog",
        "$d.Multiselect = $true",
        "$d.Title = " + winapi._ps("选择要压缩的视频") + "",
        "$d.Filter = " + winapi._ps(winapi.VIDEO_FILTER) + "",
        "$d.InitialDirectory = " + winapi._ps("E:\\新建文件夹") + "",
        "[ordered]@{ multi = $d.Multiselect; title = $d.Title;",
        "  filterOk = ($d.Filter -like '*mp4*mkv*');",
        "  initOk = ($d.InitialDirectory -eq 'E:\\新建文件夹');",
        "  names = @('E:\\新建文件夹\\a.mp4') } | ConvertTo-Json -Compress -Depth 4",
    ])
    result = winapi._run_powershell(script)
    check("OpenFileDialog constructs from the generated script", bool(result), str(result))
    if result:
        check("multi-select enabled", result.get("multi") is True)
        check("filter applied", result.get("filterOk") is True)
        check("non-ASCII initial directory survives the round trip",
              result.get("initOk") is True, str(result.get("initOk")))

    # The folder dialog, configured exactly as pick_folder() does.
    folder_script = "\n".join([
        "Add-Type -AssemblyName System.Windows.Forms | Out-Null",
        "$d = New-Object System.Windows.Forms.FolderBrowserDialog",
        "$d.Description = " + winapi._ps("选择要加入的文件夹") + "",
        "$d.ShowNewFolderButton = $false",
        "$d.SelectedPath = " + winapi._ps("E:\\新建文件夹\\001") + "",
        "[ordered]@{ newBtn = $d.ShowNewFolderButton;",
        "  descOk = ($d.Description -eq '选择要加入的文件夹');",
        "  pathOk = ($d.SelectedPath -eq 'E:\\新建文件夹\\001') } | ConvertTo-Json -Compress",
    ])
    folder = winapi._run_powershell(folder_script)
    check("FolderBrowserDialog constructs from the generated script", bool(folder), str(folder))
    if folder:
        check("new-folder button disabled", folder.get("newBtn") is False)
        check("non-ASCII folder path survives", folder.get("pathOk") is True)

    # A script that produces no JSON must return None rather than raise.
    check("script without JSON returns None safely",
          winapi._run_powershell("Write-Output 'no json here'") is None)

    passed = sum(1 for r in results if r)
    print("\n  {}/{} checks passed\n".format(passed, len(results)))
    return 0 if passed == len(results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
