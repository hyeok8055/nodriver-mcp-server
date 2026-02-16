# nodriver MCP Server

[nodriver](https://github.com/AusaafMohamed/nodriver) 기반 MCP 서버. 브라우저 자동화, 크롤링, CDP 직접 호출을 MCP 프로토콜로 제공합니다.

## Install

### GitHub에서 직접 설치 (권장)

```bash
# uvx로 바로 실행 (설치 불필요)
uvx --from git+https://github.com/hyeok8055/nodriver-mcp-server.git nodriver-mcp

# 또는 pip로 설치
pip install git+https://github.com/hyeok8055/nodriver-mcp-server.git
```

### 로컬 개발용

```bash
git clone https://github.com/hyeok8055/nodriver-mcp-server.git
cd nodriver-mcp-server
uv venv && uv pip install -e .
```

## Claude Code에서 사용

프로젝트 루트에 `.mcp.json`을 생성하거나, 글로벌 설정(`~/.claude/settings.json`)에 추가:

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

또는 로컬 설치 후:

```json
{
  "mcpServers": {
    "nodriver": {
      "command": "nodriver-mcp"
    }
  }
}
```

## Claude Desktop에서 사용

`claude_desktop_config.json`에 추가:

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

## Run (수동 실행)

```bash
# 엔트리포인트
nodriver-mcp

# 또는 모듈 직접 실행
python -m nodriver_mcp
```

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

## 요구사항

- Python >= 3.10
- Chrome / Chromium 브라우저 설치 필요

## Notes

- Chrome 프로세스 실행이 환경 차이로 실패하면 `browser_start_session`의 config에서 Chromium 인자를 조정하세요.
