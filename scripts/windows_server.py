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
import json
import logging
import os
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


def _get_window_pid(hwnd: int) -> int:
    pid = ctypes.wintypes.DWORD()
    ctypes.windll.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return int(pid.value)


# TokenIntegrityLevel = 25; RIDs: 0x1000 low, 0x2000 medium, 0x3000 high, 0x4000 system
_INTEGRITY_RID = {
    0x1000: "low",
    0x2000: "medium",
    0x3000: "high",
    0x4000: "system",
}


def _get_process_integrity(pid: int) -> dict:
    """Return {integrity, elevated, error} for a process. Honest probe — no silent success."""
    TOKEN_QUERY = 0x0008
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    TokenIntegrityLevel = 25
    kernel32 = ctypes.windll.kernel32
    advapi32 = ctypes.windll.advapi32
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return {
            "integrity": "unknown",
            "elevated": None,
            "error": f"OpenProcess failed ({ctypes.GetLastError()})",
        }
    token = ctypes.wintypes.HANDLE()
    try:
        if not advapi32.OpenProcessToken(handle, TOKEN_QUERY, ctypes.byref(token)):
            return {
                "integrity": "unknown",
                "elevated": None,
                "error": f"OpenProcessToken failed ({ctypes.GetLastError()})",
            }
        needed = ctypes.wintypes.DWORD()
        advapi32.GetTokenInformation(
            token, TokenIntegrityLevel, None, 0, ctypes.byref(needed)
        )
        buf = ctypes.create_string_buffer(max(needed.value, 128))
        if not advapi32.GetTokenInformation(
            token, TokenIntegrityLevel, buf, needed, ctypes.byref(needed)
        ):
            return {
                "integrity": "unknown",
                "elevated": None,
                "error": f"GetTokenInformation failed ({ctypes.GetLastError()})",
            }

        class SID_AND_ATTRIBUTES(ctypes.Structure):
            _fields_ = [("Sid", ctypes.c_void_p), ("Attributes", ctypes.wintypes.DWORD)]

        class TOKEN_MANDATORY_LABEL(ctypes.Structure):
            _fields_ = [("Label", SID_AND_ATTRIBUTES)]

        tml = TOKEN_MANDATORY_LABEL.from_buffer_copy(buf.raw)
        get_sub_auth_count = advapi32.GetSidSubAuthorityCount
        get_sub_auth_count.restype = ctypes.POINTER(ctypes.c_ubyte)
        get_sub_auth_count.argtypes = [ctypes.c_void_p]
        get_sub_auth = advapi32.GetSidSubAuthority
        get_sub_auth.restype = ctypes.POINTER(ctypes.wintypes.DWORD)
        get_sub_auth.argtypes = [ctypes.c_void_p, ctypes.wintypes.DWORD]
        sub_auth_count = get_sub_auth_count(tml.Label.Sid)
        if not sub_auth_count:
            return {"integrity": "unknown", "elevated": None, "error": "sid subauth missing"}
        rid = get_sub_auth(tml.Label.Sid, int(sub_auth_count.contents.value) - 1)
        rid_val = int(rid.contents.value) if rid else 0
        integrity = _INTEGRITY_RID.get(rid_val & 0xF000, f"rid_{rid_val}")
        elevated = integrity in ("high", "system")
        return {"integrity": integrity, "elevated": elevated, "error": None}
    finally:
        kernel32.CloseHandle(handle)
        if token:
            kernel32.CloseHandle(token)


def _check_target_access(hwnd: int) -> dict | None:
    """Return an error payload if the window's process is elevated above us (UAC boundary)."""
    if not hwnd:
        return None
    current = _get_process_integrity(os.getpid())
    target = _get_process_integrity(_get_window_pid(hwnd))
    if target.get("error"):
        return {
            "ok": False,
            "code": "process_query_failed",
            "error": target["error"],
            "hwnd": hwnd,
        }
    if target.get("elevated") and not current.get("elevated"):
        return {
            "ok": False,
            "code": "elevated_target",
            "error": (
                f"Target window is elevated (integrity={target.get('integrity')}) while this MCP "
                f"runs at integrity={current.get('integrity')}. UIA/input will fail or crash; "
                "refusing instead of silent no-op. Restart the host elevated or choose a non-elevated window."
            ),
            "hwnd": hwnd,
            "target_integrity": target.get("integrity"),
            "self_integrity": current.get("integrity"),
        }
    return None


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


_EDIT_CLASS_NAMES = {"edit", "richedit", "richedit20a", "richedit20w", "richedit50w", "texbox"}


def _find_edit_hwnd(root: int | None = None) -> int:
    """Find a focused-or-first edit/richedit child window for background EM_REPLACESEL."""
    user32 = ctypes.windll.user32
    root = root or user32.GetForegroundWindow()
    if not root:
        return 0
    found = user32.GetFocus()
    if found and _hwnd_class_name(found).lower() in _EDIT_CLASS_NAMES:
        return found
    result = {"hwnd": 0}

    def _callback(hwnd: int, _lParam: Any) -> bool:
        if _hwnd_class_name(hwnd).lower() in _EDIT_CLASS_NAMES:
            result["hwnd"] = hwnd
            return False
        return True

    EnumChildProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_int, ctypes.POINTER(ctypes.c_int))
    user32.EnumChildWindows(root, EnumChildProc(_callback), 0)
    return int(result["hwnd"])


def _hwnd_class_name(hwnd: int) -> str:
    buf = ctypes.create_unicode_buffer(256)
    ctypes.windll.user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def _type_background_unicode(text: str, hwnd: int | None = None) -> dict:
    """Type into an edit control via EM_SETSEL/EM_REPLACESEL without stealing focus semantics of SendInput."""
    user32 = ctypes.windll.user32
    target = hwnd or _find_edit_hwnd()
    if not target:
        return {
            "ok": False,
            "code": "no_edit_control",
            "error": "No edit/richedit control found for background typing",
        }
    EM_SETSEL = 0x00B1
    EM_REPLACESEL = 0x00C2
    user32.SendMessageW(target, EM_SETSEL, 0, -1)
    buf = ctypes.create_unicode_buffer(text)
    ok = user32.SendMessageW(target, EM_REPLACESEL, True, buf)
    return {
        "ok": True,
        "method": "em_replacesel",
        "hwnd": int(target),
        "class": _hwnd_class_name(target),
        "characters": len(text),
        "replace_result": int(ok),
    }


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
        "automation_id": getattr(control, "AutomationId", None) or "",
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
    truncated = False
    while child:
        if count >= max_children:
            truncated = True
            break
        children.append(_walk_ui_tree(child, depth - 1, max_children))
        child = child.GetNextSiblingControl()
        count += 1
    if children:
        info["children"] = children
    if truncated:
        info["truncated"] = True
    return info


def _find_control_recursive(
    control: Any,
    name: str = "",
    control_type: str = "",
    automation_id: str = "",
) -> Any:
    name_lower = (name or "").lower()
    aid_lower = (automation_id or "").lower()
    child = control.GetFirstChildControl()
    while child:
        child_name = (child.Name or "").lower()
        child_aid = (getattr(child, "AutomationId", None) or "").lower()
        name_ok = (not name_lower) or (name_lower in child_name)
        aid_ok = (not aid_lower) or (aid_lower in child_aid)
        type_ok = (not control_type) or (child.ControlTypeName == control_type)
        if name_ok and aid_ok and type_ok and (name_lower or aid_lower or control_type):
            return child
        found = _find_control_recursive(child, name, control_type, automation_id)
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


def _ocr_image(image: PILImage.Image) -> dict:
    """OCR via Windows.Media.Ocr. Returns {text, words:[{text,left,top,right,bottom}]} in image pixels."""
    buf = io.BytesIO()
    image.save(buf, format="PNG")
    b64 = base64.b64encode(buf.getvalue()).decode("ascii")
    ps_script = r"""
Add-Type -AssemblyName System.Runtime.WindowsRuntime
$null = [Windows.Storage.StorageFile, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Storage.FileAccessMode, Windows.Storage, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Graphics.Imaging.SoftwareBitmap, Windows.Graphics.Imaging, ContentType=WindowsRuntime]
$null = [Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType=WindowsRuntime]
$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
  $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1'
})[0]
function Await($WinRtTask, $ResultType) {
  $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
  $netTask = $asTask.Invoke($null, @($WinRtTask))
  $netTask.Wait(-1) | Out-Null
  return $netTask.Result
}
$b64 = [Console]::In.ReadToEnd().Trim()
$bytes = [Convert]::FromBase64String($b64)
$tmp = Join-Path ([IO.Path]::GetTempPath()) (([IO.Path]::GetRandomFileName()) + '.png')
[IO.File]::WriteAllBytes($tmp, $bytes)
try {
  $file = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($tmp)) ([Windows.Storage.StorageFile])
  $stream = Await ($file.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
  $decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
  $soft = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])
  $bitmap = [Windows.Graphics.Imaging.SoftwareBitmap]::Convert(
    $soft,
    [Windows.Graphics.Imaging.BitmapPixelFormat]::Bgra8,
    [Windows.Graphics.Imaging.BitmapAlphaMode]::Premultiplied
  )
  $ocrEngine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
  if ($null -eq $ocrEngine) { Write-Output '{"text":"","words":[],"error":"no_ocr_engine"}'; exit 0 }
  $result = Await ($ocrEngine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])
  $words = New-Object System.Collections.Generic.List[object]
  foreach ($line in $result.Lines) {
    foreach ($word in $line.Words) {
      $r = $word.BoundingRect
      $words.Add(@{
        text = $word.Text
        left = [int][Math]::Round($r.X)
        top = [int][Math]::Round($r.Y)
        right = [int][Math]::Round($r.X + $r.Width)
        bottom = [int][Math]::Round($r.Y + $r.Height)
      })
    }
  }
  ConvertTo-Json @{ text = $result.Text; words = $words } -Compress -Depth 4
} finally {
  Remove-Item -Force -ErrorAction SilentlyContinue $tmp
}
"""
    try:
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_script],
            input=b64,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=45,
        )
        raw = (proc.stdout or "").strip()
        if not raw:
            return {"text": "", "words": [], "error": (proc.stderr or "OCR returned empty").strip()[:500]}
        data = json.loads(raw)
        return {
            "text": data.get("text") or "",
            "words": data.get("words") or [],
            "error": data.get("error"),
        }
    except Exception as exc:
        logger.warning("OCR failed: %s", exc)
        return {"text": "", "words": [], "error": str(exc)}


def _ocr_capture(
    x: int | None, y: int | None, width: int | None, height: int | None
) -> dict:
    """Capture + OCR. Word boxes mapped to absolute screen coordinates."""
    image, info = _capture_image(x, y, width, height)
    ocr = _ocr_image(image)
    words = []
    for word in ocr.get("words") or []:
        words.append(
            {
                "text": word.get("text") or "",
                "left": info.origin_x + int(word.get("left") or 0),
                "top": info.origin_y + int(word.get("top") or 0),
                "right": info.origin_x + int(word.get("right") or 0),
                "bottom": info.origin_y + int(word.get("bottom") or 0),
            }
        )
    result = {
        "text": ocr.get("text") or "",
        "words": words,
        "origin_x": info.origin_x,
        "origin_y": info.origin_y,
        "coordinate_space": "screen",
    }
    if ocr.get("error"):
        result["error"] = ocr["error"]
    return result


def _match_ocr_word(words: list[dict], query: str) -> dict | None:
    """Best word/line-ish match for query (case-insensitive substring; exact word preferred)."""
    q = (query or "").strip().lower()
    if not q:
        return None
    exact = None
    substring = None
    for word in words:
        text = (word.get("text") or "").strip()
        lowered = text.lower()
        if lowered == q:
            exact = word
            break
        if q in lowered and substring is None:
            substring = word
    return exact or substring


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
def type_text(text: str, interval: float = 0.0, background: bool = False) -> dict:
    """Type ASCII text. background=true uses EM_REPLACESEL on an edit control (no SendInput)."""
    _ensure_windows()
    if background:
        return _type_background_unicode(text)
    pyautogui.write(text, interval=max(interval, 0.0))
    return {"ok": True, "method": "sendinput", "characters": len(text)}


@mcp.tool()
def type_unicode(
    text: str,
    background: bool = False,
    restore_clipboard: bool = True,
) -> dict:
    """Type Unicode text (CJK/emoji). Default: clipboard paste (Ctrl+V). background=true uses EM_REPLACESEL."""
    _ensure_windows()
    if background:
        return _type_background_unicode(text)
    previous = _clipboard_get_unicode() if restore_clipboard else None
    _clipboard_set_unicode(text)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.05)
    restored = False
    if restore_clipboard and previous is not None:
        try:
            _clipboard_set_unicode(previous)
            restored = True
        except Exception as exc:
            logger.warning("clipboard restore failed: %s", exc)
    return {
        "ok": True,
        "method": "clipboard_paste",
        "characters": len(text),
        "clipboard_restored": restored,
    }


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
def get_ui_tree(depth: int = 3, max_children: int = 50) -> dict:
    """Return the accessibility UI tree of the foreground window (low-token alternative to screenshots)."""
    _ensure_windows()
    if not _UIA_AVAILABLE:
        return {"ok": False, "error": "uiautomation package not installed"}
    hwnd = _get_foreground_hwnd()
    access_error = _check_target_access(hwnd)
    if access_error:
        return access_error
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
    name: str = "",
    control_type: str = "",
    automation_id: str = "",
    click_button: Literal["left", "right"] = "left",
) -> dict:
    """Find a UI element by name and/or AutomationId (optional ControlType) in the foreground window and click it."""
    _ensure_windows()
    if not _UIA_AVAILABLE:
        return {"ok": False, "error": "uiautomation package not installed"}
    if not (name or automation_id or control_type):
        return {"ok": False, "error": "Provide name and/or automation_id (and optional control_type)"}
    hwnd = _get_foreground_hwnd()
    access_error = _check_target_access(hwnd)
    if access_error:
        return access_error
    try:
        root = uia.GetForegroundControl()
        target = _find_control_recursive(root, name=name, control_type=control_type, automation_id=automation_id)
        if target is None:
            return {
                "ok": False,
                "code": "not_found",
                "error": f"Element not found (name={name!r}, automation_id={automation_id!r}, type={control_type!r})",
            }
        rect = target.BoundingRectangle
        cx = (rect.left + rect.right) // 2
        cy = (rect.top + rect.bottom) // 2
        pyautogui.click(cx, cy, button=click_button)
        return {
            "ok": True,
            "name": target.Name,
            "type": target.ControlTypeName,
            "automation_id": getattr(target, "AutomationId", None) or "",
            "clicked_x": cx,
            "clicked_y": cy,
            "coordinate_space": "screen",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def set_element_value(
    value: str,
    name: str = "",
    automation_id: str = "",
    control_type: str = "",
) -> dict:
    """Set a UI element's value via ValuePattern (text boxes, combos). No mouse click."""
    _ensure_windows()
    if not _UIA_AVAILABLE:
        return {"ok": False, "error": "uiautomation package not installed"}
    hwnd = _get_foreground_hwnd()
    access_error = _check_target_access(hwnd)
    if access_error:
        return access_error
    try:
        root = uia.GetForegroundControl()
        target = _find_control_recursive(root, name=name, control_type=control_type, automation_id=automation_id)
        if target is None:
            return {"ok": False, "code": "not_found", "error": "Element not found"}
        try:
            vp = target.GetValuePattern()
            if vp is not None:
                vp.SetValue(value)
                return {
                    "ok": True,
                    "method": "value_pattern",
                    "name": target.Name,
                    "automation_id": getattr(target, "AutomationId", None) or "",
                    "value": value,
                }
        except Exception as exc:
            logger.debug("ValuePattern failed: %s", exc)
        return {
            "ok": False,
            "code": "no_value_pattern",
            "error": "Element does not support ValuePattern.SetValue",
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def invoke_element(
    name: str = "",
    automation_id: str = "",
    control_type: str = "",
    action: Literal["invoke", "toggle", "expand", "collapse", "select"] = "invoke",
) -> dict:
    """Invoke/Toggle/Expand/Select a UI element via UIA patterns (buttons, checkboxes, trees)."""
    _ensure_windows()
    if not _UIA_AVAILABLE:
        return {"ok": False, "error": "uiautomation package not installed"}
    hwnd = _get_foreground_hwnd()
    access_error = _check_target_access(hwnd)
    if access_error:
        return access_error
    try:
        root = uia.GetForegroundControl()
        target = _find_control_recursive(root, name=name, control_type=control_type, automation_id=automation_id)
        if target is None:
            return {"ok": False, "code": "not_found", "error": "Element not found"}
        errors: list[str] = []
        try:
            if action == "invoke":
                ip = target.GetInvokePattern()
                if ip:
                    ip.Invoke()
                    return {"ok": True, "method": "invoke_pattern", "name": target.Name, "action": action}
                errors.append("no InvokePattern")
            if action == "toggle":
                tp = target.GetTogglePattern()
                if tp:
                    tp.Toggle()
                    return {"ok": True, "method": "toggle_pattern", "name": target.Name, "action": action}
                errors.append("no TogglePattern")
            if action in ("expand", "collapse"):
                ep = target.GetExpandCollapsePattern()
                if ep:
                    ep.Expand() if action == "expand" else ep.Collapse()
                    return {"ok": True, "method": "expand_collapse_pattern", "name": target.Name, "action": action}
                errors.append("no ExpandCollapsePattern")
            if action == "select":
                sp = target.GetSelectionItemPattern()
                if sp:
                    sp.Select()
                    return {"ok": True, "method": "selection_item_pattern", "name": target.Name, "action": action}
                errors.append("no SelectionItemPattern")
        except Exception as exc:
            errors.append(str(exc))
        return {
            "ok": False,
            "code": "pattern_unavailable",
            "error": "; ".join(errors) or f"action {action} failed",
            "name": target.Name,
        }
    except Exception as exc:
        return {"ok": False, "error": str(exc)}


@mcp.tool()
def click_text(
    text: str,
    x: int | None = None,
    y: int | None = None,
    width: int | None = None,
    height: int | None = None,
    button: Literal["left", "right"] = "left",
    clicks: int = 1,
) -> dict:
    """OCR a screen region and click the center of the best-matching word (exact match preferred)."""
    _ensure_windows()
    ocr = _ocr_capture(x, y, width, height)
    match = _match_ocr_word(ocr.get("words") or [], text)
    if not match:
        return {
            "ok": False,
            "code": "text_not_found",
            "error": f"No OCR word matching {text!r}",
            "ocr_text": (ocr.get("text") or "")[:500],
            "word_count": len(ocr.get("words") or []),
        }
    cx = (int(match["left"]) + int(match["right"])) // 2
    cy = (int(match["top"]) + int(match["bottom"])) // 2
    pyautogui.click(cx, cy, button=button, clicks=max(clicks, 1))
    return {
        "ok": True,
        "matched_text": match["text"],
        "clicked_x": cx,
        "clicked_y": cy,
        "box": {
            "left": match["left"],
            "top": match["top"],
            "right": match["right"],
            "bottom": match["bottom"],
        },
        "coordinate_space": "screen",
        "method": "ocr_click",
    }


@mcp.tool()
def get_window_text(title: str) -> dict:
    """Get all visible text from a window using UI Automation (no screenshot needed)."""
    _ensure_windows()
    focus_result = focus_window(title)
    if not focus_result.get("ok"):
        return focus_result
    time.sleep(0.15)
    hwnd = focus_result.get("hwnd") or _get_foreground_hwnd()
    access_error = _check_target_access(hwnd)
    if access_error:
        return access_error
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
def send_text_to_window(
    title: str,
    text: str,
    paste: bool = True,
    background: bool = False,
    restore_clipboard: bool = True,
) -> dict:
    """Focus a window and send text. paste=clipboard Ctrl+V; background=EM_REPLACESEL; else WM_SETTEXT."""
    _ensure_windows()
    focus_result = focus_window(title)
    if not focus_result.get("ok"):
        return focus_result
    time.sleep(0.15)
    hwnd = int(focus_result.get("hwnd") or 0)
    access_error = _check_target_access(hwnd)
    if access_error:
        return access_error
    if background:
        return _type_background_unicode(text, hwnd=_find_edit_hwnd(hwnd) or hwnd)
    if paste:
        previous = _clipboard_get_unicode() if restore_clipboard else None
        _clipboard_set_unicode(text)
        pyautogui.hotkey("ctrl", "v")
        time.sleep(0.05)
        restored = False
        if restore_clipboard and previous is not None:
            try:
                _clipboard_set_unicode(previous)
                restored = True
            except Exception as exc:
                logger.warning("clipboard restore failed: %s", exc)
        return {
            "ok": True,
            "method": "clipboard_paste",
            "characters": len(text),
            "clipboard_restored": restored,
        }
    edit_hwnd = _find_edit_hwnd(hwnd) or (ctypes.windll.user32.GetFocus() or hwnd)
    WM_SETTEXT = 0x000C
    buf = ctypes.create_unicode_buffer(text)
    ctypes.windll.user32.SendMessageW(edit_hwnd, WM_SETTEXT, 0, buf)
    return {"ok": True, "method": "wm_settext", "hwnd": int(edit_hwnd), "characters": len(text)}


@mcp.tool()
def send_keys_to_window(
    title: str,
    text: str,
    send_enter: bool = False,
    restore_clipboard: bool = True,
) -> dict:
    """Focus a window, paste Unicode text, and optionally press Enter (chat apps)."""
    _ensure_windows()
    focus_result = focus_window(title)
    if not focus_result.get("ok"):
        return focus_result
    time.sleep(0.15)
    hwnd = int(focus_result.get("hwnd") or 0)
    access_error = _check_target_access(hwnd)
    if access_error:
        return access_error
    previous = _clipboard_get_unicode() if restore_clipboard else None
    _clipboard_set_unicode(text)
    pyautogui.hotkey("ctrl", "v")
    time.sleep(0.05)
    if send_enter:
        time.sleep(0.05)
        pyautogui.press("enter")
    restored = False
    if restore_clipboard and previous is not None:
        try:
            _clipboard_set_unicode(previous)
            restored = True
        except Exception as exc:
            logger.warning("clipboard restore failed: %s", exc)
    return {
        "ok": True,
        "characters": len(text),
        "enter_sent": send_enter,
        "window": focus_result.get("title", ""),
        "clipboard_restored": restored,
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
    """Extract text and word boxes from the screen/region via Windows OCR. Boxes are screen-absolute."""
    _ensure_windows()
    ocr = _ocr_capture(x, y, width, height)
    if not ocr.get("text") and not ocr.get("words"):
        return {
            "ok": False,
            "code": "ocr_empty",
            "error": ocr.get("error") or "OCR returned no text",
            "text": "",
            "words": [],
        }
    return {
        "ok": True,
        "text": ocr.get("text") or "",
        "words": ocr.get("words") or [],
        "coordinate_space": "screen",
    }


@mcp.tool()
def extract_text_active_window() -> dict:
    """Extract text and word boxes from the currently focused window using OCR."""
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
        result["ocr_words"] = ocr_result.get("words", [])
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
