"""
🌐 Web sources: websites the admin connects for the bot to read from.

How it works
------------
1. The admin opens a website in a real browser running on the server
   (headless Chromium via Playwright). The mini app shows it as a live
   screenshot: a tap on the picture is a click at that spot, and typing,
   keys, scrolling, back and reload are buttons. That is enough to get
   through an ordinary login page.

2. "Save" keeps the browser's cookies and local storage (Playwright's
   storage state), so the bot can come back already logged in. It also
   keeps the steps taken since the recording started -- every page opened,
   every click (as a CSS selector of the element tapped, with the
   coordinates as a fallback), every key and every piece of typed text --
   so when the saved session expires the bot replays them to log in again
   by itself. Text typed with "type secret" (a password) is stored
   encrypted (Fernet; key from WEB_SOURCES_KEY, else derived from the bot
   token) and never sent back to the mini app.

3. The admin then tells the bot where to look: a search URL with {query}
   in it, which links on the results page are results (a piece of text or
   a regular expression that their address contains), and how many to
   read. A source switched on appears under "Choose resources" in Ask your
   books; a question then searches the site, reads the top results, and
   the passages are cited like book pages, with a link.

Safety: the server-side browser refuses addresses on the server's own
network (localhost, private IP ranges, *.internal), for every request a
page makes, not just the first one. Only admins reach the browser.
"""

import asyncio
import base64
import concurrent.futures
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
import socket
import tempfile
import threading
import time
import urllib.parse

from cryptography.fernet import Fernet, InvalidToken

from config import STORAGE_DIR, TELEGRAM_BOT_TOKEN

logger = logging.getLogger(__name__)

_DIR = os.path.join(STORAGE_DIR, "web_sources")
_SITES_PATH = os.path.join(_DIR, "sites.json")

VIEWPORT = {"width": 390, "height": 740}
_SCALE = 2
_USER_AGENT = (
    "Mozilla/5.0 (Linux; Android 14; Pixel 8) AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/131.0.0.0 Mobile Safari/537.36"
)
_NAV_TIMEOUT_MS = 25000
_STEP_TIMEOUT_MS = 6000
_LIVE_IDLE_SECONDS = 600       # an abandoned live browser closes after this
_BROWSER_IDLE_SECONDS = 900    # and Chromium itself after this with nothing to do
MAX_RESULTS = 5
_PAGE_TEXT_LIMIT = 15000
_CHUNK_CHARS = 1400
_MAX_CHUNKS_PER_SITE = 24
_ALLOWED_KEYS = {"Enter", "Tab", "Backspace", "Escape", "ArrowDown", "ArrowUp", "ArrowLeft", "ArrowRight"}


class WebSourceError(Exception):
    pass


# ---------------------------------------------------------------------
# Registry (sites.json) and saved sessions (<id>.state.json)
# ---------------------------------------------------------------------

_lock = threading.Lock()


def _load(strict: bool = False) -> dict:
    """
    The registry. A missing file is an empty one; any other read or parse
    error raises when strict -- the callers that write the file back use
    strict, so a transient error can't turn into "save only the new site".
    """
    try:
        with open(_SITES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except (OSError, ValueError):
        if strict:
            logger.exception("Couldn't read the web sources registry")
            raise WebSourceError("The saved websites couldn't be read just now. Please try again.")
        return {}


def _write_json(path: str, data, private: bool = False) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        if private:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise


def _state_path(site_id: str) -> str:
    return os.path.join(_DIR, f"{site_id}.state.json")


def valid_id(site_id) -> bool:
    return isinstance(site_id, str) and re.fullmatch(r"ws[0-9a-f]{10}", site_id) is not None


def get_site(site_id: str) -> dict | None:
    if not valid_id(site_id):
        return None
    return _load().get(site_id)


def _public(site_id: str, site: dict) -> dict:
    steps = site.get("login_steps") or []
    return {
        "site_id": site_id,
        "name": site.get("name"),
        "start_url": site.get("start_url"),
        "search_url": site.get("search_url") or "",
        "link_pattern": site.get("link_pattern") or "",
        "max_results": site.get("max_results") or 3,
        "enabled": bool(site.get("enabled")),
        "login_steps": len(steps),
        "secret_steps": sum(1 for s in steps if s.get("secret")),
        "has_session": os.path.exists(_state_path(site_id)),
        "last_login": site.get("last_login"),
        "last_login_ok": site.get("last_login_ok"),
        "last_error": site.get("last_error"),
        "updated": site.get("updated"),
    }


def list_sites() -> list[dict]:
    sites = _load()
    out = [_public(sid, s) for sid, s in sites.items()]
    out.sort(key=lambda s: (s["name"] or "").lower())
    return out


def ask_sources() -> list[dict]:
    """Switched-on sources with a search address: what users can tick in Drug LookUp."""
    return [
        {"book_id": f"web:{s['site_id']}", "title": s["name"], "qa_indexed": True, "web": True}
        for s in list_sites()
        if s["enabled"] and s["search_url"]
    ]


def update_site(site_id: str, fields: dict) -> dict:
    """Admin edits: name, search_url, link_pattern, max_results, enabled. Validated here."""
    with _lock:
        sites = _load(strict=True)
        site = sites.get(site_id)
        if site is None:
            raise WebSourceError("That website isn't saved anymore.")
        if "name" in fields:
            name = str(fields["name"] or "").strip()[:80]
            if not name:
                raise WebSourceError("Give the website a name.")
            site["name"] = name
        if "search_url" in fields:
            url = str(fields["search_url"] or "").strip()
            if url:
                if "{query}" not in url:
                    raise WebSourceError("The search address needs {query} where the search words go.")
                check_url(url.replace("{query}", "test"))
            site["search_url"] = url
        if "link_pattern" in fields:
            pattern = str(fields["link_pattern"] or "").strip()[:300]
            site["link_pattern"] = pattern
        if "max_results" in fields:
            try:
                n = int(fields["max_results"])
            except (TypeError, ValueError):
                raise WebSourceError("Number of results must be a number.")
            site["max_results"] = max(1, min(MAX_RESULTS, n))
        if "enabled" in fields:
            site["enabled"] = bool(fields["enabled"])
            if site["enabled"] and not site.get("search_url"):
                raise WebSourceError("Set the search address before switching it on for Drug LookUp.")
        site["updated"] = int(time.time())
        sites[site_id] = site
        _write_json(_SITES_PATH, sites)
        return _public(site_id, site)


def delete_site(site_id: str) -> None:
    with _lock:
        sites = _load(strict=True)
        if sites.pop(site_id, None) is None:
            raise WebSourceError("That website isn't saved anymore.")
        _write_json(_SITES_PATH, sites)
    try:
        os.remove(_state_path(site_id))
    except OSError:
        pass


def _set_login_result(site_id: str, ok: bool, error: str | None = None) -> None:
    with _lock:
        sites = _load(strict=True)
        site = sites.get(site_id)
        if site is None:
            return
        site["last_login"] = int(time.time())
        site["last_login_ok"] = ok
        site["last_error"] = error
        _write_json(_SITES_PATH, sites)


def _load_state(site_id: str) -> dict | None:
    try:
        with open(_state_path(site_id), encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _save_state(site_id: str, state: dict) -> None:
    _write_json(_state_path(site_id), state, private=True)


# ---------------------------------------------------------------------
# Secrets (passwords typed while recording)
# ---------------------------------------------------------------------

def _fernet() -> Fernet:
    raw = os.environ.get("WEB_SOURCES_KEY") or ("web-sources:" + TELEGRAM_BOT_TOKEN)
    return Fernet(base64.urlsafe_b64encode(hashlib.sha256(raw.encode("utf-8")).digest()))


def _encrypt(text: str) -> str:
    return _fernet().encrypt(text.encode("utf-8")).decode("ascii")


def _decrypt(token: str) -> str:
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        raise WebSourceError("A saved password can't be read anymore (the key changed). Log in again and save.")


# ---------------------------------------------------------------------
# Address checks (no reaching the server's own network)
# ---------------------------------------------------------------------

_ALLOW_LOCAL = os.environ.get("WEB_SOURCES_ALLOW_LOCAL") == "1"  # tests only


def _blocked_host(host: str) -> bool:
    if _ALLOW_LOCAL:
        return False
    host = (host or "").strip("[]").lower().rstrip(".")
    if not host or host == "localhost" or host.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        return not ipaddress.ip_address(host).is_global
    except ValueError:
        return False


def check_url(url: str) -> str:
    url = (url or "").strip()
    if url and "://" not in url:
        url = "https://" + url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise WebSourceError("That isn't a website address.")
    if _blocked_host(parts.hostname):
        raise WebSourceError("That address isn't allowed.")
    return url


def _ip_allowed(ip_text: str) -> bool:
    try:
        ip = ipaddress.ip_address(ip_text.split("%", 1)[0])
    except ValueError:
        return False
    if ip.version == 6 and ip.ipv4_mapped:
        ip = ip.ipv4_mapped
    return ip.is_global


# ---------------------------------------------------------------------
# Egress proxy: every connection the browser makes goes through here
# ---------------------------------------------------------------------
# The hostname check above only sees names. A public name can resolve to
# a private address, and a page can redirect (or load a sub-resource) to
# one -- Playwright's request routing does not see redirects. So Chromium
# is started with this in-process proxy as its only way out: it resolves
# each host itself, refuses the connection unless every address is a
# public one, and connects to the exact address it checked (so a second,
# different DNS answer can't be slipped in between check and connect).

_PROXY_PORTS = {80, 443, 8080, 8443}
_DNS_POOL = concurrent.futures.ThreadPoolExecutor(max_workers=4, thread_name_prefix="web-dns")
_PROXY_CONNECT_TIMEOUT = 10
_PROXY_HEAD_LIMIT = 64 * 1024


async def _resolve_public(host: str, port: int) -> str:
    host = host.strip("[]")
    if _ALLOW_LOCAL:
        return host
    if _blocked_host(host) or port not in _PROXY_PORTS:
        raise PermissionError(f"{host}:{port} not allowed")
    # Its own small thread pool and a timeout: on the default pool, a page
    # making many slow lookups stalled every AI call in the bot (they share it).
    loop = asyncio.get_running_loop()
    try:
        infos = await asyncio.wait_for(
            loop.run_in_executor(_DNS_POOL, socket.getaddrinfo, host, port, 0, socket.SOCK_STREAM), timeout=5
        )
    except asyncio.TimeoutError:
        raise PermissionError(f"{host}: DNS lookup timed out")
    addrs = [info[4][0] for info in infos]
    if not addrs or not all(_ip_allowed(a) for a in addrs):
        raise PermissionError(f"{host} resolves to a non-public address")
    return addrs[0]


async def _pipe(reader, writer) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except Exception:
        pass
    finally:
        try:
            writer.close()
        except Exception:
            pass


async def _proxy_client(client_reader, client_writer) -> None:
    upstream_writer = None
    try:
        head = await asyncio.wait_for(client_reader.readuntil(b"\r\n\r\n"), timeout=30)
        if len(head) > _PROXY_HEAD_LIMIT:
            raise ValueError("request head too large")
        lines = head.decode("latin-1").split("\r\n")
        method, target, version = lines[0].split(" ", 2)
        if method.upper() == "CONNECT":
            host, _, port = target.rpartition(":")
            port = int(port)
            ip = await _resolve_public(host, port)
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=_PROXY_CONNECT_TIMEOUT
            )
            client_writer.write(b"HTTP/1.1 200 Connection established\r\n\r\n")
            await client_writer.drain()
        else:
            parts = urllib.parse.urlsplit(target)
            if parts.scheme != "http" or not parts.hostname:
                raise ValueError("unsupported request")
            port = parts.port or 80
            ip = await _resolve_public(parts.hostname, port)
            upstream_reader, upstream_writer = await asyncio.wait_for(
                asyncio.open_connection(ip, port), timeout=_PROXY_CONNECT_TIMEOUT
            )
            path = urllib.parse.urlunsplit(("", "", parts.path or "/", parts.query, ""))
            headers = [
                ln for ln in lines[1:]
                if ln and not ln.lower().startswith(("proxy-connection:", "proxy-authorization:", "connection:"))
            ]
            upstream_writer.write(
                (f"{method} {path} {version}\r\n" + "\r\n".join(headers + ["Connection: close"]) + "\r\n\r\n")
                .encode("latin-1")
            )
            await upstream_writer.drain()
        await asyncio.gather(_pipe(client_reader, upstream_writer), _pipe(upstream_reader, client_writer))
    except Exception as e:
        logger.info("Web browser connection refused: %s", e)
        try:
            client_writer.write(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\nConnection: close\r\n\r\n")
            await client_writer.drain()
        except Exception:
            pass
    finally:
        for w in (client_writer, upstream_writer):
            if w is not None:
                try:
                    w.close()
                except Exception:
                    pass


_proxy_server = None


async def _proxy_address() -> str:
    global _proxy_server
    if _proxy_server is None:
        _proxy_server = await asyncio.start_server(_proxy_client, "127.0.0.1", 0)
    port = _proxy_server.sockets[0].getsockname()[1]
    return f"http://127.0.0.1:{port}"


async def _guard(route) -> None:
    url = route.request.url
    parts = urllib.parse.urlsplit(url)
    # data:/blob: are the page's own content; anything else that isn't
    # web traffic (file:, chrome:, ftp: ...) has no business here.
    if parts.scheme in ("data", "blob"):
        await route.continue_()
    elif parts.scheme not in ("http", "https", "ws", "wss") or _blocked_host(parts.hostname or ""):
        await route.abort()
    else:
        await route.continue_()


# ---------------------------------------------------------------------
# The browser
# ---------------------------------------------------------------------

_pw = None
_browser = None
_browser_lock = asyncio.Lock()
_last_activity = 0.0
_watcher: asyncio.Task | None = None


def _executable() -> str | None:
    return (
        os.environ.get("WEB_BROWSER_EXECUTABLE")
        or shutil.which("chromium")
        or shutil.which("chromium-browser")
        or None  # Playwright's own download, if there is one
    )


async def _get_browser():
    global _pw, _browser
    async with _browser_lock:
        if _browser is not None and _browser.is_connected():
            return _browser
        try:
            from playwright.async_api import async_playwright
        except ImportError:
            raise WebSourceError("The web browser isn't installed on the server.")
        if _pw is None:
            _pw = await async_playwright().start()
        try:
            _browser = await _pw.chromium.launch(
                executable_path=_executable(),
                headless=True,
                args=[
                    "--no-sandbox", "--disable-dev-shm-usage",
                    # WebRTC's UDP doesn't go through a proxy; this keeps it off.
                    "--webrtc-ip-handling-policy=disable_non_proxied_udp",
                    "--disable-background-networking",
                ],
                # "<-loopback>" removes Chromium's built-in exemption, so even
                # localhost goes through the proxy (and is refused there).
                proxy={"server": await _proxy_address(), "bypass": "<-loopback>"},
            )
        except Exception as e:
            logger.exception("Couldn't start Chromium")
            raise WebSourceError(f"The web browser couldn't start on the server ({type(e).__name__}).")
        _ensure_watcher()
        return _browser


async def _close_browser() -> None:
    global _pw, _browser
    async with _browser_lock:
        if _browser is not None:
            try:
                await _browser.close()
            except Exception:
                pass
        if _pw is not None:
            try:
                await _pw.stop()
            except Exception:
                pass
        _browser = _pw = None


def _touch() -> None:
    global _last_activity
    _last_activity = time.monotonic()


def _ensure_watcher() -> None:
    global _watcher
    if _watcher is None or _watcher.done():
        _watcher = asyncio.get_running_loop().create_task(_watch_idle())


async def _watch_idle() -> None:
    """Close an abandoned live browser, then Chromium itself, so they don't hold memory for nothing."""
    while True:
        await asyncio.sleep(60)
        idle = time.monotonic() - _last_activity
        if _live is not None and idle > _LIVE_IDLE_SECONDS:
            logger.info("Closing idle live web browser")
            await live_close()
        if _live is None and _fetches_running == 0 and idle > _BROWSER_IDLE_SECONDS and _browser is not None:
            logger.info("Closing idle Chromium")
            await _close_browser()
            return


async def _new_context(state: dict | None = None, text_only: bool = False):
    browser = await _get_browser()
    ctx = await browser.new_context(
        viewport=VIEWPORT,
        device_scale_factor=_SCALE,
        is_mobile=True,
        has_touch=True,
        user_agent=_USER_AGENT,
        locale="en-US",
        storage_state=state,
        accept_downloads=False,
    )
    ctx.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
    ctx.set_default_timeout(_STEP_TIMEOUT_MS)
    await ctx.route("**/*", _guard_text_only if text_only else _guard)
    return ctx


async def _guard_text_only(route) -> None:
    """For searches: the text is all that's read, so pictures, media and fonts aren't fetched."""
    if route.request.resource_type in ("image", "media", "font"):
        await route.abort()
    else:
        await _guard(route)


async def _settle(page, quick: bool = False) -> None:
    try:
        await page.wait_for_load_state("domcontentloaded", timeout=8000)
    except Exception:
        pass
    await asyncio.sleep(0.4 if quick else 0.9)


async def _goto(page, url: str) -> None:
    url = check_url(url)
    try:
        await page.goto(url, wait_until="domcontentloaded")
    except Exception as e:
        raise WebSourceError(f"Couldn't open {urllib.parse.urlsplit(url).hostname}: {type(e).__name__}.")
    await _settle(page)


# A CSS selector for the element at a point (or the focused one), so a
# recorded click can be replayed even when the layout shifts. Prefers an
# id, then a distinctive attribute, then a path of tags.
_JS_PATH = r"""
function cssPath(e) {
  if (!e || e.nodeType !== 1) return null;
  const uniq = (s) => { try { return document.querySelectorAll(s).length === 1; } catch (_) { return false; } };
  if (e.id && uniq('#' + CSS.escape(e.id))) return '#' + CSS.escape(e.id);
  const tag = e.tagName.toLowerCase();
  for (const a of ['name', 'data-testid', 'aria-label', 'placeholder', 'type', 'value']) {
    const v = e.getAttribute(a);
    if (v && v.length < 80) {
      const s = tag + '[' + a + '=' + JSON.stringify(v) + ']';
      if (uniq(s)) return s;
    }
  }
  const parts = [];
  let cur = e;
  while (cur && cur.nodeType === 1 && cur !== document.documentElement) {
    if (cur.id && uniq('#' + CSS.escape(cur.id))) { parts.unshift('#' + CSS.escape(cur.id)); break; }
    let p = cur.tagName.toLowerCase();
    const parent = cur.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter((c) => c.tagName === cur.tagName);
      if (same.length > 1) p += ':nth-of-type(' + (same.indexOf(cur) + 1) + ')';
    }
    parts.unshift(p);
    cur = parent;
  }
  return parts.join(' > ');
}
"""
_JS_AT_POINT = "([x, y]) => {" + _JS_PATH + """
  let el = document.elementFromPoint(x, y);
  if (!el) return null;
  el = el.closest('a,button,input,select,textarea,label,summary,[role=button],[role=link],[onclick]') || el;
  return cssPath(el);
}"""
_JS_ACTIVE = "() => {" + _JS_PATH + """
  const el = document.activeElement;
  return el && el !== document.body ? cssPath(el) : null;
}"""


# What has focus: whether the keyboard should open in the mini app, and
# hide what is typed (a password field).
_JS_FOCUS = """() => {
  const el = document.activeElement;
  if (!el || el === document.body) return {editable: false};
  const tag = el.tagName.toLowerCase();
  const type = (el.getAttribute('type') || '').toLowerCase();
  const textInput = tag === 'input' && !['button', 'submit', 'reset', 'checkbox', 'radio', 'file', 'image', 'range', 'color', 'hidden'].includes(type);
  const editable = (textInput || tag === 'textarea' || el.isContentEditable) && !el.disabled && !el.readOnly;
  const label = (el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('name') || '').slice(0, 60);
  return {editable: !!editable, password: type === 'password', label};
}"""


_JS_SCROLL = """([x, y, dx, dy]) => {
  let el = document.elementFromPoint(x, y);
  while (el && el !== document.body && el !== document.documentElement) {
    const st = getComputedStyle(el);
    const canY = dy && /(auto|scroll|overlay)/.test(st.overflowY) && el.scrollHeight > el.clientHeight + 1;
    const canX = dx && /(auto|scroll|overlay)/.test(st.overflowX) && el.scrollWidth > el.clientWidth + 1;
    if (canY || canX) {
      const before = [el.scrollLeft, el.scrollTop];
      el.scrollBy({left: dx, top: dy, behavior: 'instant'});
      if (el.scrollLeft !== before[0] || el.scrollTop !== before[1]) return;  // at its end: let the page scroll
    }
    el = el.parentElement;
  }
  window.scrollBy({left: dx, top: dy, behavior: 'instant'});
}"""


async def _tap(page, x: float, y: float) -> None:
    try:
        await page.touchscreen.tap(x, y)
    except Exception:
        await page.mouse.click(x, y)  # a context without touch support


async def _page_text(page) -> str:
    try:
        text = await page.evaluate(
            """() => {
              const root = document.querySelector('main, article, [role=main], #main, #content') || document.body;
              return root ? root.innerText.slice(0, 40000) : '';
            }"""
        )
    except Exception:
        text = ""
    lines = [re.sub(r"[ \t]+", " ", ln).strip() for ln in (text or "").splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text[:_PAGE_TEXT_LIMIT]


async def _looks_logged_out(page) -> bool:
    """A visible password box on the page we wanted is the sign of a lost session."""
    try:
        box = page.locator("input[type=password]").first
        return await box.count() > 0 and await box.is_visible()
    except Exception:
        return False


async def self_test() -> None:
    """Start Chromium and draw a page: the startup check that the browser works on this server."""
    _touch()
    ctx = await _new_context(None)
    try:
        page = await ctx.new_page()
        await page.set_content("<p>ok</p>")
        await page.screenshot(type="jpeg", quality=30)
    finally:
        await ctx.close()


# ---------------------------------------------------------------------
# The admin's live browser
# ---------------------------------------------------------------------

class _Live:
    def __init__(self, ctx, page, site_id: str | None, start_url: str):
        self.ctx = ctx
        self.page = page
        self.site_id = site_id
        self.start_url = start_url
        self.steps: list[dict] = [{"a": "goto", "url": start_url}]
        self.lock = asyncio.Lock()
        self.had_state = False
        self.seen: set[int] = {id(page)}  # tabs the admin has looked at (the others show as new)

    def tabs(self) -> list:
        return [pg for pg in self.ctx.pages if not pg.is_closed()]


_live: _Live | None = None


def live_info() -> dict | None:
    if _live is None:
        return None
    return {"site_id": _live.site_id, "steps": len(_live.steps)}


_live_switch = asyncio.Lock()


async def live_start(url: str, site_id: str | None = None) -> dict:
    global _live
    url = check_url(url)
    if site_id is not None and get_site(site_id) is None:
        raise WebSourceError("That website isn't saved anymore.")
    async with _live_switch:  # two quick opens would otherwise leak a browser context
        await live_close()
        _touch()
        state = _load_state(site_id) if site_id else None
        ctx = await _new_context(state)
        page = await ctx.new_page()
        live = _Live(ctx, page, site_id, url)
        live.had_state = state is not None

        def on_close(closed_page):
            # A sign-in popup that closes itself: back to a page still open.
            if live.page is closed_page:
                open_pages = [pg for pg in ctx.pages if not pg.is_closed()]
                if open_pages:
                    live.page = open_pages[-1]

        def on_page(new_page):
            # A link that opens a new tab ("Sign in with ...") does not take
            # over the view: it shows up in the tab bar, marked new, and the
            # admin switches to it with a tap.
            new_page.on("close", on_close)

        page.on("close", on_close)
        ctx.on("page", on_page)
        _live = live
    try:
        await _goto(page, url)
    except WebSourceError:
        pass  # still show what is there (an error page), so the admin can try another address
    return await live_frame()


async def live_frame() -> dict:
    if _live is None:
        raise WebSourceError("The browser is closed. Open a website first.")
    _touch()
    page = _live.page
    try:
        shot = await page.screenshot(type="jpeg", quality=62, timeout=15000)
    except Exception as e:
        raise WebSourceError(f"Couldn't take a picture of the page ({type(e).__name__}).")
    try:
        title = await page.title()
    except Exception:
        title = ""
    _live.seen.add(id(page))
    try:
        focus = await asyncio.wait_for(page.evaluate(_JS_FOCUS), 2)
    except Exception:
        focus = {"editable": False}
    tabs = []
    for i, pg in enumerate(_live.tabs()):
        try:
            tab_title = title if pg is page else await asyncio.wait_for(pg.title(), 2)
        except Exception:
            tab_title = ""
        tabs.append({
            "i": i,
            "title": (tab_title or urllib.parse.urlsplit(pg.url).hostname or pg.url or "New tab")[:60],
            "active": pg is page,
            "new": id(pg) not in _live.seen,
        })
    return {
        "image": base64.b64encode(shot).decode("ascii"),
        "url": page.url,
        "title": title,
        "tabs": tabs,
        "focus": focus,
        "width": VIEWPORT["width"],
        "height": VIEWPORT["height"],
        "steps": len(_live.steps),
        "site_id": _live.site_id,
    }


async def live_action(action: dict) -> dict:
    if _live is None:
        raise WebSourceError("The browser is closed. Open a website first.")
    _touch()
    kind = action.get("type")
    async with _live.lock:
        page = _live.page
        if kind in ("tab", "close_tab"):
            tabs = _live.tabs()
            i = action.get("i")
            if not isinstance(i, int) or isinstance(i, bool) or not 0 <= i < len(tabs):
                raise WebSourceError("That tab is already closed.")
            if kind == "tab":
                _live.page = tabs[i]
                try:
                    await tabs[i].bring_to_front()
                except Exception:
                    pass
            else:
                if len(tabs) == 1:
                    raise WebSourceError("That's the only tab open.")
                await tabs[i].close()  # on_close moves the view to another tab if this one was showing
            _live.steps.append({"a": kind, "i": i})
            return await live_frame()
        if kind == "click":
            try:
                x = max(0, min(VIEWPORT["width"], float(action["x"])))
                y = max(0, min(VIEWPORT["height"], float(action["y"])))
            except (KeyError, TypeError, ValueError):
                raise WebSourceError("Invalid tap position.")
            try:
                selector = await page.evaluate(_JS_AT_POINT, [x, y])
            except Exception:
                selector = None
            # A finger tap, as on a phone (this browser is a phone-sized one):
            # touch events, then the click a phone makes from them. A mouse
            # click missed controls that only listen for touches.
            await _tap(page, x, y)
            _live.steps.append({"a": "click", "sel": selector, "x": x, "y": y})
            await _settle(page)
        elif kind == "type":
            text = action.get("text")
            if not isinstance(text, str) or not text or len(text) > 500:
                raise WebSourceError("Type some text first (up to 500 characters).")
            await page.keyboard.type(text, delay=15)
            try:
                selector = await page.evaluate(_JS_ACTIVE)
            except Exception:
                selector = None
            step = {"a": "type", "sel": selector}
            try:
                in_password_field = await page.evaluate(
                    "() => !!document.activeElement && document.activeElement.type === 'password'"
                )
            except Exception:
                in_password_field = False
            if action.get("secret") or in_password_field:  # a password is never stored in plain text
                step["secret"] = _encrypt(text)
            else:
                step["text"] = text
            _live.steps.append(step)
            await _settle(page, quick=True)
        elif kind == "edit":
            try:
                backspaces = int(action.get("backspaces") or 0)
            except (TypeError, ValueError):
                backspaces = -1
            text = action.get("text") or ""
            if not 0 <= backspaces <= 500 or not isinstance(text, str) or len(text) > 500 or not (backspaces or text):
                raise WebSourceError("Invalid typing.")
            for _ in range(backspaces):
                await page.keyboard.press("Backspace")
            if text:
                await page.keyboard.type(text, delay=10)
            try:
                selector = await page.evaluate(_JS_ACTIVE)
                in_password_field = await page.evaluate(
                    "() => !!document.activeElement && document.activeElement.type === 'password'"
                )
            except Exception:
                selector, in_password_field = None, False
            _record_edit(_live.steps, selector, backspaces, text, bool(action.get("secret") or in_password_field))
            await _settle(page, quick=True)
        elif kind == "key":
            key = action.get("key")
            if key not in _ALLOWED_KEYS:
                raise WebSourceError("That key isn't supported.")
            await page.keyboard.press(key)
            _live.steps.append({"a": "key", "key": key})
            await _settle(page)
        elif kind == "scroll":
            try:
                dy = max(-3000, min(3000, int(action.get("dy", 500))))
                dx = max(-3000, min(3000, int(action.get("dx", 0))))
            except (TypeError, ValueError):
                dy, dx = 500, 0
            try:
                x = max(0, min(VIEWPORT["width"], float(action["x"])))
                y = max(0, min(VIEWPORT["height"], float(action["y"])))
            except (KeyError, TypeError, ValueError):
                x, y = VIEWPORT["width"] / 2, VIEWPORT["height"] / 2
            # By exactly the swipe's distance, in whatever scrolls under the
            # finger (a list in a pop-up, not just the page). A mouse wheel in
            # this phone-sized browser moves about 2.5 times as far.
            try:
                await page.evaluate(_JS_SCROLL, [x, y, dx, dy])
            except Exception:
                await page.mouse.move(x, y)
                await page.mouse.wheel(dx / 2.5, dy / 2.5)
            await asyncio.sleep(0.35)
        elif kind == "goto":
            url = check_url(action.get("url") or "")
            await _goto(page, url)
            _live.steps.append({"a": "goto", "url": url})
        elif kind == "back":
            try:
                await page.go_back(wait_until="domcontentloaded")
            except Exception:
                pass
            _live.steps.append({"a": "back"})
            await _settle(page)
        elif kind == "reload":
            try:
                await page.reload(wait_until="domcontentloaded")
            except Exception:
                pass
            await _settle(page)
        elif kind == "restart_recording":
            # Steps so far were just finding the way; the login starts here.
            _live.steps = [{"a": "goto", "url": page.url}]
        else:
            raise WebSourceError("Unknown action.")
    return await live_frame()


def _record_edit(steps: list, selector, backspaces: int, text: str, secret: bool) -> None:
    """
    Record keyboard typing as one "type" step per field: the phone sends a
    few letters at a time, and each correction (backspaces) is folded into
    the text, so the replay types the final value once.
    """
    last = steps[-1] if steps else None
    if last and last.get("a") == "type" and last.get("sel") == selector and bool(last.get("secret")) == secret:
        current = _decrypt(last["secret"]) if secret else last.get("text", "")
        if backspaces <= len(current):
            current = current[: len(current) - backspaces] + text
            if not current:
                steps.pop()
            elif secret:
                last["secret"] = _encrypt(current)
            else:
                last["text"] = current
            return
    steps.extend({"a": "key", "key": "Backspace"} for _ in range(backspaces))
    if text:
        step = {"a": "type", "sel": selector}
        if secret:
            step["secret"] = _encrypt(text)
        else:
            step["text"] = text
        steps.append(step)


async def live_save(name: str | None, keep_steps: bool = True) -> dict:
    """Save the live browser's session (and recorded login steps) as a new website or onto the one it was opened for."""
    live = _live
    if live is None:
        raise WebSourceError("The browser is closed. Open a website first.")
    _touch()
    state = await live.ctx.storage_state()
    steps = list(live.steps) if keep_steps else []
    if len(steps) <= 1:
        steps = []  # just opening the start page: nothing to replay
    with _lock:
        sites = _load(strict=True)
        site_id = live.site_id
        now = int(time.time())
        if site_id and site_id in sites:
            site = sites[site_id]
            # Opened already logged in (saved cookies), a few taps and Save
            # aren't a login recording: they would replace the real steps and
            # the next automatic login would do nothing. A new recording with
            # a password in it is.
            relogged = any(st.get("secret") for st in steps)
            if steps and (relogged or not live.had_state or not site.get("login_steps")):
                site["login_steps"] = steps
            if name:
                site["name"] = name.strip()[:80]
        else:
            site_id = "ws" + secrets.token_hex(5)
            host = urllib.parse.urlsplit(live.start_url).hostname or "Website"
            site = {
                "name": (name or "").strip()[:80] or host,
                "start_url": live.start_url,
                "search_url": "",
                "link_pattern": "",
                "max_results": 3,
                "enabled": False,
                "login_steps": steps,
                "created": now,
            }
            live.site_id = site_id
        site["updated"] = now
        site["last_login"] = now
        site["last_login_ok"] = True
        site["last_error"] = None
        sites[site_id] = site
        _write_json(_SITES_PATH, sites)
    _save_state(site_id, state)
    return _public(site_id, site)


async def live_close() -> None:
    global _live
    live, _live = _live, None
    if live is not None:
        try:
            await live.ctx.close()
        except Exception:
            pass


# ---------------------------------------------------------------------
# Logging in again by itself, and reading a site
# ---------------------------------------------------------------------

_relogin_locks: dict[str, asyncio.Lock] = {}
_fetch_semaphore = asyncio.Semaphore(2)
_fetches_running = 0


async def _replay(site: dict, ctx) -> None:
    page = await ctx.new_page()
    for step in site.get("login_steps") or []:
        kind = step.get("a")
        open_tabs = [pg for pg in ctx.pages if not pg.is_closed()]
        if page.is_closed() and open_tabs:
            page = open_tabs[-1]  # a sign-in popup that closed itself, as in the live browser
        if kind in ("tab", "close_tab"):
            i = step.get("i")
            if not isinstance(i, int) or not 0 <= i < len(open_tabs):
                raise WebSourceError("The login opened different tabs this time. Log in again from the admin panel.")
            if kind == "tab":
                page = open_tabs[i]
            elif len(open_tabs) > 1:
                await open_tabs[i].close()
                if page.is_closed():
                    page = [pg for pg in ctx.pages if not pg.is_closed()][-1]
            continue
        if kind == "goto":
            await _goto(page, step["url"])
            continue
        if kind == "click":
            done = False
            if step.get("sel"):
                try:
                    await page.locator(step["sel"]).first.click(timeout=_STEP_TIMEOUT_MS)
                    done = True
                except Exception:
                    pass
            if not done:
                await _tap(page, step["x"], step["y"])
        elif kind == "type":
            secret = bool(step.get("secret"))
            text = _decrypt(step["secret"]) if secret else step.get("text", "")
            done = False
            if step.get("sel"):
                try:
                    # Typed, not filled: recording appended to what the field
                    # held, so "admin@" + "example.com" must stay one address.
                    target = page.locator(step["sel"]).first
                    await target.focus(timeout=_STEP_TIMEOUT_MS)
                    await target.press_sequentially(text, delay=15, timeout=_STEP_TIMEOUT_MS * 4)
                    done = True
                except Exception:
                    pass
            if not done:
                if secret:
                    # Never typed blind: whatever has focus could be a search
                    # box that sends the password to the site in the clear.
                    raise WebSourceError("The login page changed (the password field wasn't found). Log in again from the admin panel.")
                await page.keyboard.type(text, delay=15)
        elif kind == "key":
            await page.keyboard.press(step["key"])
        elif kind == "back":
            try:
                await page.go_back(wait_until="domcontentloaded")
            except Exception:
                pass
        await _settle(page)
    if page.is_closed():
        open_tabs = [pg for pg in ctx.pages if not pg.is_closed()]
        if open_tabs:
            page = open_tabs[-1]
    # A login that finishes with a background request needs longer than the
    # usual settle before its session cookie exists.
    try:
        await page.wait_for_load_state("networkidle", timeout=10000)
    except Exception:
        pass
    return page


_RELOGIN_PAUSE_SECONDS = 1800


async def relogin(site_id: str) -> None:
    """Replay the recorded login steps in a fresh browser and save the new session."""
    site = get_site(site_id)
    if site is None:
        raise WebSourceError("That website isn't saved anymore.")
    if not site.get("login_steps"):
        raise WebSourceError("No login steps were recorded for this website. Log in from the browser and save.")
    asked_at = time.time()
    lock = _relogin_locks.setdefault(site_id, asyncio.Lock())
    async with lock:
        # Another request logged in while this one waited for the lock.
        try:
            if os.path.getmtime(_state_path(site_id)) > asked_at:
                return
        except OSError:
            pass
        # After a failed login, no automatic retry for a while: every user
        # question re-submitting a changed (or CAPTCHA-blocked) password
        # would get the site account locked.
        site = get_site(site_id) or site
        if site.get("last_login_ok") is False and time.time() - (site.get("last_login") or 0) < _RELOGIN_PAUSE_SECONDS:
            raise WebSourceError(f"{site['name']} needs a new login. Open it from the admin panel and log in again.")
        _touch()
        ctx = await _new_context(None)
        try:
            page = await _replay(site, ctx)
            # Logged in for real? Check the page the searches start from.
            check_url_ = site.get("search_url", "").replace("{query}", "test") or site["start_url"]
            await _goto(page, check_url_)
            if await _looks_logged_out(page):
                raise WebSourceError("Logging in again didn't work (the site still asks for a login). Open it from the admin panel and log in.")
            _save_state(site_id, await ctx.storage_state())
            _set_login_result(site_id, True)
            logger.info("Web source %s: logged in again by replaying %d steps", site_id, len(site["login_steps"]))
        except WebSourceError as e:
            _set_login_result(site_id, False, str(e))
            raise
        except Exception as e:
            _set_login_result(site_id, False, f"Login replay failed: {type(e).__name__}")
            logger.exception("Web source %s: login replay failed", site_id)
            raise WebSourceError("Logging in again didn't work. Open the website from the admin panel and log in.")
        finally:
            await ctx.close()


def _link_matcher(pattern: str):
    """Matches when the address contains the text as typed, or matches it as a regular expression ("/view?id=" is both)."""
    if not pattern:
        return None
    low = pattern.lower()
    try:
        rx = re.compile(pattern, re.I)
    except re.error:
        rx = None
    return lambda href: low in href.lower() or bool(rx and rx.search(href[:2000]))


def _same_site(url: str, base_host: str) -> bool:
    host = (urllib.parse.urlsplit(url).hostname or "").lower()
    base = base_host.lower().removeprefix("www.")
    return host == base or host.endswith("." + base)


async def _read_site(site_id: str, site: dict, query: str, ctx) -> list[dict]:
    page = await ctx.new_page()
    search_url = site["search_url"].replace("{query}", urllib.parse.quote_plus(query))
    base_host = urllib.parse.urlsplit(search_url).hostname or ""
    await _goto(page, search_url)
    if await _looks_logged_out(page) or not _same_site(page.url, base_host):
        raise _LoggedOut()
    matcher = _link_matcher(site.get("link_pattern") or "")
    results = []
    if matcher is None:
        results.append({"title": await page.title(), "url": _clean_url(page.url), "text": await _page_text(page)})
        return results
    links = await page.eval_on_selector_all(
        "a[href]", "els => els.slice(0, 2000).map((a) => a.href).filter((h) => h.length < 2000)"
    )
    picked, seen = [], {page.url.split("#")[0]}
    for href in links:
        href = (href or "").split("#")[0]
        # Only the site's own pages: a results page could otherwise point the
        # (logged-in) browser anywhere, and that page would be cited under
        # the trusted site's name.
        if not href.startswith(("http://", "https://")) or href in seen or not _same_site(href, base_host) or not matcher(href):
            continue
        seen.add(href)
        picked.append(href)
        if len(picked) >= (site.get("max_results") or 3):
            break
    for href in picked:
        try:
            await _goto(page, href)
        except WebSourceError:
            continue
        if not _same_site(page.url, base_host):
            continue  # redirected off the site
        if await _looks_logged_out(page):
            raise _LoggedOut()  # a login wall is not a result
        results.append({"title": await page.title(), "url": _clean_url(page.url), "text": await _page_text(page)})
    return results


_SESSION_PARAM_RE = re.compile(r"^(sid|session|sessionid|jsessionid|phpsessid|token|auth|access_token|sig|signature)$", re.I)


def _clean_url(url: str) -> str:
    """Session ids in a URL opened with the admin's login aren't passed on to every user who sees the citation."""
    parts = urllib.parse.urlsplit(url)
    query = [(k, v) for k, v in urllib.parse.parse_qsl(parts.query, keep_blank_values=True) if not _SESSION_PARAM_RE.match(k)]
    path = re.sub(r";jsessionid=[^/?#]*", "", parts.path, flags=re.I)
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, urllib.parse.urlencode(query), ""))


class _LoggedOut(Exception):
    pass


async def search(site_id: str, query: str) -> list[dict]:
    """Search one website and read its top results: [{'title', 'url', 'text'}]."""
    global _fetches_running
    site = get_site(site_id)
    if site is None or not site.get("search_url"):
        raise WebSourceError("That website has no search address set.")
    async with _fetch_semaphore:
        _fetches_running += 1
        _touch()
        try:
            for attempt in (1, 2):
                try:
                    loaded_at = os.path.getmtime(_state_path(site_id))
                except OSError:
                    loaded_at = None
                ctx = await _new_context(_load_state(site_id), text_only=True)
                try:
                    results = await _read_site(site_id, site, query, ctx)
                    try:
                        # Refreshed cookies are kept -- unless the site was
                        # deleted or saved anew while this search ran.
                        try:
                            current = os.path.getmtime(_state_path(site_id))
                        except OSError:
                            current = None
                        if get_site(site_id) is not None and current == loaded_at:
                            _save_state(site_id, await ctx.storage_state())
                    except Exception:
                        pass
                    return results
                except _LoggedOut:
                    if attempt == 2 or not site.get("login_steps"):
                        _set_login_result(site_id, False, "The website asked for a login.")
                        raise WebSourceError(f"{site['name']} asked for a login. Log in again from the admin panel.")
                finally:
                    await ctx.close()
                await relogin(site_id)
                site = get_site(site_id) or site
        finally:
            _fetches_running -= 1
            _touch()
    return []


def _chunks(text: str) -> list[str]:
    paragraphs = [p.strip() for p in text.split("\n") if p.strip()]
    out, current = [], ""
    for p in paragraphs:
        if len(current) + len(p) + 1 > _CHUNK_CHARS and current:
            out.append(current)
            current = ""
        current = f"{current}\n{p}" if current else p
        while len(current) > _CHUNK_CHARS * 1.5:
            out.append(current[:_CHUNK_CHARS])
            current = current[_CHUNK_CHARS:]
    if current:
        out.append(current)
    return [c for c in out if len(c) > 80]


def _source_title(site_name: str, page_title: str) -> str:
    """'PubMed: Lithium treatment of ...' -- just the site name when the page title adds nothing."""
    # A web page picks its own title, and it goes into the AI prompt: no
    # quotes or line breaks, and short.
    page_title = re.sub(r"[\"\r\n\t]+", " ", page_title or "").strip()[:120]
    page_title = re.sub(r"\s*[-|–]\s*" + re.escape(site_name) + r"\s*$", "", page_title, flags=re.I)
    if not page_title or page_title.lower() == site_name.lower():
        return site_name[:200]
    return f"{site_name}: {page_title}"[:200]


async def passages(site_id: str, query: str) -> list[dict]:
    """Search results as passages for Drug LookUp answers: [{'text', 'title', 'url', 'book_id', 'page'}]."""
    site = get_site(site_id) or {}
    out = []
    for r in await search(site_id, query):
        for chunk in _chunks(r["text"]):
            out.append({
                "text": chunk,
                "title": _source_title(site.get("name") or "Web", r["title"] or r["url"]),
                "url": r["url"],
                "book_id": f"web:{site_id}",
                "page": None,
            })
            if len(out) >= _MAX_CHUNKS_PER_SITE:
                return out
    return out


async def search_terms(question: str) -> str:
    """
    A question becomes a few search words: a website's search box (PubMed's
    included) does badly with a whole sentence. Falls back to the question.
    """
    import pdf_qa  # lazy: pdf_qa is heavy and only needed here
    from config import TRANSLATION_MODEL

    try:
        response = await asyncio.to_thread(
            pdf_qa.claude_client.messages.create,
            model=TRANSLATION_MODEL,
            max_tokens=60,
            system=(
                "Turn the user's question into a short search query for a medical website's search box: "
                "2 to 6 key terms in English, the standard medical terms, no question words, no quotes, "
                "no operators. Output ONLY the query."
            ),
            messages=[{"role": "user", "content": question[:1000]}],
        )
        terms = "".join(b.text for b in response.content if b.type == "text").strip().strip('"')
        return terms[:200] or question[:200]
    except Exception:
        logger.exception("Search-term extraction failed (using the question)")
        return question[:200]
