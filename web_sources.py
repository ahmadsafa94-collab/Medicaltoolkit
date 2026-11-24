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
import hashlib
import ipaddress
import json
import logging
import os
import re
import secrets
import shutil
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


def _load() -> dict:
    try:
        with open(_SITES_PATH, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
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
    """Switched-on sources with a search address: what users can tick in Ask your books."""
    return [
        {"book_id": f"web:{s['site_id']}", "title": s["name"], "qa_indexed": True, "web": True}
        for s in list_sites()
        if s["enabled"] and s["search_url"]
    ]


def update_site(site_id: str, fields: dict) -> dict:
    """Admin edits: name, search_url, link_pattern, max_results, enabled. Validated here."""
    with _lock:
        sites = _load()
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
                raise WebSourceError("Set the search address before making it available in Ask your books.")
        site["updated"] = int(time.time())
        sites[site_id] = site
        _write_json(_SITES_PATH, sites)
        return _public(site_id, site)


def delete_site(site_id: str) -> None:
    with _lock:
        sites = _load()
        if sites.pop(site_id, None) is None:
            raise WebSourceError("That website isn't saved anymore.")
        _write_json(_SITES_PATH, sites)
    try:
        os.remove(_state_path(site_id))
    except OSError:
        pass


def _set_login_result(site_id: str, ok: bool, error: str | None = None) -> None:
    with _lock:
        sites = _load()
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


async def _guard(route) -> None:
    url = route.request.url
    parts = urllib.parse.urlsplit(url)
    if parts.scheme in ("http", "https") and _blocked_host(parts.hostname or ""):
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
                args=["--no-sandbox", "--disable-dev-shm-usage"],
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


async def _new_context(state: dict | None = None):
    browser = await _get_browser()
    ctx = await browser.new_context(
        viewport=VIEWPORT,
        device_scale_factor=_SCALE,
        is_mobile=True,
        has_touch=True,
        user_agent=_USER_AGENT,
        locale="en-US",
        storage_state=state,
    )
    ctx.set_default_navigation_timeout(_NAV_TIMEOUT_MS)
    ctx.set_default_timeout(_STEP_TIMEOUT_MS)
    await ctx.route("**/*", _guard)
    return ctx


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


async def _page_text(page) -> str:
    try:
        text = await page.evaluate(
            """() => {
              const root = document.querySelector('main, article, [role=main], #main, #content') || document.body;
              return root ? root.innerText : '';
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


_live: _Live | None = None


def live_info() -> dict | None:
    if _live is None:
        return None
    return {"site_id": _live.site_id, "steps": len(_live.steps)}


async def live_start(url: str, site_id: str | None = None) -> dict:
    global _live
    url = check_url(url)
    if site_id is not None and get_site(site_id) is None:
        raise WebSourceError("That website isn't saved anymore.")
    await live_close()
    _touch()
    ctx = await _new_context(_load_state(site_id) if site_id else None)
    page = await ctx.new_page()
    live = _Live(ctx, page, site_id, url)

    def on_page(new_page):
        # A link that opens a new tab (common for "Sign in with ...") is
        # followed there, as a phone browser would.
        live.page = new_page

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
    return {
        "image": base64.b64encode(shot).decode("ascii"),
        "url": page.url,
        "title": title,
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
            await page.mouse.click(x, y)
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
            if action.get("secret"):
                step["secret"] = _encrypt(text)
            else:
                step["text"] = text
            _live.steps.append(step)
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
            except (TypeError, ValueError):
                dy = 500
            await page.mouse.wheel(0, dy)
            await asyncio.sleep(0.4)
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


async def live_save(name: str | None, keep_steps: bool = True) -> dict:
    """Save the live browser's session (and recorded login steps) as a new website or onto the one it was opened for."""
    if _live is None:
        raise WebSourceError("The browser is closed. Open a website first.")
    _touch()
    state = await _live.ctx.storage_state()
    steps = list(_live.steps) if keep_steps else []
    if len(steps) <= 1:
        steps = []  # just opening the start page: nothing to replay
    with _lock:
        sites = _load()
        site_id = _live.site_id
        now = int(time.time())
        if site_id and site_id in sites:
            site = sites[site_id]
            if steps:
                site["login_steps"] = steps
            if name:
                site["name"] = name.strip()[:80]
        else:
            site_id = "ws" + secrets.token_hex(5)
            host = urllib.parse.urlsplit(_live.start_url).hostname or "Website"
            site = {
                "name": (name or "").strip()[:80] or host,
                "start_url": _live.start_url,
                "search_url": "",
                "link_pattern": "",
                "max_results": 3,
                "enabled": False,
                "login_steps": steps,
                "created": now,
            }
            _live.site_id = site_id
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
                await page.mouse.click(step["x"], step["y"])
        elif kind == "type":
            text = _decrypt(step["secret"]) if step.get("secret") else step.get("text", "")
            done = False
            if step.get("sel"):
                try:
                    await page.locator(step["sel"]).first.fill(text, timeout=_STEP_TIMEOUT_MS)
                    done = True
                except Exception:
                    pass
            if not done:
                await page.keyboard.type(text, delay=15)
        elif kind == "key":
            await page.keyboard.press(step["key"])
        elif kind == "back":
            try:
                await page.go_back(wait_until="domcontentloaded")
            except Exception:
                pass
        await _settle(page)


async def relogin(site_id: str) -> None:
    """Replay the recorded login steps in a fresh browser and save the new session."""
    site = get_site(site_id)
    if site is None:
        raise WebSourceError("That website isn't saved anymore.")
    if not site.get("login_steps"):
        raise WebSourceError("No login steps were recorded for this website. Log in from the browser and save.")
    lock = _relogin_locks.setdefault(site_id, asyncio.Lock())
    async with lock:
        _touch()
        ctx = await _new_context(None)
        try:
            await _replay(site, ctx)
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
    if not pattern:
        return None
    try:
        rx = re.compile(pattern, re.I)
        return lambda href: bool(rx.search(href))
    except re.error:
        low = pattern.lower()
        return lambda href: low in href.lower()


async def _read_site(site_id: str, site: dict, query: str, ctx) -> list[dict]:
    page = await ctx.new_page()
    search_url = site["search_url"].replace("{query}", urllib.parse.quote_plus(query))
    await _goto(page, search_url)
    if await _looks_logged_out(page):
        raise _LoggedOut()
    matcher = _link_matcher(site.get("link_pattern") or "")
    results = []
    if matcher is None:
        results.append({"title": await page.title(), "url": page.url, "text": await _page_text(page)})
        return results
    links = await page.eval_on_selector_all("a[href]", "els => els.map((a) => a.href)")
    picked, seen = [], {page.url.split("#")[0]}
    for href in links:
        href = (href or "").split("#")[0]
        if not href.startswith(("http://", "https://")) or href in seen or not matcher(href):
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
        results.append({"title": await page.title(), "url": page.url, "text": await _page_text(page)})
    return results


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
                ctx = await _new_context(_load_state(site_id))
                try:
                    results = await _read_site(site_id, site, query, ctx)
                    try:
                        _save_state(site_id, await ctx.storage_state())  # keep refreshed cookies
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
    page_title = re.sub(r"\s*[-|–]\s*" + re.escape(site_name) + r"\s*$", "", page_title.strip(), flags=re.I)
    if not page_title or page_title.lower() == site_name.lower():
        return site_name[:200]
    return f"{site_name}: {page_title}"[:200]


async def passages(site_id: str, query: str) -> list[dict]:
    """Search results as passages for Ask your books: [{'text', 'title', 'url', 'book_id', 'page'}]."""
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
