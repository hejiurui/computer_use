# computer-use-windows MCP

Windows 电脑控制 MCP（截图 / UI 树 / 鼠标键盘 / 剪贴板 / OCR），供支持 MCP 的客户端（如 MiMo Desktop、Claude Desktop、Cursor 等）通过 stdio 调用。

> **安全提示**：本工具可以截取屏幕、读取窗口文本/剪贴板，并模拟鼠标键盘。请仅在你拥有或已获授权的机器上运行，不要用于未授权访问、监控他人或规避安全策略。默认只作用于当前交互式桌面会话，不能操控 UAC / 安全桌面。

## 结构

```
computer-use-windows/
├── plugin.json                 # 插件元数据
├── .mcp.json                   # MCP 启动配置示例
├── LICENSE
├── scripts/
│   ├── launch-windows.cmd      # 建 venv + 启动
│   ├── requirements.txt
│   └── windows_server.py       # FastMCP 服务
└── skills/computer-use-windows/SKILL.md
```

## 参考与致谢

结构与工具面参考了以下开源项目（均为 MIT）：

- [ezpzai/codex-computer-use-windows](https://github.com/ezpzai/codex-computer-use-windows) — FastMCP + pyautogui/mss/uiautomation 工具面
- [CursorTouch/Windows-MCP](https://github.com/CursorTouch/Windows-MCP) — Windows MCP 工具设计

## 协议

使用官方 Python SDK：

```python
from mcp.server.fastmcp import FastMCP, Image
mcp = FastMCP("Computer Use (Windows)", json_response=True)
```

`mcp.run()` 走 stdio，由宿主以 local MCP 拉起。

## 工具

| 类别 | 工具 |
|---|---|
| 屏幕 | `get_screen_size` `get_cursor_position` `get_last_capture_info` `screenshot` `screenshot_active_window` |
| 鼠标 | `move_mouse` `click` `drag_mouse` |
| 键盘 | `type_text` `type_unicode` `press_key` `hotkey` `scroll` `wait` |
| 剪贴板 | `get_clipboard` `set_clipboard` |
| 窗口 | `list_windows` `focus_window` `run_program` `open_app` |
| UIA | `get_ui_tree` `find_and_click_element` `get_window_text` |
| 消息 | `send_text_to_window` `send_keys_to_window` |
| OCR | `extract_text` `extract_text_active_window` |
| 组合 | `batch_actions` `observe_screen` |

## 安装

依赖：Windows 10/11，Python 3.10+（带 `py` launcher）。

```bat
git clone https://github.com/hejiurui/computer_use.git
cd computer_use
scripts\launch-windows.cmd
```

首次启动会自动：

1. `py -3 -m venv .venv`
2. `pip install -r scripts/requirements.txt`（mcp&lt;2 / pyautogui / pillow / mss / uiautomation）
3. 运行 `windows_server.py`

## 注册为 MCP

在宿主的 MCP 配置中加入（路径按你的克隆位置调整），例如 MiMo Desktop 的 `~/.config/mimocode/mimocode.jsonc`：

```jsonc
"computer-use-windows": {
  "type": "local",
  "command": ["cmd.exe", "/d", "/s", "/c",
    "C:\\path\\to\\computer_use\\scripts\\launch-windows.cmd"],
  "enabled": true,
  "timeout": 30000
}
```

仓库内 `.mcp.json` 是相对路径示例，可按客户端要求复制修改。配置变更后通常需要**新建对话**才会加载。

技能文件：`skills/computer-use-windows/SKILL.md`。若宿主支持技能发现，可复制到对应技能根目录（例如 `~/.config/mimocode/skills/computer-use-windows/`）。

## 限制

- 仅交互式桌面会话
- 不能操控 UAC / 安全桌面
- 依赖前台/可访问的 UI；部分游戏、远程桌面或硬件加速窗口可能读不到 UIA 树

## License

MIT — 见 [LICENSE](./LICENSE)。
