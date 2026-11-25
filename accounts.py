"""
Username/password accounts, created by the admin.

A person logs in once in the mini app; that links the username to their
Telegram account, which then has full access (subscriptions.is_premium
treats a linked account like Premium) in the mini app and the chat alike.
A username is linked to one Telegram account at a time: logging in from a
second one is refused until the admin unlinks it (or its owner logs out).
Everyone not logged in, not Premium and not the admin is a guest: the
limited free tier (see config's FREE_* values).

Each account keeps an activity log: every mini app API call it makes (the
endpoint, status, time taken), its errors with their message, and logins.
The admin sees it, with statistics, in Accounts.

Two kinds of account ("access"):
  full   created by the admin: full access for as long as it exists
  paid   registered by a Telegram user who paid for Premium with Stars
         (the "Create your account" page after paying): full access while
         that payer's Premium is active -- one account per payer

Storage: STORAGE_DIR/_admin/accounts.json and
STORAGE_DIR/_admin/account_logs/{username}.jsonl. Passwords are kept only as
scrypt hashes.
"""

import hashlib
import hmac
import json
import os
import re
import secrets
import threading
import time
from collections import Counter, defaultdict

from config import STORAGE_DIR

_DIR = os.path.join(STORAGE_DIR, "_admin")
_PATH = os.path.join(_DIR, "accounts.json")
_LOG_DIR = os.path.join(_DIR, "account_logs")
_LOG_MAX_LINES = 5000       # older entries are dropped past this
_LOG_KEEP_LINES = 4000

USERNAME_RE = re.compile(r"[a-z0-9][a-z0-9._-]{2,31}")
MIN_PASSWORD = 6

_lock = threading.RLock()
_by_telegram: dict[int, dict] | None = None   # telegram id -> {username, access, paid_by} (cache of the file)

# Wrong passwords: a Telegram account or a username that keeps failing is
# paused, so a password can't be guessed by trying many.
_FAIL_WINDOW = 15 * 60
_MAX_FAILS_PER_TG = 5
_MAX_FAILS_PER_USER = 10
_fails_tg: dict[int, list[float]] = defaultdict(list)
_fails_user: dict[str, list[float]] = defaultdict(list)


class AccountError(Exception):
    pass


# ---------------------------------------------------------------------
# Storage
# ---------------------------------------------------------------------

def _load() -> dict:
    try:
        with open(_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, json.JSONDecodeError):
        return {}


def _save(data: dict) -> None:
    global _by_telegram
    os.makedirs(_DIR, exist_ok=True)
    tmp = f"{_PATH}.{secrets.token_hex(4)}.tmp"
    with open(tmp, "w") as f:
        json.dump(data, f)
    os.replace(tmp, _PATH)
    _by_telegram = None


def _hash(password: str, salt: bytes | None = None) -> str:
    salt = salt or secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, n=2**14, r=8, p=1, dklen=32)
    return f"scrypt${salt.hex()}${digest.hex()}"


def _check(password: str, stored: str) -> bool:
    try:
        _, salt_hex, digest_hex = stored.split("$")
        digest = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt_hex), n=2**14, r=8, p=1, dklen=32)
        return hmac.compare_digest(digest.hex(), digest_hex)
    except Exception:
        return False


def clean_username(raw) -> str:
    name = str(raw or "").strip().lower()
    if not USERNAME_RE.fullmatch(name):
        raise AccountError("A username is 3-32 characters: letters, digits, dot, dash or underscore, starting with a letter or digit.")
    return name


def generate_password() -> str:
    # No look-alike characters (0/O, 1/l/I): it is read off a screen and typed.
    alphabet = "abcdefghjkmnpqrstuvwxyzABCDEFGHJKLMNPQRSTUVWXYZ23456789"
    return "".join(secrets.choice(alphabet) for _ in range(10))


# ---------------------------------------------------------------------
# Who is logged in
# ---------------------------------------------------------------------

def linked_account(telegram_id: int) -> dict | None:
    """{username, access, paid_by} of the account linked to this Telegram account (not disabled), or None."""
    global _by_telegram
    with _lock:
        if _by_telegram is None:
            _by_telegram = {
                int(a["telegram_id"]): {"username": name, "access": a.get("access") or "full", "paid_by": a.get("paid_by")}
                for name, a in _load().items()
                if a.get("telegram_id") and not a.get("disabled")
            }
        return _by_telegram.get(int(telegram_id))


def username_for(telegram_id: int) -> str | None:
    acct = linked_account(telegram_id)
    return acct["username"] if acct else None


def paid_account_of(telegram_id: int) -> str | None:
    """The account this Telegram user registered after paying, if any."""
    for name, a in _load().items():
        if a.get("access") == "paid" and a.get("paid_by") and int(a["paid_by"]) == int(telegram_id):
            return name
    return None


def register(telegram_id: int, username, password) -> str:
    """
    A Premium payer creates their own account (the caller checks they paid).
    It is linked to their Telegram account at once. One per payer.
    """
    existing = paid_account_of(telegram_id)
    if existing:
        raise AccountError(f"You already have an account: {existing}. Log in with it.")
    if username_for(telegram_id):
        raise AccountError("This Telegram account is already logged in to an account.")
    name = clean_username(username)
    password = str(password or "")
    if len(password) < 8:
        raise AccountError("Choose a password of at least 8 characters.")
    now = time.time()
    with _lock:
        data = _load()
        if name in data:
            raise AccountError("That username is taken. Choose another.")
        data[name] = {"password": _hash(password), "created": now, "telegram_id": int(telegram_id), "linked_at": now,
                      "last_login": now, "disabled": False, "access": "paid", "paid_by": int(telegram_id),
                      "note": "Registered after a Premium payment"}
        _save(data)
    log_event(name, {"kind": "registered", "telegram_id": telegram_id})
    return name


def _recent(stamps: list[float], now: float) -> list[float]:
    return [t for t in stamps if now - t < _FAIL_WINDOW]


def login(telegram_id: int, username, password) -> str:
    """Check the password and link the account to this Telegram account. Returns the username."""
    now = time.time()
    tg_fails = _fails_tg[telegram_id] = _recent(_fails_tg[telegram_id], now)
    if len(tg_fails) >= _MAX_FAILS_PER_TG:
        raise AccountError("Too many wrong attempts. Try again in 15 minutes.")
    try:
        name = clean_username(username)
    except AccountError:
        tg_fails.append(now)
        raise AccountError("Wrong username or password.")
    user_fails = _fails_user[name] = _recent(_fails_user[name], now)
    if len(user_fails) >= _MAX_FAILS_PER_USER:
        raise AccountError("Too many wrong attempts on this account. Try again in 15 minutes.")
    with _lock:
        data = _load()
        acct = data.get(name)
        if acct is None or not _check(str(password or ""), acct.get("password", "")):
            tg_fails.append(now)
            user_fails.append(now)
            if acct is not None:
                log_event(name, {"kind": "login_failed", "telegram_id": telegram_id})
            raise AccountError("Wrong username or password.")
        if acct.get("disabled"):
            raise AccountError("This account is disabled. Contact the admin.")
        linked = acct.get("telegram_id")
        if linked and int(linked) != int(telegram_id):
            log_event(name, {"kind": "login_refused", "telegram_id": telegram_id, "detail": "linked to another Telegram account"})
            raise AccountError("This account is already in use on another Telegram account. Ask the admin to unlink it.")
        # One account per Telegram account: logging into another moves the link.
        for other_name, other in data.items():
            if other_name != name and other.get("telegram_id") and int(other["telegram_id"]) == int(telegram_id):
                other["telegram_id"] = None
        acct["telegram_id"] = int(telegram_id)
        acct["linked_at"] = now
        acct["last_login"] = now
        _save(data)
    _fails_tg.pop(telegram_id, None)
    _fails_user.pop(name, None)
    log_event(name, {"kind": "login", "telegram_id": telegram_id})
    return name


def logout(telegram_id: int) -> str | None:
    """Unlink this Telegram account from its account (back to guest)."""
    with _lock:
        data = _load()
        for name, acct in data.items():
            if acct.get("telegram_id") and int(acct["telegram_id"]) == int(telegram_id):
                acct["telegram_id"] = None
                _save(data)
                log_event(name, {"kind": "logout", "telegram_id": telegram_id})
                return name
    return None


# ---------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------

def list_accounts() -> list[dict]:
    out = []
    for name, a in sorted(_load().items()):
        out.append({
            "username": name,
            "telegram_id": a.get("telegram_id"),
            "disabled": bool(a.get("disabled")),
            "created": a.get("created"),
            "last_login": a.get("last_login"),
            "last_active": a.get("last_active"),
            "note": a.get("note") or "",
            "access": a.get("access") or "full",
            "paid_by": a.get("paid_by"),
        })
    return out


def create_account(username, password: str | None = None, note: str = "") -> tuple[str, str]:
    """Returns (username, password): the password is shown to the admin once, then only its hash is kept."""
    name = clean_username(username)
    password = password or generate_password()
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"A password needs at least {MIN_PASSWORD} characters.")
    with _lock:
        data = _load()
        if name in data:
            raise AccountError("That username already exists.")
        data[name] = {"password": _hash(password), "created": time.time(), "telegram_id": None,
                      "disabled": False, "note": str(note or "")[:200]}
        _save(data)
    log_event(name, {"kind": "created"})
    return name, password


def _update(username: str, fn) -> dict:
    name = clean_username(username)
    with _lock:
        data = _load()
        acct = data.get(name)
        if acct is None:
            raise AccountError("No such account.")
        result = fn(acct) or {}
        _save(data)
    return result


def reset_password(username, password: str | None = None) -> str:
    password = password or generate_password()
    if len(password) < MIN_PASSWORD:
        raise AccountError(f"A password needs at least {MIN_PASSWORD} characters.")
    _update(username, lambda a: a.__setitem__("password", _hash(password)))
    log_event(clean_username(username), {"kind": "password_reset"})
    return password


def unlink(username) -> None:
    _update(username, lambda a: a.__setitem__("telegram_id", None))
    log_event(clean_username(username), {"kind": "unlinked"})


def set_disabled(username, disabled: bool) -> None:
    _update(username, lambda a: a.__setitem__("disabled", bool(disabled)))
    log_event(clean_username(username), {"kind": "disabled" if disabled else "enabled"})


def delete_account(username) -> None:
    name = clean_username(username)
    with _lock:
        data = _load()
        if data.pop(name, None) is None:
            raise AccountError("No such account.")
        _save(data)
    try:
        os.remove(os.path.join(_LOG_DIR, f"{name}.jsonl"))
    except OSError:
        pass


# ---------------------------------------------------------------------
# Activity log and statistics
# ---------------------------------------------------------------------

_log_lock = threading.Lock()
_last_active_saved: dict[str, float] = {}

# Requests that run an AI model (and so cost money), by what they are.
AI_ENDPOINTS = [
    (re.compile(r"^/api/ask/multi$"), "Ask your books"),
    (re.compile(r"^/api/books/:id/(ask|qa)"), "Ask a book"),
    (re.compile(r"^/api/drugs/ask$"), "Drug LookUp"),
    (re.compile(r"^/api/drugs/interactions$"), "Interactions"),
    (re.compile(r"^/api/lexidrug/ask$"), "Lexicomp"),
    (re.compile(r"^/api/books/:id/explain$"), "Explain"),
    (re.compile(r"^/api/books/:id/summar"), "Summaries"),
    (re.compile(r"^/api/books/:id/quiz"), "Quizzes"),
    (re.compile(r"^/api/books/:id/flashcards"), "Flashcards"),
    (re.compile(r"^/api/books/:id/chapters$"), "Chapter division"),
]

_ID_SEGMENT = re.compile(r"/(?=[A-Za-z0-9_-]*\d)[A-Za-z0-9_-]{6,}(?=/|$)")


def endpoint_label(path: str) -> str:
    """/api/books/7f3209c7f6/notes -> /api/books/:id/notes"""
    return _ID_SEGMENT.sub("/:id", path)


def log_event(username: str, event: dict) -> None:
    event = {"ts": round(time.time(), 2), **event}
    path = os.path.join(_LOG_DIR, f"{username}.jsonl")
    with _log_lock:
        os.makedirs(_LOG_DIR, exist_ok=True)
        with open(path, "a") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
        # Trim now and then (not on every write): keep the newest entries.
        if secrets.randbelow(200) == 0:
            try:
                with open(path) as f:
                    lines = f.readlines()
                if len(lines) > _LOG_MAX_LINES:
                    with open(path, "w") as f:
                        f.writelines(lines[-_LOG_KEEP_LINES:])
            except OSError:
                pass
    # "Last active" on the account, written at most every 5 minutes.
    now = time.time()
    if event.get("kind") == "api" and now - _last_active_saved.get(username, 0) > 300:
        _last_active_saved[username] = now
        try:
            _update(username, lambda a: a.__setitem__("last_active", now))
        except AccountError:
            pass


def log_request(username: str, method: str, path: str, status: int, ms: float, detail: str | None = None) -> None:
    event = {"kind": "api", "m": method, "p": endpoint_label(path), "s": int(status), "ms": int(ms)}
    if detail and status >= 400:
        event["err"] = str(detail)[:300]
    log_event(username, event)


def read_log(username: str, limit: int = 300) -> dict:
    """Recent entries (newest first) and statistics over everything kept."""
    name = clean_username(username)
    path = os.path.join(_LOG_DIR, f"{name}.jsonl")
    events = []
    try:
        with open(path) as f:
            for line in f:
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    api = [e for e in events if e.get("kind") == "api"]
    now = time.time()
    day = 86400
    errors = [e for e in api if e.get("s", 0) >= 400]
    by_endpoint = Counter(f"{e.get('m')} {e.get('p')}" for e in api)
    ai = Counter()
    for e in api:
        if e.get("s") == 200 and e.get("m") == "POST":
            for pattern, label in AI_ENDPOINTS:
                if pattern.search(e.get("p", "")):
                    ai[label] += 1
                    break
    per_day = Counter(time.strftime("%Y-%m-%d", time.gmtime(e["ts"])) for e in api if now - e["ts"] < 14 * day)
    durations = sorted(e.get("ms", 0) for e in api)
    stats = {
        "requests": len(api),
        "requests_24h": sum(1 for e in api if now - e["ts"] < day),
        "requests_7d": sum(1 for e in api if now - e["ts"] < 7 * day),
        "errors": len(errors),
        "errors_7d": sum(1 for e in errors if now - e["ts"] < 7 * day),
        "server_errors": sum(1 for e in errors if e.get("s", 0) >= 500),
        "median_ms": durations[len(durations) // 2] if durations else None,
        "slowest_ms": durations[-1] if durations else None,
        "ai_calls": dict(ai.most_common()),
        "ai_calls_total": sum(ai.values()),
        "top_endpoints": by_endpoint.most_common(12),
        "per_day": sorted(per_day.items()),
        "logins": sum(1 for e in events if e.get("kind") == "login"),
        "failed_logins": sum(1 for e in events if e.get("kind") in ("login_failed", "login_refused")),
        "since": events[0]["ts"] if events else None,
    }
    return {"events": list(reversed(events[-limit:])), "errors": list(reversed(errors[-50:])), "stats": stats}
