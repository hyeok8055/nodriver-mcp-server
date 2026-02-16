"""
Nodriver 기반 MCP 서버 v2 (실행 구현).
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import sys
import tempfile
import uuid
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Deque, Dict, Optional, Union

import nodriver as uc
from nodriver.core.connection import ProtocolException
from mcp.server.fastmcp import Context, FastMCP

mcp = FastMCP("nodriver-mcp-v2")
logger = logging.getLogger("nodriver-mcp")

MAX_EVENT_BYTES = 4000
DEFAULT_EVENT_LIMIT = 2000

ALLOWED_CDP_DOMAINS = {
    "Page",
    "Network",
    "Runtime",
    "DOM",
    "Input",
    "Security",
    "Log",
    "Target",
}

CDP_MODULE_EVENTS = {
    "Network": (
        "Network.requestWillBeSent",
        "Network.responseReceived",
        "Network.loadingFinished",
        "Network.loadingFailed",
    ),
    "Log": (
        "Log.entryAdded",
    ),
    "Page": (
        "Page.loadEventFired",
        "Page.domContentEventFired",
    ),
}


class ErrorCodes:
    SESSION_NOT_FOUND = "ERR_SESSION_NOT_FOUND"
    TAB_NOT_FOUND = "ERR_TAB_NOT_FOUND"
    SELECTOR_TIMEOUT = "ERR_SELECTOR_TIMEOUT"
    INVALID_INPUT = "ERR_INVALID_INPUT"
    CTP_DENIED = "ERR_CDP_DENIED"
    TIMEOUT = "ERR_TIMEOUT"
    INTERNAL = "ERR_INTERNAL"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _to_iso(dt: datetime) -> str:
    return dt.isoformat()


def _truncate(value: Any, max_bytes: int = 20000) -> str:
    if value is None:
        return ""
    if hasattr(value, 'to_json') and callable(value.to_json):
        value = _to_serializable(value)
    text = json.dumps(value, ensure_ascii=False, default=str) if isinstance(value, (dict, list, tuple)) else str(value)
    if len(text) <= max_bytes:
        return text
    return text[:max_bytes] + f"...(truncated {len(text) - max_bytes} bytes)"


def _to_serializable(obj: Any) -> Any:
    """Recursively convert CDP dataclass objects (RemoteObject etc.) to plain dicts."""
    if obj is None or isinstance(obj, (str, int, float, bool)):
        return obj
    if hasattr(obj, 'to_json') and callable(obj.to_json):
        return _to_serializable(obj.to_json())
    if isinstance(obj, dict):
        return {str(k): _to_serializable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_to_serializable(item) for item in obj]
    return str(obj)


def _js_value(value: str) -> str:
    return json.dumps(value)


def _camel_to_snake(name: str) -> str:
    return re.sub(r"(?<!^)(?=[A-Z])", "_", name).lower()


def _cdp_module_name(domain: str) -> str:
    return {
        "INPUT": "input_",
    }.get(domain.upper(), domain.lower())


def _resolve_cdp_callable(domain: str, method: str):
    full = method
    if "." in method and not domain:
        full = method
        domain, method = method.split(".", 1)
    domain_normalized = _cdp_module_name(domain)
    cdp_module = getattr(uc.cdp, domain_normalized, None)
    if cdp_module is None:
        raise ValueError(f"cdp domain '{domain}' not found")

    if hasattr(cdp_module, method):
        return full, getattr(cdp_module, method), method

    snake = _camel_to_snake(method)
    if hasattr(cdp_module, snake):
        return full, getattr(cdp_module, snake), snake

    raise ValueError(f"cdp method '{method}' not found in domain '{domain}'")


@dataclass
class ToolResult:
    ok: bool
    session_id: Optional[str] = None
    tab_id: Optional[str] = None
    data: Optional[Any] = None
    artifacts: list[dict] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    error_code: Optional[str] = None
    error_detail: Optional[str] = None

    @classmethod
    def fail(cls, session_id: Optional[str], tab_id: Optional[str], error_code: str, detail: str) -> "ToolResult":
        return cls(ok=False, session_id=session_id, tab_id=tab_id, error_code=error_code, error_detail=detail)

    def to_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "session_id": self.session_id,
            "tab_id": self.tab_id,
            "data": self.data,
            "artifacts": self.artifacts,
            "warnings": self.warnings,
            "elapsed_ms": self.elapsed_ms,
            "error_code": self.error_code,
            "error_detail": self.error_detail,
        }


@dataclass
class SessionConfig:
    headless: bool = False
    width: int = 1920
    height: int = 1080
    user_agent: Optional[str] = None
    locale: Optional[str] = None
    timezone_id: Optional[str] = None
    proxy: Optional[str] = None
    user_data_dir: Optional[str] = None
    disable_cookies: bool = False
    extra_chromium_args: list[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, value: Optional[Dict[str, Any]]) -> "SessionConfig":
        if not value:
            return cls()
        return cls(
            headless=bool(value.get("headless", False)),
            width=max(1, int(value.get("width", 1920))),
            height=max(1, int(value.get("height", 1080))),
            user_agent=value.get("user_agent"),
            locale=value.get("locale"),
            timezone_id=value.get("timezone_id"),
            proxy=value.get("proxy"),
            user_data_dir=value.get("user_data_dir"),
            disable_cookies=bool(value.get("disable_cookies", False)),
            extra_chromium_args=list(value.get("extra_chromium_args", [])),
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "headless": self.headless,
            "width": self.width,
            "height": self.height,
            "user_agent": self.user_agent,
            "locale": self.locale,
            "timezone_id": self.timezone_id,
            "proxy": self.proxy,
            "user_data_dir": self.user_data_dir,
            "disable_cookies": self.disable_cookies,
            "extra_chromium_args": self.extra_chromium_args,
        }


@dataclass
class TabRecord:
    tab_id: str
    tab: Any
    created_at: datetime = field(default_factory=_utcnow)
    last_used_at: datetime = field(default_factory=_utcnow)
    is_active: bool = False

    def touch(self) -> None:
        self.last_used_at = _utcnow()


@dataclass
class BrowserSession:
    session_id: str
    config: SessionConfig
    browser: Any
    tmp_dir: Path
    context: Optional[Any] = None
    tabs: Dict[str, TabRecord] = field(default_factory=dict)
    active_tab_id: Optional[str] = None
    event_buffer: Deque[Dict[str, Any]] = field(default_factory=lambda: deque(maxlen=DEFAULT_EVENT_LIMIT))
    subscriptions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    created_at: datetime = field(default_factory=_utcnow)
    last_used_at: datetime = field(default_factory=_utcnow)
    network_capture_active: bool = False
    network_capture_settings: Dict[str, Any] = field(default_factory=dict)

    def touch(self) -> None:
        self.last_used_at = _utcnow()
        if self.active_tab_id and self.active_tab_id in self.tabs:
            self.tabs[self.active_tab_id].touch()


def _serialize_event(event: Any, max_bytes: int = MAX_EVENT_BYTES) -> Dict[str, Any]:
    payload = event
    if not isinstance(event, (str, int, float, bool, list, dict)):
        if hasattr(event, 'to_json') and callable(event.to_json):
            payload = _to_serializable(event)
        else:
            try:
                payload = event.__dict__
            except Exception:
                payload = str(event)
    return {"ts": _to_iso(_utcnow()), "payload": _truncate(payload, max_bytes=max_bytes), "raw_type": type(event).__name__}


class BrowserManager:
    def __init__(self, max_sessions: int = 8) -> None:
        self._sessions: Dict[str, BrowserSession] = {}
        self._lock = asyncio.Lock()
        self._max_sessions = max_sessions

    async def start_session(self, config: Optional[SessionConfig] = None) -> ToolResult:
        start = _utcnow()
        config = config or SessionConfig()
        if config.width <= 0 or config.height <= 0:
            return ToolResult.fail(None, None, ErrorCodes.INVALID_INPUT, "width/height must be positive")

        sid = str(uuid.uuid4())
        tmp_dir = Path(tempfile.mkdtemp(prefix=f"nodriver-mcp-{sid[:8]}-"))

        browser_args = [
            f"--window-size={config.width},{config.height}",
            "--disable-dev-shm-usage",
            "--disable-blink-features=AutomationControlled",
            "--no-first-run",
        ]
        if config.locale:
            browser_args.append(f"--lang={config.locale}")
        if config.proxy:
            browser_args.append(f"--proxy-server={config.proxy}")
        if config.user_agent:
            browser_args.append(f"--user-agent={config.user_agent}")
        browser_args.extend(config.extra_chromium_args)

        async with self._lock:
            if len(self._sessions) >= self._max_sessions:
                return ToolResult.fail(None, None, ErrorCodes.INTERNAL, "max sessions reached")

        attempt_kwargs = [
            {"headless": config.headless, "browser_args": browser_args},
        ]
        if config.user_data_dir:
            attempt_kwargs.insert(0, {"headless": config.headless, "browser_args": browser_args, "user_data_dir": str(Path(config.user_data_dir).expanduser())})

        browser = None
        last_err = None
        for params in attempt_kwargs:
            try:
                browser = await uc.start(**params)
                break
            except Exception as exc:  # noqa: BLE001
                last_err = exc
                logger.warning("uc.start failed: %s", exc)
        if browser is None:
            try:
                browser = await uc.start()
            except Exception as exc:  # noqa: BLE001
                shutil.rmtree(tmp_dir, ignore_errors=True)
                return ToolResult.fail(None, None, ErrorCodes.INTERNAL, f"uc.start failed: {last_err or exc}")

        session = BrowserSession(session_id=sid, config=config, browser=browser, tmp_dir=tmp_dir)
        try:
            tab = await _open_tab(browser, "about:blank")
            tab_id = str(uuid.uuid4())
            session.tabs[tab_id] = TabRecord(tab_id=tab_id, tab=tab, is_active=True)
            session.active_tab_id = tab_id
            async with self._lock:
                self._sessions[sid] = session
        except Exception:
            await _safe_stop_browser(browser)
            shutil.rmtree(tmp_dir, ignore_errors=True)
            return ToolResult.fail(None, None, ErrorCodes.INTERNAL, "failed to create initial tab")

        return ToolResult(
            ok=True,
            session_id=sid,
            tab_id=tab_id,
            elapsed_ms=int((_utcnow() - start).total_seconds() * 1000),
            data={"config": config.as_dict(), "created_at": _to_iso(session.created_at), "tmp_dir": str(tmp_dir)},
        )

    async def stop_session(self, session_id: str) -> ToolResult:
        start = _utcnow()
        async with self._lock:
            session = self._sessions.pop(session_id, None)
        if session is None:
            return ToolResult.fail(session_id, None, ErrorCodes.SESSION_NOT_FOUND, "session not found")

        async with session.lock:
            for info in list(session.subscriptions.values()):
                _remove_handler(info)
            session.subscriptions.clear()

            for rec in list(session.tabs.values()):
                try:
                    await _safe_call(rec.tab.close)
                except Exception:
                    pass

            await _safe_stop_browser(session.browser)
            shutil.rmtree(session.tmp_dir, ignore_errors=True)

            return ToolResult(
                ok=True,
                session_id=session_id,
                data={"closed": True},
                elapsed_ms=int((_utcnow() - start).total_seconds() * 1000),
            )

    async def get_session(self, session_id: str) -> Optional[BrowserSession]:
        async with self._lock:
            return self._sessions.get(session_id)

    async def cleanup_stale(self, ttl_seconds: int) -> ToolResult:
        now = _utcnow()
        async with self._lock:
            stale = [sid for sid, session in self._sessions.items() if session.last_used_at < now - timedelta(seconds=ttl_seconds)]
        removed: list[str] = []
        for sid in stale:
            result = await self.stop_session(sid)
            if result.ok:
                removed.append(sid)
        return ToolResult(ok=True, data={"removed": removed, "count": len(removed)})


def _maybe_async(value: Any) -> Any:
    if asyncio.iscoroutine(value):
        return value
    fut = asyncio.sleep(0, result=value)
    return fut


async def _safe_call(fn: Union[Callable[..., Any], None], *args: Any, **kwargs: Any) -> Any:
    if fn is None:
        return None
    result = fn(*args, **kwargs)
    return await _maybe_async(result)


async def _safe_stop_browser(browser: Any) -> None:
    await _safe_call(getattr(browser, "stop", None))


def _find_tab(session: BrowserSession, tab_id: Optional[str]) -> TabRecord:
    if tab_id is None:
        if session.active_tab_id and session.active_tab_id in session.tabs:
            return session.tabs[session.active_tab_id]
        if session.tabs:
            first = next(iter(session.tabs.values()))
            session.active_tab_id = first.tab_id
            first.is_active = True
            return first
        raise KeyError("no tab")
    if tab_id not in session.tabs:
        raise KeyError(f"tab {tab_id} not found")
    for rec in session.tabs.values():
        rec.is_active = rec.tab_id == tab_id
    session.active_tab_id = tab_id
    return session.tabs[tab_id]


async def _open_tab(browser: Any, url: str) -> Any:
    candidates: list[Callable[..., Any]] = []
    if hasattr(browser, "new_tab"):
        candidates.append(browser.new_tab)
    if hasattr(browser, "get"):
        candidates.append(browser.get)
    if not candidates:
        raise RuntimeError("browser.new_tab/get not available")
    for method in candidates:
        try:
            return await _safe_call(method, url)
        except Exception:
            continue
    raise RuntimeError("tab open failed")


async def _navigate(tab: Any, url: str, timeout_ms: int = 20000) -> None:
    if hasattr(tab, "goto"):
        nav = tab.goto(url)  # type: ignore[misc]
    elif hasattr(tab, "get"):
        nav = tab.get(url)  # type: ignore[misc]
    else:
        raise RuntimeError("tab navigation is not supported")
    await asyncio.wait_for(_maybe_async(nav), timeout=timeout_ms / 1000)


async def _select_element(tab: Any, selector: str, timeout_ms: int) -> Any:
    if not selector:
        raise ValueError("selector required")
    if not hasattr(tab, "select"):
        raise RuntimeError("tab.select not available")
    return await _safe_call(tab.select, selector, timeout=timeout_ms / 1000)


async def _find_text(tab: Any, text: str, timeout_ms: int) -> Any:
    if not text:
        raise ValueError("text required")
    if not hasattr(tab, "find"):
        raise RuntimeError("tab.find not available")
    return await _safe_call(tab.find, text, timeout=timeout_ms / 1000)


async def _evaluate(tab: Any, script: str) -> Any:
    if not hasattr(tab, "evaluate"):
        raise RuntimeError("tab.evaluate not available")
    return await _safe_call(tab.evaluate, script)


def _set_cookie_flag(tab: Any, tab_record: TabRecord) -> None:
    tab_record.touch()


def _serialize_result(obj: Any) -> Any:
    if isinstance(obj, (str, int, float, bool)):
        return obj
    if obj is None:
        return None
    if hasattr(obj, 'to_json') and callable(obj.to_json):
        return _to_serializable(obj)
    try:
        return json.loads(obj)
    except Exception:
        return _truncate(obj)


def _network_entry(event: str, payload: Any) -> Dict[str, Any]:
    return {"event": event, "ts": _to_iso(_utcnow()), "payload": _serialize_event(payload)}


def _record_event(session: BrowserSession, event: str, data: Any) -> None:
    if session is None:
        return
    session.event_buffer.append(_network_entry(event, data))


def _make_handler(session: BrowserSession, event: str, max_bytes: int) -> Callable[[Any], None]:
    def _handler(payload: Any) -> None:
        try:
            serialized = _serialize_event(payload, max_bytes=max_bytes)
        except Exception:
            serialized = {"raw": str(payload)}
        session.event_buffer.append({"event": event, "ts": _to_iso(_utcnow()), "payload": serialized})

    return _handler


def _remove_handler(info: Dict[str, Any]) -> None:
    tab = info.get("tab")
    event = info.get("event")
    callback = info.get("callback")
    remover = getattr(tab, "remove_handler", None)
    if callable(remover) and event and callback:
        try:
            remover(event, callback)
        except Exception:
            pass


def _is_allowed_domain(domain: str) -> bool:
    return domain in ALLOWED_CDP_DOMAINS


async def _cdp_call(tab: Any, domain: str, method: str, params: Optional[Dict[str, Any]]) -> Any:
    if not domain and "." in method:
        domain, method = method.split(".", 1)
    if not domain:
        raise RuntimeError("domain required")
    _, cdp_callable, _ = _resolve_cdp_callable(domain, method)
    send = getattr(tab, "send", None)
    if not callable(send):
        raise RuntimeError("tab.send not available")
    try:
        cdp_obj = cdp_callable(**(params or {}))
    except TypeError as exc:  # noqa: BLE001
        raise RuntimeError(f"Invalid CDP params: {exc}") from exc
    return await _safe_call(send, cdp_obj)


def _cdp_error_code(exc: Exception) -> str:
    if isinstance(exc, ValueError):
        return ErrorCodes.INVALID_INPUT
    if isinstance(exc, ProtocolException):
        return ErrorCodes.CTP_DENIED
    return ErrorCodes.INTERNAL


def _session_summary(session: BrowserSession) -> Dict[str, Any]:
    return {
        "session_id": session.session_id,
        "created_at": _to_iso(session.created_at),
        "last_used_at": _to_iso(session.last_used_at),
        "active_tab_id": session.active_tab_id,
        "tab_count": len(session.tabs),
        "subscription_count": len(session.subscriptions),
        "event_count": len(session.event_buffer),
        "network_capture_active": session.network_capture_active,
        "config": session.config.as_dict(),
    }


async def _ctx_info(ctx: Optional[Context], level: str, msg: str) -> None:
    if ctx is None:
        return
    fn = getattr(ctx, level, None)
    if fn is None:
        return
    result = fn(msg)
    if asyncio.iscoroutine(result):
        await result


async def _ctx_progress(ctx: Optional[Context], current: int, total: int, message: str = "") -> None:
    if ctx is None:
        return
    fn = getattr(ctx, "report_progress", None)
    if fn is None:
        return
    result = fn(current=current, total=total, message=message)
    if asyncio.iscoroutine(result):
        await result


manager = BrowserManager()
@mcp.tool()
async def browser_start_session(ctx: Optional[Context], config: Optional[dict[str, Any]] = None) -> dict[str, Any]:
    start = _utcnow()
    await _ctx_info(ctx, "info", "tool: browser_start_session")
    conf = SessionConfig.from_dict(config)
    result = await manager.start_session(conf)
    result.elapsed_ms = int((_utcnow() - start).total_seconds() * 1000)
    return result.to_dict()


@mcp.tool()
async def browser_stop_session(ctx: Optional[Context], session_id: str) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_stop_session session={session_id}")
    result = await manager.stop_session(session_id)
    return result.to_dict()


@mcp.tool()
async def browser_session_info(ctx: Optional[Context], session_id: str) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_session_info session={session_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, None, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        session.touch()
        return ToolResult(
            ok=True,
            session_id=session_id,
            data={
                "session": _session_summary(session),
                "tabs": [
                    {
                        "tab_id": rec.tab_id,
                        "is_active": rec.is_active,
                        "created_at": _to_iso(rec.created_at),
                        "last_used_at": _to_iso(rec.last_used_at),
                    }
                    for rec in session.tabs.values()
                ],
            },
        ).to_dict()


@mcp.tool()
async def browser_new_tab(ctx: Optional[Context], session_id: str, url: Optional[str] = None, headless: Optional[bool] = None, new_window: bool = False) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_new_tab session={session_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, None, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    if headless is not None:
        warnings: list[str] = ["headless argument ignored in active browser context"]
    else:
        warnings = []
    async with session.lock:
        target = url or "about:blank"
        try:
            new_tab = await _open_tab(session.browser, target)
            tab_id = str(uuid.uuid4())
            session.tabs[tab_id] = TabRecord(tab_id=tab_id, tab=new_tab, is_active=True)
            if session.active_tab_id:
                session.tabs[session.active_tab_id].is_active = False
            session.active_tab_id = tab_id
            session.touch()
            return ToolResult(
                ok=True,
                session_id=session_id,
                tab_id=tab_id,
                data={"tab_id": tab_id, "url": target, "new_window": new_window},
                warnings=warnings,
            ).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, None, ErrorCodes.INTERNAL, f"new tab failed: {exc}").to_dict()


@mcp.tool()
async def browser_switch_tab(ctx: Optional[Context], session_id: str, tab_id: str) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_switch_tab session={session_id} tab={tab_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        if tab_id not in session.tabs:
            return ToolResult.fail(session_id, tab_id, ErrorCodes.TAB_NOT_FOUND, "tab not found").to_dict()
        for rec in session.tabs.values():
            rec.is_active = rec.tab_id == tab_id
        session.active_tab_id = tab_id
        session.touch()
        return ToolResult(ok=True, session_id=session_id, tab_id=tab_id, data={"switched": True}).to_dict()


@mcp.tool()
async def browser_close_tab(ctx: Optional[Context], session_id: str, tab_id: str) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_close_tab session={session_id} tab={tab_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = session.tabs.pop(tab_id, None)
        if rec is None:
            return ToolResult.fail(session_id, tab_id, ErrorCodes.TAB_NOT_FOUND, "tab not found").to_dict()
        try:
            await _safe_call(rec.tab.close)
        except Exception:
            pass
        if session.active_tab_id == tab_id:
            session.active_tab_id = next(iter(session.tabs), None)
            if session.active_tab_id:
                session.tabs[session.active_tab_id].is_active = True
            else:
                default_tab = await _open_tab(session.browser, "about:blank")
                new_tab_id = str(uuid.uuid4())
                session.tabs[new_tab_id] = TabRecord(tab_id=new_tab_id, tab=default_tab, is_active=True)
                session.active_tab_id = new_tab_id
        session.touch()
        return ToolResult(ok=True, session_id=session_id, data={"closed": tab_id, "active_tab_id": session.active_tab_id}).to_dict()


@mcp.tool()
async def browser_list_tabs(ctx: Optional[Context], session_id: str) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_list_tabs session={session_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, None, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        session.touch()
        return ToolResult(
            ok=True,
            session_id=session_id,
            data={
                "tabs": [
                    {
                        "tab_id": rec.tab_id,
                        "is_active": rec.is_active,
                        "created_at": _to_iso(rec.created_at),
                        "last_used_at": _to_iso(rec.last_used_at),
                    }
                    for rec in session.tabs.values()
                ]
            },
        ).to_dict()
@mcp.tool()
async def browser_navigate(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str],
    url: str,
    timeout_ms: int = 20000
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_navigate session={session_id} url={url}")
    start = _utcnow()
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()

    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
        except KeyError as exc:
            return ToolResult.fail(session_id, tab_id, ErrorCodes.TAB_NOT_FOUND, str(exc)).to_dict()

        try:
            await _navigate(rec.tab, url, timeout_ms=timeout_ms)
            return ToolResult(
                ok=True,
                session_id=session_id,
                tab_id=rec.tab_id,
                data={"url": url},
                elapsed_ms=int((_utcnow() - start).total_seconds() * 1000),
            ).to_dict()
        except asyncio.TimeoutError:
            return ToolResult.fail(session_id, rec.tab_id, ErrorCodes.TIMEOUT, "navigation timeout").to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_go_back(ctx: Optional[Context], session_id: str, tab_id: Optional[str] = None) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_go_back session={session_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            await _safe_call(rec.tab.back)
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"action": "back"}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_go_forward(ctx: Optional[Context], session_id: str, tab_id: Optional[str] = None) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_go_forward session={session_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            await _safe_call(rec.tab.forward)
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"action": "forward"}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_refresh(ctx: Optional[Context], session_id: str, tab_id: Optional[str] = None, ignore_cache: bool = False) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_refresh session={session_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            if hasattr(rec.tab, "reload"):
                fn = rec.tab.reload
                if ignore_cache:
                    await _safe_call(fn, ignore_cache=True)
                else:
                    await _safe_call(fn)
            else:
                await _safe_call(rec.tab.get, rec.tab.url)
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"ignore_cache": ignore_cache}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_wait_for(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    selector: str = "",
    text: str = "",
    timeout: int = 10,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_wait_for session={session_id}")
    if not selector and not text:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "selector or text required").to_dict()
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            if selector:
                await _select_element(rec.tab, selector, timeout * 1000)
            else:
                await _find_text(rec.tab, text, timeout * 1000)
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"selector": selector, "text": text}).to_dict()
        except asyncio.TimeoutError:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.SELECTOR_TIMEOUT, "wait timeout").to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_click_by_selector(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    selector: str = "",
    timeout_ms: int = 10000,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_click_by_selector session={session_id}")
    if not selector:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "selector required").to_dict()
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            element = await _select_element(rec.tab, selector, timeout_ms)
            await _safe_call(element.click)
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"selector": selector}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_click_by_text(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    text: str = "",
    timeout_ms: int = 10000,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_click_by_text session={session_id}")
    if not text:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "text required").to_dict()
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            element = await _find_text(rec.tab, text, timeout_ms)
            await _safe_call(element.click)
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"text": text}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_fill(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str],
    selector: str,
    value: str,
    timeout_ms: int = 10000
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_fill session={session_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            element = await _select_element(rec.tab, selector, timeout_ms)
            if hasattr(element, "clear_input"):
                await _safe_call(element.clear_input)
            elif hasattr(element, "clear"):
                await _safe_call(element.clear)
            await _safe_call(element.send_keys, value)
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"selector": selector, "value": value}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_select_text(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str],
    selector: str,
    text: str,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_select_text session={session_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            safe_selector = _js_value(selector)
            safe_text = _js_value(text)
            script = (
                "(() => {"
                "const sel = document.querySelector(" + safe_selector + ");"
                "if (!sel) { throw new Error('not found'); }"
                "for (let i = 0; i < sel.options.length; i++) {"
                f" if (sel.options[i].text === {safe_text}) {{ sel.value = sel.options[i].value; sel.dispatchEvent(new Event('change', {{ bubbles: true }})); return sel.value; }}"
                " }"
                " throw new Error('option not found');"
                "})();"
            )
            result = await _evaluate(rec.tab, script)
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"selector": selector, "selected": _serialize_result(result)}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_select_value(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str],
    selector: str,
    value: str,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_select_value session={session_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            safe_selector = _js_value(selector)
            safe_value = _js_value(value)
            script = (
                "(() => {"
                "const sel = document.querySelector(" + safe_selector + ");"
                "if (!sel) { throw new Error('not found'); }"
                f" sel.value = {safe_value};"
                "sel.dispatchEvent(new Event('change', {{ bubbles: true }}));"
                "return sel.value;"
                "})();"
            )
            result = await _evaluate(rec.tab, script)
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"selector": selector, "selected": _serialize_result(result)}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_hover(ctx: Optional[Context], session_id: str, tab_id: Optional[str], selector: str) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_hover session={session_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    if not selector:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "selector required").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            element = await _select_element(rec.tab, selector, 10000)
            if hasattr(element, "mouse_move"):
                await _safe_call(element.mouse_move)
            else:
                await _evaluate(rec.tab, f"document.querySelector({_js_value(selector)}).dispatchEvent(new MouseEvent('mouseover', {{bubbles:true}}));")
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"selector": selector}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_press_key(ctx: Optional[Context], session_id: str, tab_id: Optional[str], key: str, modifiers: Optional[str] = None) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_press_key session={session_id}")
    if not key:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "key required").to_dict()
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            final = f"{modifiers}+{key}" if modifiers else key
            if hasattr(rec.tab, "send_keys"):
                await _safe_call(rec.tab.send_keys, final)
            else:
                await _evaluate(rec.tab, f"window.dispatchEvent(new KeyboardEvent('keydown', {{ key: {json.dumps(final)} }}));")
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"key": final}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def browser_scroll(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str],
    direction: str = "down",
    amount: int = 800,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_scroll session={session_id}")
    if direction not in {"up", "down", "left", "right", "top", "bottom"}:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "invalid direction").to_dict()
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            scripts = {
                "up": f"window.scrollBy(0, -{int(amount)});",
                "down": f"window.scrollBy(0, {int(amount)});",
                "left": f"window.scrollBy(-{int(amount)}, 0);",
                "right": f"window.scrollBy({int(amount)}, 0);",
                "top": "window.scrollTo(0,0);",
                "bottom": "window.scrollTo(0, document.body.scrollHeight);",
            }
            await _evaluate(rec.tab, scripts[direction])
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"direction": direction, "amount": amount}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()
@mcp.tool()
async def browser_screenshot(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    path_policy: str = "temp",
    name: str = "screenshot",
    full_page: bool = False,
    max_width: Optional[int] = None,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_screenshot session={session_id}")
    session = await manager.get_session(session_id)
    if session is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        try:
            rec = _find_tab(session, tab_id)
            root = session.tmp_dir / "artifacts"
            root.mkdir(parents=True, exist_ok=True)
            safe_name = f"{name}.png" if not str(name).lower().endswith('.png') else name
            if path_policy != "temp":
                p = Path(path_policy).expanduser()
                if p.is_dir():
                    path = p / safe_name
                else:
                    path = Path(path_policy)
            else:
                path = root / safe_name
            try:
                if full_page and hasattr(rec.tab, "get_screenshot"):
                    await _safe_call(rec.tab.get_screenshot, str(path), full_page=True)
                else:
                    await _safe_call(rec.tab.save_screenshot, str(path))
            except TypeError:
                await _safe_call(rec.tab.save_screenshot, str(path), full_page=full_page)
            if max_width is not None:
                pass
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"path": str(path)}).to_dict()
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id if "rec" in locals() else tab_id, ErrorCodes.INTERNAL, str(exc)).to_dict()


@mcp.tool()
async def page_get_content(ctx: Optional[Context], session_id: str, tab_id: Optional[str] = None, max_bytes: int = 200000) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: page_get_content session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        html = await _evaluate(rec.tab, "return document.documentElement.outerHTML;")
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"html": _truncate(html, max_bytes)}).to_dict()


@mcp.tool()
async def page_get_html(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    selector: Optional[str] = None,
    max_bytes: int = 50000
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: page_get_html session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        if selector:
            html = await _evaluate(rec.tab, f"const e = document.querySelector({_js_value(selector)}); return e ? e.outerHTML : '';")
            key = selector
        else:
            html = await _evaluate(rec.tab, "return document.documentElement.outerHTML;")
            key = "document"
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"html": _truncate(html, max_bytes), "selector": key}).to_dict()


@mcp.tool()
async def page_get_text(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    selector: Optional[str] = None,
    all: bool = False,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: page_get_text session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        if selector:
            if all:
                text = await _evaluate(
                    rec.tab,
                    f"const nodes = document.querySelectorAll({_js_value(selector)}); return Array.from(nodes).map(n => n.textContent || '').join('\\n');",
                )
            else:
                text = await _evaluate(rec.tab, f"const e = document.querySelector({_js_value(selector)}); return e ? (e.textContent || '') : '';")
        else:
            text = await _evaluate(rec.tab, "return document.body ? (document.body.textContent || '') : '';")
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"text": _truncate(text, 200000)}).to_dict()


@mcp.tool()
async def page_get_links(ctx: Optional[Context], session_id: str, tab_id: Optional[str] = None, absolute: bool = True) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: page_get_links session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        links = await _evaluate(
            rec.tab,
            "(() => { return Array.from(document.querySelectorAll('a[href]')).map(a => ({ text: (a.textContent || '').trim(), href: a.getAttribute('href'), absolute: (new URL(a.getAttribute('href'), window.location.href)).href })); })();",
        )
        if not absolute:
            links = [{"text": i.get("text"), "href": i.get("href")} for i in links]
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"links": links}).to_dict()
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"links": links}).to_dict()


@mcp.tool()
async def page_get_resources(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    only_visible: bool = False,
    as_json: bool = True,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: page_get_resources session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        resources = await _evaluate(
            rec.tab,
            'const visible = (el) => { const r = el.getBoundingClientRect(); return r.width > 0 && r.height > 0 };'
            "return {"
            "images: Array.from(document.images).map(i => ({url:i.currentSrc || i.src, visible: visible(i)})),"
            "scripts: Array.from(document.scripts).filter(s=>s.src).map(s => ({src:s.src})),"
            'styles: Array.from(document.querySelectorAll(\'link[rel="stylesheet"]\')).map(s => ({href:s.href})),'
            "};",
        )
        if only_visible and isinstance(resources, dict):
            resources["images"] = [i for i in resources.get("images", []) if i.get("visible")]
        if as_json:
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"resources": resources}).to_dict()
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"resources": _truncate(resources)}).to_dict()


@mcp.tool()
async def page_set_local_storage(ctx: Optional[Context], session_id: str, tab_id: Optional[str] = None, storage: dict[str, str] = None) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: page_set_local_storage session={session_id}")
    if storage is None:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "storage required").to_dict()
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        for k, v in storage.items():
            await _evaluate(rec.tab, f"localStorage.setItem({_js_value(str(k))}, {_js_value(str(v))});")
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"set_keys": list(storage.keys())}).to_dict()


@mcp.tool()
async def page_get_local_storage(ctx: Optional[Context], session_id: str, tab_id: Optional[str] = None) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: page_get_local_storage session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        data = await _evaluate(rec.tab, "const out={}; for (const k of Object.keys(localStorage)) { out[k]=localStorage.getItem(k); } return out;")
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"local_storage": data or {}}).to_dict()


@mcp.tool()
async def page_clear_local_storage(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    keys: Optional[list[str]] = None,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: page_clear_local_storage session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        if not keys:
            await _evaluate(rec.tab, "localStorage.clear();")
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"cleared": "all"}).to_dict()
        for key in keys:
            await _evaluate(rec.tab, f"localStorage.removeItem({_js_value(str(key))});")
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"cleared": keys}).to_dict()


@mcp.tool()
async def element_send_file(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    selector: str = "",
    file_paths: list[str] = None,
    timeout_ms: int = 10000,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: element_send_file session={session_id}")
    if not selector:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "selector required").to_dict()
    if not file_paths:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "file_paths required").to_dict()
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        element = await _select_element(rec.tab, selector, timeout_ms)
        if hasattr(element, "send_file"):
            try:
                await _safe_call(element.send_file, *file_paths)
            except TypeError:
                await _safe_call(element.send_file, file_paths)
            except Exception:
                await _safe_call(element.send_keys, "\n".join(file_paths))
        else:
            await _safe_call(element.send_keys, "\n".join(file_paths))
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"uploaded": file_paths}).to_dict()


@mcp.tool()
async def element_mouse_drag(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    selector_from: str = "",
    selector_to_or_xy: Union[str, dict[str, int]] = "",
    steps: int = 5,
    timeout_ms: int = 10000,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: element_mouse_drag session={session_id}")
    if not selector_from:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "selector_from required").to_dict()
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        src = await _select_element(rec.tab, selector_from, timeout_ms)
        if hasattr(src, "mouse_drag"):
            if isinstance(selector_to_or_xy, str):
                dst = await _select_element(rec.tab, selector_to_or_xy, timeout_ms)
                await _safe_call(src.mouse_drag, dst)
                return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"selector_from": selector_from, "selector_to": selector_to_or_xy}).to_dict()
            await _safe_call(src.mouse_drag, selector_to_or_xy)
            return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"selector_from": selector_from, "to": selector_to_or_xy}).to_dict()

        if isinstance(selector_to_or_xy, str):
            js = (
                f"const a = document.querySelector({_js_value(selector_from)});"
                f"const b = document.querySelector({_js_value(selector_to_or_xy)});"
                "if (!a || !b) throw new Error('element not found');"
                "const ra = a.getBoundingClientRect();"
                "const rb = b.getBoundingClientRect();"
                "const fromX = ra.left + ra.width / 2;"
                "const fromY = ra.top + ra.height / 2;"
                "const toX = rb.left + rb.width / 2;"
                "const toY = rb.top + rb.height / 2;"
                "a.dispatchEvent(new MouseEvent('mousedown', {clientX: fromX, clientY: fromY, bubbles: true}));"
                "a.dispatchEvent(new MouseEvent('mousemove', {clientX: toX, clientY: toY, bubbles: true}));"
                "b.dispatchEvent(new MouseEvent('mouseup', {clientX: toX, clientY: toY, bubbles: true}));"
                "return true;"
            )
        else:
            x = int(selector_to_or_xy.get("x", 0))
            y = int(selector_to_or_xy.get("y", 0))
            js = (
                f"const a = document.querySelector({_js_value(selector_from)});"
                "if (!a) throw new Error('element not found');"
                "const ra = a.getBoundingClientRect();"
                f"const toX = {x};"
                f"const toY = {y};"
                "const fromX = ra.left + ra.width / 2;"
                "const fromY = ra.top + ra.height / 2;"
                "a.dispatchEvent(new MouseEvent('mousedown', {clientX: fromX, clientY: fromY, bubbles: true}));"
                "a.dispatchEvent(new MouseEvent('mousemove', {clientX: toX, clientY: toY, bubbles: true}));"
                "a.dispatchEvent(new MouseEvent('mouseup', {clientX: toX, clientY: toY, bubbles: true}));"
                "return true;"
            )
        await _evaluate(rec.tab, js)
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"selector_from": selector_from, "selector_to_or_xy": selector_to_or_xy, "steps": steps}).to_dict()

@mcp.tool()
async def cdp_call(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    domain: str = "",
    method: str = "",
    params: Optional[dict[str, Any]] = None,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: cdp_call session={session_id} {domain}.{method}")
    if not domain:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "domain required").to_dict()
    if not method:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "method required").to_dict()
    if not _is_allowed_domain(domain):
        return ToolResult.fail(session_id, tab_id, ErrorCodes.CTP_DENIED, "domain not allowed").to_dict()
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        try:
            result = await _cdp_call(rec.tab, domain, method, params)
        except Exception as exc:
            return ToolResult.fail(session_id, rec.tab_id, _cdp_error_code(exc), str(exc)).to_dict()
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"result": _serialize_result(result)}).to_dict()


@mcp.tool()
async def cdp_subscribe(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    event_type_or_module: str = "",
    limit: int = DEFAULT_EVENT_LIMIT,
    max_bytes: int = MAX_EVENT_BYTES,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: cdp_subscribe session={session_id}")
    if not event_type_or_module:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.INVALID_INPUT, "event required").to_dict()
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        events = [event_type_or_module]
        if event_type_or_module in CDP_MODULE_EVENTS:
            events = list(CDP_MODULE_EVENTS[event_type_or_module])
            if event_type_or_module in ALLOWED_CDP_DOMAINS:
                try:
                    await _cdp_call(rec.tab, event_type_or_module, "enable", {})
                except Exception as exc:
                    return ToolResult.fail(session_id, rec.tab_id, _cdp_error_code(exc), str(exc)).to_dict()
        session.event_buffer = deque(session.event_buffer, maxlen=max(limit, 10))
        add_handler = getattr(rec.tab, "add_handler", None)
        if not callable(add_handler):
            return ToolResult.fail(session_id, rec.tab_id, ErrorCodes.INTERNAL, "add_handler not available").to_dict()
        created = []
        for ev in events:
            handler = _make_handler(session, ev, max_bytes)
            add_handler(ev, handler)
            sid = str(uuid.uuid4())
            session.subscriptions[sid] = {"tab": rec.tab, "event": ev, "callback": handler}
            created.append(sid)
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"subscription_ids": created, "events": events}).to_dict()


@mcp.tool()
async def cdp_unsubscribe(ctx: Optional[Context], session_id: str, subscription_id: str) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: cdp_unsubscribe session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, None, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        info = session.subscriptions.pop(subscription_id, None)
        if not info:
            return ToolResult.fail(session_id, None, ErrorCodes.INVALID_INPUT, "subscription not found").to_dict()
        _remove_handler(info)
        return ToolResult(ok=True, session_id=session_id, data={"subscription_id": subscription_id}).to_dict()


@mcp.tool()
async def network_capture_start(
    ctx: Optional[Context],
    session_id: str,
    tab_id: Optional[str] = None,
    include_headers: bool = True,
    include_body: bool = False,
    max_events: int = 2000,
) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: network_capture_start session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        if session.network_capture_active:
            return ToolResult.fail(session_id, tab_id, ErrorCodes.INTERNAL, "network capture already active").to_dict()
        rec = _find_tab(session, tab_id)
        session.network_capture_active = True
        session.network_capture_settings = {
            "include_headers": include_headers,
            "include_body": include_body,
            "max_events": max_events,
        }
        session.event_buffer = deque(session.event_buffer, maxlen=max_events)
        try:
            await _cdp_call(rec.tab, "Network", "enable", {})
        except Exception as exc:
            session.network_capture_active = False
            return ToolResult.fail(session_id, rec.tab_id, _cdp_error_code(exc), str(exc)).to_dict()
        ids = []
        for ev in CDP_MODULE_EVENTS["Network"]:
            handler = _make_handler(session, ev, MAX_EVENT_BYTES)
            rec.tab.add_handler(ev, handler)
            sid = str(uuid.uuid4())
            session.subscriptions[sid] = {
                "tab": rec.tab,
                "event": ev,
                "callback": handler,
                "kind": "network_capture",
            }
            ids.append(sid)
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"subscription_ids": ids, "active": True}).to_dict()


@mcp.tool()
async def network_capture_stop(ctx: Optional[Context], session_id: str, tab_id: Optional[str] = None) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: network_capture_stop session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        stopped = []
        for sid, info in list(session.subscriptions.items()):
            if info.get("kind") == "network_capture":
                _remove_handler(info)
                session.subscriptions.pop(sid, None)
                stopped.append(sid)
        session.network_capture_active = False
        try:
            await _cdp_call(rec.tab, "Network", "disable", {})
        except Exception:
            return ToolResult.fail(session_id, rec.tab_id, ErrorCodes.INTERNAL, "failed to disable network domain").to_dict()
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"stopped": stopped, "active": False}).to_dict()


@mcp.tool()
async def browser_capture_console(ctx: Optional[Context], session_id: str, tab_id: Optional[str] = None, level: str = "warning") -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_capture_console session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, tab_id, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        rec = _find_tab(session, tab_id)
        entries = [e for e in session.event_buffer if "Log" in str(e.get("event", ""))]
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"entries": entries, "count": len(entries), "level": level}).to_dict()


@mcp.tool()
async def browser_healthcheck(ctx: Optional[Context], session_id: str) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_healthcheck session={session_id}")
    session = await manager.get_session(session_id)
    if not session:
        return ToolResult.fail(session_id, None, ErrorCodes.SESSION_NOT_FOUND, "session not found").to_dict()
    async with session.lock:
        if not session.tabs:
            return ToolResult.fail(session_id, None, ErrorCodes.TAB_NOT_FOUND, "no tab").to_dict()
        rec = _find_tab(session, None)
        await _evaluate(rec.tab, "return 1+1;")
        return ToolResult(ok=True, session_id=session_id, tab_id=rec.tab_id, data={"summary": _session_summary(session)}).to_dict()


@mcp.tool()
async def browser_cleanup_stale(ctx: Optional[Context], ttl_seconds: int = 300) -> dict[str, Any]:
    await _ctx_info(ctx, "info", f"tool: browser_cleanup_stale ttl_seconds={ttl_seconds}")
    return (await manager.cleanup_stale(ttl_seconds)).to_dict()


@mcp.resource("resource://sessions/{session_id}/tabs")
async def resource_sessions_tabs(session_id: str) -> str:
    session = await manager.get_session(session_id)
    if not session:
        return json.dumps({"ok": False, "error": "session not found"}, ensure_ascii=False)
    async with session.lock:
        return json.dumps(
            {
                "ok": True,
                "session": _session_summary(session),
                "tabs": {
                    rec.tab_id: {
                        "active": rec.is_active,
                        "created_at": _to_iso(rec.created_at),
                        "last_used_at": _to_iso(rec.last_used_at),
                    }
                    for rec in session.tabs.values()
                },
            },
            ensure_ascii=False,
        )


@mcp.resource("resource://sessions/{session_id}/cookies")
async def resource_sessions_cookies(session_id: str) -> str:
    session = await manager.get_session(session_id)
    if not session:
        return json.dumps({"ok": False, "error": "session not found"}, ensure_ascii=False)
    async with session.lock:
        rec = _find_tab(session, None)
        raw_cookie = await _evaluate(rec.tab, "return document.cookie || '';")
        cookies: dict[str, str] = {}
        if isinstance(raw_cookie, str):
            for part in raw_cookie.split(";"):
                if "=" in part:
                    k, v = part.split("=", 1)
                    cookies[k.strip()] = v.strip()
        return json.dumps({"ok": True, "cookies": cookies}, ensure_ascii=False)


@mcp.resource("resource://sessions/{session_id}/network_events")
async def resource_sessions_network_events(session_id: str) -> str:
    session = await manager.get_session(session_id)
    if not session:
        return json.dumps({"ok": False, "error": "session not found"}, ensure_ascii=False)
    async with session.lock:
        return json.dumps({"ok": True, "events": list(session.event_buffer)}, ensure_ascii=False)


@mcp.prompt()
def crawl_plan(objective: str, target_url: str, constraints: Optional[str] = None) -> str:
    lines = ["Crawl Plan", f"Objective: {objective}", f"Target URL: {target_url}"]
    if constraints:
        lines.append(f"Constraints: {constraints}")
    lines.extend(
        [
            "1) Start session with safe profile and timeout policy.",
            "2) Navigate and wait until DOM ready.",
            "3) Extract links and key fields.",
            "4) Handle pagination with safe guards.",
            "5) Output normalized structured JSON.",
        ]
    )
    return "\n".join(lines)


@mcp.prompt()
def extract_plan(page_type: str, fields: list[str] | str) -> str:
    if isinstance(fields, list):
        fields_text = ", ".join(fields)
    else:
        fields_text = fields
    return (
        f"Extraction plan for {page_type}:\n"
        f"Fields: {fields_text}\n"
        "- Use stable selectors and fallback selector sets.\n"
        "- Validate each row before returning."
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s - %(levelname)s - %(message)s")
    logger.info("Starting nodriver MCP server")
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
