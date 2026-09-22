---
name: computer-use-windows
description: 用 Computer Use（截图、UI 树、鼠标键盘）检查并操控 Windows 桌面。当用户要求截图、点击、填表、打开应用、读窗口内容或自动化桌面操作时使用。
---

# Computer Use (Windows)

Use the `computer-use-windows` MCP tools to inspect and drive the active Windows desktop.

## Core Workflow
- Start with `get_screen_size` and `screenshot` (or `observe_screen` for combined info).
- After every UI action, call `wait` if needed and then `screenshot` again.
- Use absolute screen coordinates with `move_mouse`, `click`, and `drag_mouse`.
- Use `type_text` for ASCII, `type_unicode` for CJK/emoji (clipboard paste).
- Use `press_key` and `hotkey` for keyboard shortcuts.

## Token-Saving Strategies
- Prefer `observe_screen(include_screenshot=false, include_ui_tree=true)` for structured UI without image tokens.
- Use `extract_text` / `extract_text_active_window` for OCR when you only need text.
- Use `screenshot_active_window` instead of full-screen `screenshot` to reduce image size.
- Use `get_ui_tree` + `find_and_click_element` to interact without screenshots.
- Use `batch_actions` to combine multiple actions into a single call.
- Use `get_window_text` to read a window's text content via UI Automation.

## Window Management
- `list_windows` to see all open windows.
- `focus_window` to bring a window to the foreground by title.
- `run_program` to launch applications.
- `open_app` to launch common Windows apps by name (notepad, chrome, edge, calc, ...).

## Clipboard
- `get_clipboard` / `set_clipboard` for clipboard read/write.
- `type_unicode` uses clipboard internally for non-ASCII input.

## Chat / Messaging
- `send_text_to_window` — focus a window and paste Unicode text.
- `send_keys_to_window` — focus a window, paste text, and optionally press Enter.

## Constraints
- Authorized use only: operate solely on machines the user owns or has permission to control.
- Works only on the interactive desktop session.
- Cannot control elevated UAC prompts or the secure desktop.
- `find_and_click_element` and `get_ui_tree` require the `uiautomation` package (auto-installed in venv).
- Keep actions small and verify state from fresh screenshots or UI tree.

## Examples
- 「打开记事本写一句话」→ `open_app("notepad")` → `type_unicode("内容")`
- 「看看当前前台窗口」→ `observe_screen(include_screenshot=true, include_ui_tree=true)`
- 「在搜索框点一下并输入」→ `get_ui_tree` → `find_and_click_element` → `type_text`
