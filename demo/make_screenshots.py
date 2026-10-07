"""为 README 生成真实终端截图（真实命令 + 真实输出，不摆拍）。

用法（仓库根目录）：
    python demo/make_screenshots.py             # 生成 docs/screenshots/*.png

截图内容 = 真实跑一遍 `demo/compare_download.py`（真的下载 4 MiB × 2 并校验 sha256），
所以**这张图会随当天链路速度变化** —— 这正是指南里说的「跑出 1x 就如实说」。

做法：开一个真实控制台窗口，按「回显行 / 命令输出 / 说明标题」分段真实执行；
窗口内的 PowerShell 自己按列宽（中文算 2 列）统计显示行数，把窗口调整到刚好容纳，
然后把标题改成 <title>|R 通知 Python 侧：可以截了。Python 只负责居中、抓图、裁残边。

要求：Windows + Python 3.10+（Pillow）。
"""
from __future__ import annotations

import ctypes
import subprocess
import sys
import time
from ctypes import wintypes
from pathlib import Path

try:
    from PIL import Image, ImageGrab
except ImportError:
    print("需要 Pillow：pip install pillow")
    sys.exit(1)

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "docs" / "screenshots"
COLS = 116
READY_SUFFIX = "|R"

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass


try:
    _dpi = user32.GetDpiForSystem()
except Exception:
    _dpi = 96


class RECT(ctypes.Structure):
    _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                ("right", ctypes.c_long), ("bottom", ctypes.c_long)]


def work_area() -> tuple[int, int]:
    r = RECT()
    user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(r), 0)
    return (r.right - r.left, r.bottom - r.top)


def window_title(hwnd: int) -> str:
    n = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(n + 2)
    user32.GetWindowTextW(hwnd, buf, n + 2)
    return buf.value


def esc(ln: str) -> str:
    return ln.replace('"', '`"')


def build_script(title: str, blocks: list[tuple[str, list[str]]]) -> str:
    """blocks: [(kind, lines)]；kind ∈ echo / note（青色回显）、cmd（真实执行）。

    行数**不用估算**：命令全部真实输出后，读控制台光标位置即实际占用行数；
    设完窗口尺寸再把窗口滚回顶部（否则 conhost 会为保光标可见把顶部顶出去）。
    """
    parts = [
        f"$Host.UI.RawUI.WindowTitle = '{title}'",
        "chcp 65001 > $null",
        "[Console]::OutputEncoding = [Text.Encoding]::UTF8",
        "$OutputEncoding = [Text.Encoding]::UTF8",
        # 等 Python 侧把窗口调大（本机 PS 5.1 的 RawUI.WindowSize 设置会静默失败，
        # 只能由外部按像素改窗口）；窗口够大 → 输出不会被滚掉开头。
        "Start-Sleep -Milliseconds 1500",
    ]

    for kind, lines in blocks:
        if kind in ("echo", "note"):
            for ln in lines:
                parts.append(f'Write-Host "{esc(ln)}" -ForegroundColor Cyan')
        elif kind == "cmd":
            parts.extend(lines)

    parts.append("try { [Console]::CursorVisible = $false } catch {}")
    parts.append(
        "\"rows=$($Host.UI.RawUI.CursorPosition.Y + 1) win=$($Host.UI.RawUI.WindowSize.Height) "
        "top=$($Host.UI.RawUI.WindowPosition.Top) buf=$($Host.UI.RawUI.BufferSize.Height) "
        "cur=$($Host.UI.RawUI.CursorPosition.Y)\" | "
        "Out-File -FilePath (Join-Path $env:TEMP 'patrol_shot_diag.txt') -Encoding UTF8 -Append"
    )
    parts.append(f"$Host.UI.RawUI.WindowTitle = '{title}{READY_SUFFIX}'")
    parts.append("Start-Sleep -Seconds 3")
    parts.append(f"$Host.UI.RawUI.WindowTitle = '{title}'")
    parts.append("Start-Sleep -Seconds 300")
    return "; ".join(parts)


def trim(img: Image.Image, tol: int = 14) -> Image.Image:
    """裁掉右侧与底部残边（保留标题栏与左侧边框）。"""
    w, h = img.size
    px = img.load()
    bg = px[3, 3][:3]

    def is_bg(c) -> bool:
        return all(abs(c[i] - bg[i]) <= tol for i in range(3))

    bottom, right = h, w
    for y in range(h - 1, 0, -1):
        if any(not is_bg(px[x, y][:3]) for x in range(0, w, 4)):
            bottom = min(h, y + 8)
            break
    for x in range(w - 1, 0, -1):
        if any(not is_bg(px[x, y][:3]) for y in range(0, h, 4)):
            right = min(w, x + 8)
            break
    return img.crop((0, 0, right, bottom))


def grab_window(hwnd: int) -> Image.Image:
    """抓窗口自身位图（PrintWindow）；异常时回退屏幕抓取。"""
    rect = RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top

    hdc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hdc)
    bmp = gdi32.CreateCompatibleBitmap(hdc, w, h)
    old = gdi32.SelectObject(mem_dc, bmp)
    user32.PrintWindow(hwnd, mem_dc, 2)

    class BMIH(ctypes.Structure):
        _fields_ = [("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
                    ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
                    ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                    ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
                    ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
                    ("biClrImportant", wintypes.DWORD)]

    bmi = BMIH()
    bmi.biSize = ctypes.sizeof(bmi)
    bmi.biWidth = w
    bmi.biHeight = -h
    bmi.biPlanes = 1
    bmi.biBitCount = 32
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    gdi32.SelectObject(mem_dc, old)
    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hdc)

    img = Image.frombuffer("RGB", (w, h), buf, "raw", "BGRX", 0, 1)
    if not img.getbbox() or max(img.convert("L").getextrema()) < 8:
        img = ImageGrab.grab(bbox=(rect.left, rect.top, rect.right, rect.bottom),
                             all_screens=True)
    return img


def shoot(name: str, title: str, blocks: list[tuple[str, list[str]]],
          timeout_s: float = 300.0) -> Path:
    script = build_script(title, blocks)
    proc = subprocess.Popen(
        ["powershell", "-NoExit", "-NoProfile", "-Command", script],
        cwd=str(ROOT), creationflags=subprocess.CREATE_NEW_CONSOLE,
    )
    try:
        hwnd = None
        for _ in range(100):
            time.sleep(0.2)
            hwnd = user32.FindWindowW(None, title)
            if hwnd:
                break
        if not hwnd:
            raise RuntimeError(f"未找到窗口：{title}")

        # 按像素把窗口开够大（本机 PS 5.1 的 RawUI.WindowSize 设置会静默失败，
        # 只能从外部改）。字宽/行高按默认窗口度量 + DPI 比例估算；
        # 多出来的空白靠截图后的 trim 裁掉，所以宁大勿小。
        wa_w, wa_h = work_area()
        scale = max(1.0, _dpi / 192.0)
        char_w, line_h = 13.3 * scale, 38.3 * scale
        width = min(int(COLS * char_w) + int(18 * scale), wa_w - 80)
        height = min(int(56 * line_h) + int(46 * scale), wa_h - 80)
        user32.MoveWindow(hwnd, 60, 40, width, height, True)

        deadline = time.time() + timeout_s
        while time.time() < deadline:
            time.sleep(0.4)
            if window_title(hwnd).endswith(READY_SUFFIX):
                break
        else:
            raise RuntimeError(f"未收到就绪信号：{title}")

        time.sleep(3.2)          # 等标题恢复 + 控制台重排稳定
        rect = RECT()
        user32.GetWindowRect(hwnd, ctypes.byref(rect))
        w, h = rect.right - rect.left, rect.bottom - rect.top
        user32.MoveWindow(hwnd, max(0, (wa_w - w) // 2), max(0, (wa_h - h) // 3),
                          w, h, True)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002)   # TOPMOST
        user32.SetForegroundWindow(hwnd)
        time.sleep(0.8)

        img = trim(grab_window(hwnd))
        dest = OUT / name
        img.save(dest)
        print(f"[ok] {dest.name}  {img.size[0]}x{img.size[1]}")
        return dest
    finally:
        subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
        time.sleep(0.5)


def latest_report() -> str:
    d = ROOT / "_run" / "reports"
    files = sorted(d.glob("*.md"), key=lambda p: p.stat().st_mtime, reverse=True)
    return str(files[0]) if files else ""


def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    shots = []

    # 主图：同字节数对比（普通下载 vs fastdl 8 线程），真实下载 + sha256 一致性核对。
    # 这一步会真的下载 4 MiB × 2（秒级到几分钟，取决于链路），所以超时给足；
    # 输出取尾部 30 行（[5/5] + 结果表 + 正确性核对 + 解读）——完整输出比一屏高，
    # 截一屏会丢掉开头几行，取尾部才是一屏放得下的完整段落。
    shots.append(shoot(
        "fastdl-compare.png", "fastdl-compare",
        [
            ("echo", ["python demo/compare_download.py --no-tls-verify --limit-mb 4"
                      " | Select-Object -Last 30    # 真实下载对比（尾部 30 行）"]),
            ("cmd", ["python demo/compare_download.py --no-tls-verify --limit-mb 4"
                     " | Select-Object -Last 30"]),
        ],
        timeout_s=900.0,
    ))

    print("\n完成：")
    for p in shots:
        print("  ", p)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
