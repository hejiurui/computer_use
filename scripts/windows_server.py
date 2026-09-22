"""
Computer Use (Windows) — MCP server for desktop automation.

结构参考 ezpzai/codex-computer-use-windows（MIT）与 CursorTouch/Windows-MCP（MIT）。
工具命名与 Codex / Windows-MCP 电脑控制面对齐。
仅供在已授权机器上使用。
"""
from __future__ import annotations

import atexit
import base64
import ctypes
import ctypes.wintypes
import io
import logging
import platform
import subprocess
import sys
import time
import urllib.parse
from dataclasses import dataclass
from typing import Any, Literal

from mcp.server.fastmcp import FastMCP, Image
from mss import mss
from PIL import Image as PILImage

logger = logging.getLogger("computer-use-windows")


def _enable_dpi_awareness() -> None:
    if platform.system() != "Windows":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return
    except Exception:
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


_enable_dpi_awareness()

import pyautogui

pyautogui.FAILSAFE = True
pyautogui.PAUSE = 0.03

mcp = FastMCP("Computer Use (Windows)", json_response=True)

DEFAULT_MAX_WIDTH = 1600
DEFAULT_MAX_HEIGHT = 900
DEFAULT_JPEG_QUALITY = 80


@dataclass
class CaptureInfo:
    origin_x: int
    origin_y: int
    actual_width: int
    actual_height: int
    rendered_width: int
    rendered_height: int


_MSS: object | None = None
_MSS_CREATED_AT: float = 0.0
_MSS_MAX_AGE: float = 60.0
_LAST_CAPTURE: CaptureInfo | None = None


def _ensure_windows() -> None:
    if platform.system() != "Windows":
        raise RuntimeError("computer-use-windows only runs on Windows")


def _get_mss():
    global _MSS, _MSS_CREATED_AT
    now = time.monotonic()
    if _MSS is not None and (now - _MSS_CREATED_AT) > _MSS_MAX_AGE:
        try:
            _MSS.close()
        except Exception:
            pass
        _MSS = None
    if _MSS is None:
        _MSS = mss()
        _MSS_CREATED_AT = now
    return _MSS


def _close_mss() -> None:
    global _MSS
    if _MSS is not None:
        _MSS.close()
        _MSS = None


atexit.register(_close_mss)


def _encode_image(image: PILImage.Image, image_format: Literal["png", "jpeg"], quality: int) -> Image:
    buffer = io.BytesIO()
    if image_format == "jpeg":
        image.convert("RGB").save(
            buffer, format="JPEG", quality=max(30, min(int(quality), 95)), optimize=True
        )
        return Image(data=buffer.getvalue(), format="jpeg")
    image.save(buffer, format="PNG", compress_level=1)
    return Image(data=buffer.getvalue(), format="png")


def _capture_image(
    x: int | None, y: int | None, width: int | None, height: int | None
) -> tuple[PILImage.Image, CaptureInfo]:
    if any(value is None for value in (x, y, width, height)):
        monitor = _get_mss().monitors[1]
        shot = _get_mss().grab(monitor)
        image = PILImage.frombytes("RGB", shot.size, shot.rgb)
        return image, CaptureInfo(
            origin_x=monitor["left"],
            origin_y=monitor["top"],
            actual_width=monitor["width"],
            actual_height=monitor["height"],
            rendered_width=image.width,
            rendered_height=image.height,
        )
    actual_x, actual_y = int(x), int(y)
    actual_width, actual_height = max(int(width), 1), max(int(height), 1)
    shot = _get_mss().grab(
        {"left": actual_x, "top": actual_y, "width": actual_width, "height": actual_height}
    )
    image = PILImage.frombytes("RGB", shot.size, shot.rgb)
    return image, CaptureInfo(
        origin_x=actual_x,
        origin_y=actual_y,
        actual_width=actual_width,
        actual_height=actual_height,
        rendered_width=image.width,
        rendered_height=image.height,
    )


def _resize_image(image: PILImage.Image, max_width: int | None, max_height: int | None) -> PILImage.Image:
    if max_width is None and max_height is None:
        return image
    bounded_width = max_width if max_width is not None else image.width
    bounded_height = max_height if max_height is not None else image.height
    if image.width <= bounded_width and image.height <= bounded_height:
        return image
    resized = image.copy()
    resized.thumbnail((bounded_width, bounded_height), PILImage.Resampling.LANCZOS)
    return resized


def _resolve_point(
    x: int, y: int, coordinate_space: Literal["auto", "screen", "last_capture"]
) -> tuple[int, int, str]:
    global _LAST_CAPTURE
    if coordinate_space == "screen" or _LAST_CAPTURE is None:
        return int(x), int(y), "screen"
    info = _LAST_CAPTURE
    use_last_capture = coordinate_space == "last_capture"
    if coordinate_space == "auto":
        in_rendered = 0 <= x <= info.rendered_width and 0 <= y <= info.rendered_height
        screen_monitor = _get_mss().monitors[1]
        in_screen = 0 <= x <= screen_monitor["width"] and 0 <= y <= screen_monitor["height"]
        if in_rendered and (not in_screen or info.rendered_width < screen_monitor["width"]):
            use_last_capture = True
        else:
            use_last_capture = False
    if not use_last_capture or info.rendered_width <= 0 or info.rendered_height <= 0:
        return int(x), int(y), "screen"
    scaled_x = info.origin_x + round(int(x) * info.actual_width / info.rendered_width)
    scaled_y = info.origin_y + round(int(y) * info.actual_height / info.rendered_height)
    scaled_x = min(info.origin_x + info.actual_width - 1, max(info.origin_x, scaled_x))
    scaled_y = min(info.origin_y + info.actual_height - 1, max(info.origin_y, scaled_y))
    return scaled_x, scaled_y, "last_capture"


def _get_foreground_hwnd() -> int:
    return ctypes.windll.user32.GetForegroundWindow()


def _get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    rect = ctypes.wintypes.RECT()
    ctypes.windll.user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return rect.left, rect.top, rect.right, rect.bottom


def _get_window_title(hwnd: int) -> str:
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    if length == 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def _setup_clipboard_ctypes() -> None:
    import ctypes as _ct

    _k32 = _ct.windll.kernel32
    _u32 = _ct.windll.user32
    _k32.GlobalAlloc.argtypes = [_ct.wintypes.UINT, _ct.c_size_t]
    _k32.GlobalAlloc.restype = _ct.c_void_p
    _k32.GlobalLock.argtypes = [_ct.c_void_p]
    _k32.GlobalLock.restype = _ct.c_void_p
    _k32.GlobalUnlock.argtypes = [_ct.c_void_p]
    _k32.GlobalUnlock.restype = _ct.wintypes.BOOL
    _u32.OpenClipboard.argtypes = [_ct.wintypes.HWND]
    _u32.OpenClipboard.restype = _ct.wintypes.BOOL
    _u32.CloseClipboard.argtypes = []
    _u32.CloseClipboard.restype = _ct.wintypes.BOOL
    _u32.EmptyClipboard.argtypes = []
    _u32.EmptyClipboard.restype = _ct.wintypes.BOOL
    _u32.SetClipboardData.argtypes = [_ct.wintypes.UINT, _ct.c_void_p]
    _u32.SetClipboardData.restype = _ct.c_void_p
    _u32.GetClipboardData.argtypes = [_ct.wintypes.UINT]
    _u32.GetClipboardData.restype = _ct.c_void_p


_setup_clipboard_ctypes()


def _clipboard_set_unicode(text: str) -> None:
    CF_UNICODETEXT = 13
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    encoded = text.encode("utf-16-le")
    buf_size = len(encoded) + 2
    h = kernel32.GlobalAlloc(0x0002, buf_size)
    if not h:
        raise RuntimeError("GlobalAlloc failed")
    ptr = kernel32.GlobalLock(h)
    if not ptr:
        raise RuntimeError("GlobalLock failed")
    ctypes.memmove(ptr, encoded, len(encoded))
    ctypes.memset(ptr + len(encoded), 0, 2)
    kernel32.GlobalUnlock(h)
    if not user32.OpenClipboard(None):
        raise RuntimeError("OpenClipboard failed")
    try:
        user32.EmptyClipboard()
        if not user32.SetClipboardData(CF_UNICODETEXT, h):
            raise RuntimeError("SetClipboardData failed")
    finally:
        user32.CloseClipboard()


def _clipboard_get_unicode() -> str:
    CF_UNICODETEXT = 13
    kernel32 = ctypes.windll.kernel32
    user32 = ctypes.windll.user32
    if not user32.OpenClipboard(None):
        return ""
    try:
        h = user32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return ""
        ptr = kernel32.GlobalLock(h)
        if not ptr:
            return ""
        text = ctypes.wstring_at(ptr)
        kernel32.GlobalUnlock(h)
        return text
    finally:
        user32.CloseClipboard()


_UIA_AVAILABLE = False
try:
    import uiautomation as uia

    _UIA_AVAILABLE = True
except ImportError:
    uia = None  # type: ignore[assignment]


def _walk_ui_tree(control: Any, depth: int, max_children: int = 20) -> dict:
    info: dict[str, Any] = {
        "name": control.Name or "",
        "type": control.ControlTypeName,
        "rect": {
            "left": control.BoundingRectangle.left,
            "top": control.BoundingRectangle.top,
            "right": control.BoundingRectangle.right,
            "bottom": control.BoundingRectangle.bottom,
        },
    }
    if depth <= 0:
        return info
    children = []
    child = control.GetFirstChildControl()
    count = 0
    while child and count < max_children:
        children.append(_walk_ui_tree(child, depth - 1, max_children))
        child = child.GetNextSiblingControl()
        count += 1
    if children:
        info["children"] = children
    return info


def _find_control_recursive(control: Any, name: str, control_type: str = "") -> Any:
    name_lower = name.lower()
    child = control.GetFirstChildControl()
    while child:
        child_name = (child.Name or "").lower()
        if name_lower in child_name:
            if not control_type or child.ControlTypeName == control_type:
                return child
        found = _find_control_recursive(child, name, control_type)
        if found:
            return found
        child = child.GetNextSiblingControl()
    return None


def _collect_text_recursive(control: Any, texts: list, max_items: int = 200) -> None:
    if len(texts) >= max_items:
        return
    name = control.Name or ""
    if name.strip():
        texts.append(name.strip())
    try:
        vp = control.GetValuePattern()
        if vp and vp.Value:
            texts.append(vp.Value)
    except Exception:
        pass
    child = control.GetFirstChildControl()
    while child and len(texts) < max_items:
        _collect_text_recursive(child, texts, max_items)
        child = child.GetNextSiblingControl()


def _ocr_image(image: PILImage.Image) -> str:
    buf = io.BytesIO()
    image.save(buf, format="BMP")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    ps_script = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$bmpBytes = New-Object System.IO.MemoryStream(,[System.Convert]::FromBase64String($input))
[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime] | Out-Null
$bmpStream = [Windows.Storage.Streams.InMemoryRandomAccessStream]::new()
$writer = [Windows.Storage.Streams.DataWriter]::new($bmpStream)
$writer.WriteBytes($bmpBytes.ToArray())
$writer.StoreAsync().GetResults() | Out-Null
$writer.FlushAsync().GetResults() | Out-Null
$bmpStream.Seek(0)
$task = [Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($bmpStream)
while (-not $task.Status) { Start-Sleep -Milliseconds 10 }
$decoder = $task.GetResults()
$task2 = $decoder.GetSoftwareBitmapAsync()
while (-not $task2.Status) { Start-Sleep -Milliseconds 10 }
$bitmap = $task2.GetResults()
$ocrEngine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
$task3 = $ocrEngine.RecognizeAsync($bitmap)
while (-not $task3.Status) { Start-Sleep -Milliseconds 10 }
$result = $task3.GetResults()
$result.Text
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            input=b64,
            capture_output=True,
            text=True,
            timeout=20,
        )
        return proc.stdout.strip()
    except Exception as exc:
        logger.warning("OCR failed: %s", exc)
        return ""


# ---------------------------------------------------------------------------
# Screen info
# ---------------------------------------------------------------------------


@mcp.tool()
def get_screen_size() -> dict:
    """Return the current primary screen size in pixels."""
    _ensure_windows()
    monitor = _get_mss().monitors[1]
    return {"width": monitor["width"], "height": monitor["height"]}


@mcp.tool()
def get_cursor_position() -> dict:
    """Return the current mouse cursor position."""
    _ensure_windows()
    x, y = pyautogui.position()
    return {"x": x, "y": y}


@mcp.tool()
def get_last_capture_info() -> dict:
    """Return metadata for the most recent screenshot used for auto coordinate mapping."""
    _ensure_windows()
    if _LAST_CAPTURE is None:
        return {"available": False}
    return {
        "available": True,
        "origin_x": _LAST_CAPTURE.origin_x,
        "origin_y": _LAST_CAPTURE.origin_y,
        "actual_width": _LAST_CAPTURE.actual_width,
        "actual_height": _LAST_CAPTURE.actual_height,
        "rendered_width": _LAST_CAPTURE.rendered_width,
        "rendered_height": _LAST_CAPTURE.rendered_height,
    }


# ---------------------------------------------------------------------------
# Screenshot
# ---------------------------------------------------------------------------


@mcp.tool()
def screenshot(
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    max_width: int | None = None,
    max_height: int | None = None,
    format: Literal["auto", "png", "jpeg"] = "auto",
    quality: int = DEFAULT_JPEG_QUALITY,
) -> Image:
    """Capture the full screen or a region. Full-screen captures default to a downscaled JPEG."""
    _ensure_windows()
    global _LAST_CAPTURE
    if any(v is None for v in (x, y, width, height)) and any(v is not None for v in (x, y, width, height)):
        raise ValueError("x, y, width and height must be provided together")
    image, info = _capture_image(x, y, width, height)
    effective_max_width = max_width
    effective_max_height = max_height
    if x is None and y is None and width is None and height is None:
        effective_max_width = effective_max_width or DEFAULT_MAX_WIDTH
        effective_max_height = effective_max_height or DEFAULT_MAX_HEIGHT
    resized = _resize_image(image, effective_max_width, effective_max_height)
    info.rendered_width = resized.width
    info.rendered_height = resized.height
    _LAST_CAPTURE = info
    if format == "auto":
        image_format: Literal["png", "jpeg"] = (
            "jpeg" if (resized.size != image.size or x is None) else "png"
        )
    else:
        image_format = format
    return _encode_image(resized, image_format, quality)


@mcp.tool()
def screenshot_active_window(
    max_width: int | None = None,
    max_height: int | None = None,
    format: Literal["auto", "png", "jpeg"] = "auto",
    quality: int = DEFAULT_JPEG_QUALITY,
) -> Image:
    """Capture only the currently focused window (smaller than full-screen)."""
    _ensure_windows()
    hwnd = _get_foreground_hwnd()
    if not hwnd:
        return screenshot(max_width=max_width, max_height=max_height, format=format, quality=quality)
    left, top, right, bottom = _get_window_rect(hwnd)
    return screenshot(
        x=left,
        y=top,
        width=max(right - left, 1),
        height=max(bottom - top, 1),
        max_width=max_width,
        max_height=max_height,
        format=format,
        quality=quality,
    )


# ---------------------------------------------------------------------------
# Mouse
# ---------------------------------------------------------------------------


@mcp.tool()
def move_mouse(
    x: int,
    y: int,
    duration: float = 0.0,
    coordinate_space: Literal["auto", "screen", "last_capture"] = "auto",
) -> dict:
    """Move the mouse pointer to screen coordinates or auto-map from the latest screenshot."""
    _ensure_windows()
    rx, ry, space = _resolve_point(x, y, coordinate_space)
    pyautogui.moveTo(rx, ry, duration=max(duration, 0.0))
    return {"ok": True, "x": rx, "y": ry, "input_x": x, "input_y": y, "coordinate_space": space}


@mcp.tool()
def click(
    x: int | None = None,
    y: int | None = None,
    button: Literal["left", "middle", "right"] = "left",
    clicks: int = 1,
    interval: float = 0.15,
    coordinate_space: Literal["auto", "screen", "last_capture"] = "auto",
) -> dict:
    """Click using screen coordinates or auto-map from the latest screenshot."""
    _ensure_windows()
    rx, ry, space = x, y, "screen"
    if x is not None and y is not None:
        rx, ry, space = _resolve_point(x, y, coordinate_space)
    pyautogui.click(x=rx, y=ry, clicks=max(clicks, 1), interval=max(interval, 0.0), button=button)
    return {
        "ok": True,
        "button": button,
        "clicks": max(clicks, 1),
        "x": rx,
        "y": ry,
        "input_x": x,
        "input_y": y,
        "coordinate_space": space,
    }


@mcp.tool()
def drag_mouse(
    x: int,
    y: int,
    duration: float = 0.2,
    button: Literal["left", "middle", "right"] = "left",
    coordinate_space: Literal["auto", "screen", "last_capture"] = "auto",
) -> dict:
    """Drag from current position to target coordinates."""
    _ensure_windows()
    rx, ry, space = _resolve_point(x, y, coordinate_space)
    pyautogui.dragTo(rx, ry, duration=max(duration, 0.0), button=button)
    return {
        "ok": True,
        "x": rx,
        "y": ry,
        "input_x": x,
        "input_y": y,
        "button": button,
        "coordinate_space": space,
    }


# ---------------------------------------------------------------------------
# Keyboard
# ---------------------------------------------------------------------------


@mcp.tool()
def type_text(text: str, interval: float = 0.0) -> dict:
    """Type ASCII text into the active window. For CJK/emoji use type_unicode."""
    _ensure_windows()
    pyautogui.write(text, interval=max(interval, 0.0))
    return {"ok": True, "characters": len(text)}


@mcp.tool()
def type_unicode(text: str) -> dict:
    """Type Unicode text (Chinese, Japanese, emoji, ...) via clipboard paste (Ctrl+V)."""
    _ensure_windows()
    _clipboard_set_unicode(text)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.05)
    return {"ok": True, "characters": len(text)}


@mcp.tool()
def press_key(key: str, presses: int = 1, interval: float = 0.05) -> dict:
    """Press a single key one or more times (enter, tab, escape, backspace, ...)."""
    _ensure_windows()
    pyautogui.press(key, presses=max(presses, 1), interval=max(interval, 0.0))
    return {"ok": True, "key": key, "presses": max(presses, 1)}


@mcp.tool()
def hotkey(keys: list[str], interval: float = 0.05) -> dict:
    """Press a key chord such as ['ctrl', 'c'] or ['alt', 'tab']."""
    _ensure_windows()
    if not keys:
        raise ValueError("keys must not be empty")
    pyautogui.hotkey(*keys, interval=max(interval, 0.0))
    return {"ok": True, "keys": keys}


@mcp.tool()
def scroll(clicks: int) -> dict:
    """Scroll vertically. Positive values scroll up, negative values scroll down."""
    _ensure_windows()
    pyautogui.scroll(clicks)
    return {"ok": True, "clicks": clicks}


@mcp.tool()
def wait(seconds: float) -> dict:
    """Pause briefly so the UI can update before the next screenshot."""
    _ensure_windows()
    pyautogui.sleep(max(seconds, 0.0))
    return {"ok": True, "seconds": max(seconds, 0.0)}


# ---------------------------------------------------------------------------
# Clipboard
# ---------------------------------------------------------------------------


@mcp.tool()
def get_clipboard() -> dict:
    """Return the current clipboard text content."""
    _ensure_windows()
    return {"ok": True, "text": _clipboard_get_unicode()}


@mcp.tool()
def set_clipboard(text: str) -> dict:
    """Set text content to the clipboard."""
    _ensure_windows()
    _clipboard_set_unicode(text)
    return {"ok": True, "characters": len(text)}


# ---------------------------------------------------------------------------
# Window management
# ---------------------------------------------------------------------------


@mcp.tool()
def list_windows() -> dict:
    """List all visible windows with their titles, positions and sizes."""
    _ensure_windows()
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.POINTER(ctypes.c_int))
    windows: list[dict] = []

    def _callback(hwnd: int, _lParam: Any) -> bool:
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        title = _get_window_title(hwnd)
        if not title:
            return True
        left, top, right, bottom = _get_window_rect(hwnd)
        windows.append(
            {
                "hwnd": hwnd,
                "title": title,
                "x": left,
                "y": top,
                "width": right - left,
                "height": bottom - top,
            }
        )
        return True

    ctypes.windll.user32.EnumWindows(EnumWindowsProc(_callback), 0)
    return {"ok": True, "windows": windows}


@mcp.tool()
def focus_window(title: str) -> dict:
    """Bring a window to the foreground by (partial) title match. Case-insensitive."""
    _ensure_windows()
    title_lower = title.lower()
    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.POINTER(ctypes.c_int))
    found_hwnd = 0

    def _callback(hwnd: int, _lParam: Any) -> bool:
        nonlocal found_hwnd
        if not ctypes.windll.user32.IsWindowVisible(hwnd):
            return True
        wtitle = _get_window_title(hwnd)
        if title_lower in wtitle.lower():
            found_hwnd = hwnd
            return False
        return True

    ctypes.windll.user32.EnumWindows(EnumWindowsProc(_callback), 0)
    if not found_hwnd:
        return {"ok": False, "error": f"No window matching '{title}' found"}

    user32 = ctypes.windll.user32
    if user32.IsIconic(found_hwnd):
        user32.ShowWindow(found_hwnd, 9)
    user32.SetForegroundWindow(found_hwnd)
    time.sleep(0.1)
    actual_fg = user32.GetForegroundWindow()
    if actual_fg != found_hwnd:
        cur_thread = user32.GetWindowThreadProcessId(actual_fg, None)
        target_thread = user32.GetWindowThreadProcessId(found_hwnd, None)
        attached = False
        if cur_thread != target_thread:
            attached = bool(user32.AttachThreadInput(cur_thread, target_thread, True))
        try:
            user32.BringWindowToTop(found_hwnd)
            user32.ShowWindow(found_hwnd, 5)
            user32.SetForegroundWindow(found_hwnd)
        finally:
            if attached:
                user32.AttachThreadInput(cur_thread, target_thread, False)
        time.sleep(0.15)
        actual_fg = user32.GetForegroundWindow()
        if actual_fg != found_hwnd:
            return {
                "ok": False,
                "error": f"Failed to focus window (hwnd={found_hwnd}). Foreground: {_get_window_title(actual_fg)}",
                "hwnd": found_hwnd,
                "title": _get_window_title(found_hwnd),
            }
    return {"ok": True, "hwnd": found_hwnd, "title": _get_window_title(found_hwnd)}


@mcp.tool()
def run_program(command: str, wait_for_exit: bool = False, timeout: float = 10.0) -> dict:
    """Launch a program or command. If wait_for_exit is True, wait and return stdout."""
    _ensure_windows()
    if wait_for_exit:
        try:
            proc = subprocess.run(
                command, shell=True, capture_output=True, text=True, timeout=max(timeout, 1.0)
            )
            return {
                "ok": True,
                "returncode": proc.returncode,
                "stdout": proc.stdout[:4000],
                "stderr": proc.stderr[:2000],
            }
        except subprocess.TimeoutExpired:
            return {"ok": False, "error": f"Command timed out after {timeout}s"}
    subprocess.Popen(command, shell=True)
    time.sleep(0.3)
    return {"ok": True, "started": command}


@mcp.tool()
def open_app(name: str) -> dict:
    """Open a Windows app by common name (notepad, calculator, explorer, cmd, chrome, edge, paint, settings, ...)."""
    _ensure_windows()
    app_map = {
        "notepad": "notepad.exe",
        "calculator": "calc.exe",
        "calc": "calc.exe",
        "explorer": "explorer.exe",
        "cmd": "cmd.exe",
        "powershell": "powershell.exe",
        "terminal": "wt.exe",
        "settings": "ms-settings:",
        "paint": "mspaint.exe",
        "chrome": "chrome.exe",
        "edge": "msedge.exe",
    }
    name_lower = name.lower().strip()
    cmd = app_map.get(name_lower)
    if cmd:
        try:
            subprocess.Popen(cmd, shell=True)
            time.sleep(0.5)
            return {"ok": True, "app": name_lower, "command": cmd}
        except Exception as exc:
            return {"ok": False, "error": str(exc)}
    pyautogui.hotkey("win")
    time.sleep(0.5)
    _clipboard_set_unicode(name)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.8)
    pyautogui.press("enter")
    time.sleep(0.5)
    return {"ok": True, "app": name, "method": "start_search"}


# ---------------------------------------------------------------------------
# UI Automation
# ---------------------------------------------------------------------------


@mcp.tool()
def get_ui_tree(depth: int = 3, max_children: int = 20) -> dict:
    """Return the accessibility UI tree of the foreground window (low-token alternative to screenshots)."""
    _ensure_windows()
    if not _UIA_AVAILABLE:
        return {"ok": False, "error": "uiautomation package not installed"}
    try:
        root = uia.GetForegroundControl()
        if root is None:
            return {"ok": False, "error": "No foreground window found"}
        tree = _walk_ui_tree(root, max(depth, 1), max(max_children, 1))
        tree["window_title"] = root.Name or ""
        return {"ok": True, "tree": tree}
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def find_and_click_element(
    name: str,
    control_type: str = "",
    click_button: Literal["left", "right"] = "left",
) -> dict:
    """Find a UI element by name (optional ControlType) in the foreground window and click it. No screenshot needed."""
    _ensure_windows()
    if not _UIA_AVAILABLE:
        return {"ok": False, "error": "uiautomation package not installed"}
    try:
        root = uia.GetForegroundControl()
        target = _find_control_recursive(root, name, control_type)
        if target is None:
            return {"ok": False, "error": f"Element '{name}' not found"}
        rect = target.BoundingRectangle
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        pyautogui.click(cx, cy, button=click_button)
        return {
            "ok": True,
            "name": target.Name,
            "type": target.ControlTypeName,
            "clicked_x": cx,
            "clicked_y": cy,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def get_window_text(title: str) -> dict:
    """Get all visible text from a window using UI Automation (no screenshot needed)."""
    _ensure_windows()
    focus_result = focus_window(title)
    if not focus_result.get("ok"):
        return focus_result
    time.sleep(0.15)
    if _UIA_AVAILABLE:
        try:
            root = uia.GetForegroundControl()
            if root:
                texts: list[str] = []
                _collect_text_recursive(root, texts, max_items=200)
                combined = "\n".join(texts)
                return {
                    "ok": True,
                    "window_title": root.Name or "",
                    "text": combined[:8000],
                    "truncated": len(combined) > 8000,
                    "source": "uia",
                }
        except Exception as exc:
            logger.debug("UIA text extraction failed: %s", exc)
    ocr_result = extract_text_active_window()
    ocr_result["source"] = "ocr"
    return ocr_result


@mcp.tool()
def send_text_to_window(title: str, text: str, paste: bool = True) -> dict:
    """Focus a window and send text to its focused control (clipboard paste by default)."""
    _ensure_windows()
    focus_result = focus_window(title)
    if not focus_result.get("ok"):
        return focus_result
    time.sleep(0.15)
    if paste:
        _clipboard_set_unicode(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.05)
        return {"ok": True, "method": "clipboard_paste", "characters": len(text)}
    hwnd = ctypes.windll.user32.GetFocus() or ctypes.windll.user32.GetForegroundWindow()
    WM_SETTEXT = 0x000C
    buf = ctypes.create_unicode_buffer(text)
    ctypes.windll.user32.SendMessageW(hwnd, WM_SETTEXT, 0, buf)
    return {"ok": True, "method": "wm_settext", "hwnd": hwnd, "characters": len(text)}


@mcp.tool()
def send_keys_to_window(title: str, text: str, send_enter: bool = False) -> dict:
    """Focus a window, paste Unicode text, and optionally press Enter (chat apps)."""
    _ensure_windows()
    focus_result = focus_window(title)
    if not focus_result.get("ok"):
        return focus_result
    time.sleep(0.15)
    _clipboard_set_unicode(text)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.05)
    if send_enter:
        time.sleep(0.05)
        pyautogui.press("enter")
    return {
        "ok": True,
        "characters": len(text),
        "enter_sent": send_enter,
        "window": focus_result.get("title", ""),
    }


# ---------------------------------------------------------------------------
# OCR
# ---------------------------------------------------------------------------


@mcp.tool()
def extract_text(
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
) -> dict:
    """Extract text from the screen or a region using Windows built-in OCR (no screenshot image)."""
    _ensure_windows()
    image, _info = _capture_image(x, y, width, height)
    text = _ocr_image(image)
    if not text:
        return {"ok": False, "error": "OCR returned no text"}
    return {"ok": True, "text": text}


@mcp.tool()
def extract_text_active_window() -> dict:
    """Extract text from the currently focused window using OCR."""
    _ensure_windows()
    hwnd = _get_foreground_hwnd()
    if not hwnd:
        return extract_text()
    left, top, right, bottom = _get_window_rect(hwnd)
    return extract_text(x=left, y=top, width=max(right - left, 1), height=max(bottom - top, 1))


# ---------------------------------------------------------------------------
# Batch / Observe
# ---------------------------------------------------------------------------


@mcp.tool()
def batch_actions(actions: list[dict]) -> dict:
    """Execute multiple actions in sequence in a single call.
    Each action: {"tool": "<name>", "args": {...}}. Max 20. Stops on first error."""
    _ensure_windows()
    if not actions:
        return {"ok": False, "error": "actions list is empty"}
    if len(actions) > 20:
        return {"ok": False, "error": "Maximum 20 actions per batch"}
    tool_map = {
        "click": click,
        "move_mouse": move_mouse,
        "drag_mouse": drag_mouse,
        "type_text": type_text,
        "type_unicode": type_unicode,
        "press_key": press_key,
        "hotkey": hotkey,
        "scroll": scroll,
        "wait": wait,
        "focus_window": focus_window,
    }
    results: list[dict] = []
    for i, action in enumerate(actions):
        tool_name = action.get("tool", "")
        args = action.get("args", {})
        if tool_name not in tool_map:
            results.append({"index": i, "ok": False, "error": f"Unknown tool: {tool_name}"})
            return {"ok": False, "results": results, "stopped_at": i}
        try:
            result = tool_map[tool_name](**args)
            results.append({"index": i, **result})
        except Exception as exc:
            results.append({"index": i, "ok": False, "error": str(exc)})
            return {"ok": False, "results": results, "stopped_at": i}
    return {"ok": True, "results": results}


@mcp.tool()
def observe_screen(
    include_screenshot: bool = True,
    include_ui_tree: bool = True,
    include_ocr: bool = False,
    ui_depth: int = 2,
    max_width: int | None = None,
    max_height: int | None = None,
    quality: int = DEFAULT_JPEG_QUALITY,
    expected_window: str = "",
) -> dict:
    """All-in-one observation: screenshot + UI tree + optional OCR in one call.
    Use include_ui_tree=true and include_screenshot=false to save tokens."""
    _ensure_windows()
    result: dict[str, Any] = {"ok": True}
    hwnd = _get_foreground_hwnd()
    active_title = _get_window_title(hwnd) if hwnd else ""
    result["active_window"] = active_title
    result["active_hwnd"] = hwnd
    result["cursor"] = get_cursor_position()
    window_mismatch = False
    if expected_window and expected_window.lower() not in active_title.lower():
        window_mismatch = True
        result["warning"] = (
            f"Expected window '{expected_window}' but foreground is '{active_title}'. "
            "Falling back to full-screen capture."
        )
    if include_ui_tree:
        ui_result = get_ui_tree(depth=ui_depth)
        result["ui_tree"] = ui_result.get("tree") if ui_result.get("ok") else ui_result.get("error")
    if include_ocr:
        ocr_result = extract_text() if window_mismatch else extract_text_active_window()
        result["ocr_text"] = ocr_result.get("text", ocr_result.get("error", ""))
    if include_screenshot:
        if window_mismatch:
            result["screenshot"] = screenshot(max_width=max_width, max_height=max_height, quality=quality)
        else:
            result["screenshot"] = screenshot_active_window(
                max_width=max_width, max_height=max_height, quality=quality
            )
    return result


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, stream=sys.stderr)
    mcp.run()
