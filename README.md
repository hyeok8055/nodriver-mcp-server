# nodriver MCP Server

[nodriver](https://github.com/AusaafMohamed/nodriver) 기반 MCP 서버. 브라우저 자동화, 크롤링, CDP 직접 호출을 MCP 프로토콜로 제공합니다.

## 요구사항

- Chrome / Chromium 브라우저
- Python >= 3.10
- [uv](https://docs.astral.sh/uv/) (uvx 포함)

## 설치

### A. uvx로 바로 실행 (설치 불필요, 권장)

```bash
uvx --from git+https://github.com/hyeok8055/nodriver-mcp-server.git nodriver-mcp
```

### B. uv tool install (글로벌 설치)

```bash
uv tool install git+https://github.com/hyeok8055/nodriver-mcp-server.git
```

설치 후 `nodriver-mcp` 명령어를 직접 사용할 수 있습니다.

### C. 로컬 개발용

```bash
git clone https://github.com/hyeok8055/nodriver-mcp-server.git
cd nodriver-mcp-server
uv venv && uv pip install -e .
```

## Windows 11 설정 파일 경로

| 도구 | 설정 파일 경로 |
|------|--------------|
| Claude Code (글로벌) | `C:\Users\<user>\.claude\settings.json` |
| Claude Desktop | `C:\Users\<user>\AppData\Roaming\Claude\claude_desktop_config.json` |
| VSCode | `.vscode\mcp.json` (워크스페이스 루트) |
| Gemini CLI | `C:\Users\<user>\.gemini\settings.json` |
| Codex CLI | `C:\Users\<user>\.codex\config.toml` |

## Claude Code에서 사용

프로젝트 루트에 `.mcp.json`을 생성하거나, 글로벌 설정(`C:\Users\<user>\.claude\settings.json`)에 추가:

```json
{
  "mcpServers": {
    "nodriver": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/hyeok8055/nodriver-mcp-server.git", "nodriver-mcp"]
    }
  }
}
```

글로벌 설치(`uv tool install`) 후 사용 시:

```json
{
  "mcpServers": {
    "nodriver": {
      "command": "nodriver-mcp"
    }
  }
}
```

등록 확인: `claude mcp list`

## Claude Desktop에서 사용

`C:\Users\<user>\AppData\Roaming\Claude\claude_desktop_config.json`에 추가:

```json
{
  "mcpServers": {
    "nodriver": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/hyeok8055/nodriver-mcp-server.git", "nodriver-mcp"]
    }
  }
}
```

## VSCode에서 사용 (VS Code 1.99+, GitHub Copilot 필요)

워크스페이스 루트에 `.vscode/mcp.json` 파일을 생성:

```json
{
  "servers": {
    "nodriver": {
      "type": "stdio",
      "command": "uvx",
      "args": ["--from", "git+https://github.com/hyeok8055/nodriver-mcp-server.git", "nodriver-mcp"]
    }
  }
}
```

> **주의**: VSCode는 키 이름이 `servers`입니다 (다른 도구의 `mcpServers`와 다름). `"type": "stdio"` 필드가 필수입니다.

## Gemini CLI에서 사용

`C:\Users\<user>\.gemini\settings.json`에 추가:

```json
{
  "mcpServers": {
    "nodriver": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/hyeok8055/nodriver-mcp-server.git", "nodriver-mcp"]
    }
  }
}
```

설정 후 Gemini CLI를 재시작하고 `/mcp` 명령으로 연결 상태를 확인하세요.

> **참고**: `uvx`는 `.exe` 파일이므로 Windows에서 `cmd /c` 래퍼 없이 직접 사용할 수 있습니다.

## OpenAI Codex CLI에서 사용

`C:\Users\<user>\.codex\config.toml`에 추가 (TOML 형식):

```toml
[mcp_servers.nodriver]
command = "uvx"
args = ["--from", "git+https://github.com/hyeok8055/nodriver-mcp-server.git", "nodriver-mcp"]
```

> **주의**: Codex CLI의 Windows 지원은 experimental입니다.

## 제공 도구 (Tools)

| 카테고리 | 도구 |
|---------|------|
| **세션** | `browser_start_session`, `browser_stop_session`, `browser_session_info`, `browser_cleanup_stale`, `browser_healthcheck` |
| **탭** | `browser_new_tab`, `browser_switch_tab`, `browser_close_tab`, `browser_list_tabs` |
| **내비게이션** | `browser_navigate`, `browser_go_back`, `browser_go_forward`, `browser_refresh`, `browser_wait_for` |
| **인터랙션** | `browser_click_by_selector`, `browser_click_by_text`, `browser_fill`, `browser_select_text`, `browser_select_value`, `browser_hover`, `browser_press_key`, `browser_scroll` |
| **페이지 추출** | `page_get_content`, `page_get_html`, `page_get_text`, `page_get_links`, `page_get_resources` |
| **스토리지** | `page_set_local_storage`, `page_get_local_storage`, `page_clear_local_storage` |
| **파일/드래그** | `element_send_file`, `element_mouse_drag` |
| **CDP** | `cdp_call`, `cdp_subscribe`, `cdp_unsubscribe` |
| **네트워크/콘솔** | `network_capture_start`, `network_capture_stop`, `browser_capture_console` |
| **스크린샷** | `browser_screenshot` |

## 응답 형식

모든 도구는 동일한 구조를 반환합니다:

```json
{
  "ok": true,
  "session_id": "...",
  "tab_id": "...",
  "data": {},
  "artifacts": [],
  "warnings": [],
  "elapsed_ms": 0,
  "error_code": null,
  "error_detail": null
}
```

## 문제 해결

### Chrome을 찾지 못하는 경우

`browser_start_session` 호출 시 `config`에서 Chrome 실행 파일 경로를 직접 지정하세요:

```json
{
  "extra_chromium_args": ["--no-sandbox"],
  "browser_executable_path": "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe"
}
```

### uvx 명령을 찾지 못하는 경우

```bash
where uvx
```

로 경로를 확인한 뒤, 설정 파일에서 `"command"` 값을 절대 경로(예: `C:\Users\<user>\.local\bin\uvx.exe`)로 지정하세요.

### Gemini CLI에서 MCP 서버가 인식되지 않는 경우

Gemini CLI 재시작 후 `/mcp` 명령을 실행해 연결 상태를 확인하세요.
