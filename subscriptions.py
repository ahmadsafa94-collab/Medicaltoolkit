"""
Free/Premium plan tracking -- the foundation the admin panel, customer "My
Plan" panel, Telegram Stars payments, and every AI-costing feature's quota
check all sit on top of.

Storage layout (small per-purpose JSON files, same pattern as library.py):
  STORAGE_DIR/{user_id}/subscription.json   -- one user's plan/usage
  STORAGE_DIR/_admin/users_index.json       -- {user_id: {first_seen, last_seen, username}}
                                                for admin stats/broadcast/lookup, since there's
                                                otherwise no way to enumerate "every user who has
                                                ever talked to this bot" from per-user files alone.

Design notes:
  - Free-tier usage counters (summaries/quizzes/questions) reset automatically
    the first time they're touched on a new UTC day -- no cron job needed,
    see _day_key()/_load(). (_period_key() still exists, but only to group
    PAYMENTS by calendar month for the revenue stats.)
  - Premium is a plain expiry timestamp (premium_until), extended (not
    replaced) by a renewal/grant that arrives before the current one lapses
    -- see grant_premium(). is_premium() lazily downgrades an expired user
    back to "free" the next time anything checks their status.
"""

import json
import language
import logging
import os
import tempfile
import threading
import time

from config import (
    STORAGE_DIR,
    ADMIN_USER_IDS,
    FREE_DAILY_SUMMARIES,
    FREE_DAILY_QUIZZES,
    FREE_DAILY_QUESTIONS,
    FREE_DAILY_BOOK_PROCESSING,
    FREE_DAILY_FLASHCARD_SETS,
    FREE_SHELF_BOOKS,
    REFERRAL_BONUS_DAYS,
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS

logger = logging.getLogger(__name__)

_ADMIN_DIR = os.path.join(STORAGE_DIR, "_admin")
_INDEX_PATH = os.path.join(_ADMIN_DIR, "users_index.json")

# Every free-tier cap is per UTC day, reset by usage_day (see _load).
FREE_LIMITS = {
    "summaries": FREE_DAILY_SUMMARIES,
    "quizzes": FREE_DAILY_QUIZZES,
    "questions": FREE_DAILY_QUESTIONS,
    "books": FREE_DAILY_BOOK_PROCESSING,
    "flashcards": FREE_DAILY_FLASHCARD_SETS,
}
FEATURE_LABELS = {
    "summaries": "AI chapter summaries",
    "quizzes": "quizzes",
    "questions": "Ask-AI questions",
    "books": "book uploads/chapter splits",
    "flashcards": "flashcard sets",
}


class QuotaExceeded(Exception):
    """Raised when a FREE user has hit their cap on a Claude-token-heavy feature."""

    def __init__(self, feature: str, needed: int = 1, remaining: int = 0):
        self.feature = feature
        label = FEATURE_LABELS.get(feature, feature)
        if FREE_LIMITS.get(feature, 0) == 0:
            msg = (
                f"{label[0].upper() + label[1:]} aren't available in guest mode. Log in to your account "
                "(Shelf > Log in) for full access, or get Premium (⭐ My Plan)."
            )
        elif needed > 1 and remaining > 0:
            # A multi-unit request (a whole-book summary is one unit per
            # chapter) can fail with allowance left -- say so, rather than
            # claiming it's all used up.
            msg = (
                f"That needs {needed} of your free {label} for today, and you have {remaining} left. "
                "Try a single chapter instead, upgrade to Premium (⭐ My Plan) for unlimited access, "
                "or come back tomorrow."
            )
        else:
            msg = (
                f"You've used all {FREE_LIMITS.get(feature, '?')} guest {label} for today. Log in to your "
                "account (Shelf > Log in) or get Premium (⭐ My Plan) for full access, or come back tomorrow."
            )
        super().__init__(msg)


class ShelfLimitReached(Exception):
    """Raised when a FREE user's 📚 Book Shelf is already at FREE_SHELF_BOOKS and they try to add another."""

    def __init__(self, current_count: int):
        self.current_count = current_count
        self.limit = FREE_SHELF_BOOKS
        if FREE_SHELF_BOOKS == 0:
            msg = ("Uploading books isn't available in guest mode. Log in to your account (Shelf > Log in) "
                   "or get Premium (⭐ My Plan) to add your own books.")
        else:
            msg = (f"Guests can keep {FREE_SHELF_BOOKS} books on the shelf, and you have {current_count}. "
                   "Log in to your account or get Premium (⭐ My Plan) for an unlimited shelf.")
        super().__init__(msg)





# ---------------------------------------------------------------------------
# Per-user record
# ---------------------------------------------------------------------------

def _sub_path(user_id: int) -> str:
    return os.path.join(STORAGE_DIR, str(user_id), "subscription.json")


def _period_key(ts: float | None = None) -> str:
    t = time.gmtime(ts if ts is not None else time.time())
    return f"{t.tm_year:04d}-{t.tm_mon:02d}"


def _day_key(ts: float | None = None) -> str:
    """UTC calendar day -- the reset boundary for every free-tier cap (see FREE_LIMITS)."""
    t = time.gmtime(ts if ts is not None else time.time())
    return f"{t.tm_year:04d}-{t.tm_mon:02d}-{t.tm_mday:02d}"


def _default_sub(user_id: int) -> dict:
    return {
        "user_id": user_id,
        "plan": "free",
        "premium_until": None,
        "premium_source": None,
        "usage_day": _day_key(),
        "usage": {"summaries": 0, "quizzes": 0, "questions": 0, "books": 0, "flashcards": 0},
        "trial_used": {"ecg": False, "lab": False},
        "language": "English",
        "referred_by": None,
        "referral_credited": False,
        "referral_credits": [],
        "blocked": False,
        "payments": [],
        "created_at": time.time(),
    }


def _load(user_id: int) -> dict:
    path = _sub_path(user_id)
    data = None
    if os.path.exists(path):
        try:
            with open(path) as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            data = None
    if data is None:
        return _default_sub(user_id)

    default = _default_sub(user_id)
    # Read the stored day BEFORE the setdefault loop below, which would
    # otherwise fill a missing usage_day in with TODAY and so convince the
    # reset further down that a stale record is already current. Records
    # written before caps went daily have no usage_day at all, and their
    # old counters must not be charged against today's allowance.
    stored_day = data.get("usage_day")
    for k, v in default.items():
        data.setdefault(k, v)
    # usage/trial_used are nested dicts -- setdefault above only fills them in
    # wholesale if the WHOLE key was missing (an older record), not if just
    # one sub-field is missing (a newer field added to a still-existing record).
    for k, v in default["usage"].items():
        data["usage"].setdefault(k, v)
    for k, v in default["trial_used"].items():
        data["trial_used"].setdefault(k, v)

    if stored_day != _day_key():
        data["usage_day"] = _day_key()
        data["usage"] = dict(default["usage"])
    return data


def _save(user_id: int, data: dict) -> None:
    path = _sub_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    # A unique temp name per write: two threads saving the same user at
    # once (a worker thread recording a payment while the loop consumes a
    # quota unit) used to share one ".tmp" path and could replace the real
    # file with the other's half-written JSON.
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(path), prefix=".sub-", suffix=".tmp")
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(data, f)
        os.replace(tmp_path, path)
    except BaseException:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
        raise
    # Every write to a user's subscription record means the global admin
    # index needs to know they exist too -- otherwise anything that only
    # ever calls record_payment()/grant_premium() etc. (never the standalone
    # touch_user(), which chat handlers call for its username-capturing
    # side) would be invisible to all_user_ids()-based admin stats/revenue
    # scans despite having real, persisted subscription data.
    try:
        _touch_index(user_id)
    except OSError:
        logger.exception("Failed to update the users index for %s (non-fatal)", user_id)


def get_status(user_id: int) -> dict:
    """Full subscription record for this user (creates/returns defaults if they're new)."""
    return _load(user_id)


def is_premium(user_id: int) -> bool:
    """
    True if currently on an unexpired Premium plan, OR if this user is an
    admin (config.ADMIN_USER_IDS) -- admins get full, unmetered access to
    every Premium-gated feature with no subscription needed, since every
    quota/trial check in this module (check_and_consume,
    is_premium) funnels through this
    one function. Auto-downgrades (and persists) an expired plan back to
    free for everyone else.
    """
    if is_admin(user_id):
        return True
    # A username/password account linked to this Telegram account (logged in
    # in the mini app): one the admin made has full access; one registered
    # after a Stars payment has it while the payer's Premium lasts (which may
    # be a different Telegram account than the one logged in now).
    import accounts  # lazy: accounts imports nothing from here, but keep the import graph flat
    acct = accounts.linked_account(user_id)
    if acct:
        if acct["access"] == "full":
            return True
        payer = acct.get("paid_by")
        if payer and int(payer) != int(user_id) and _own_premium(int(payer)):
            return True
    return _own_premium(user_id)


def _own_premium(user_id: int) -> bool:
    """This Telegram account's own Premium plan (auto-downgrading an expired one)."""
    sub = _load(user_id)
    if sub["plan"] == "premium" and sub["premium_until"] and sub["premium_until"] > time.time():
        return True
    if sub["plan"] == "premium":
        sub["plan"] = "free"
        sub["premium_until"] = None
        _save(user_id, sub)
    return False


# ---------------------------------------------------------------------------
# Global user index -- powers admin stats/broadcast/lookup
# ---------------------------------------------------------------------------

def _load_index() -> dict:
    if not os.path.exists(_INDEX_PATH):
        return {}
    try:
        with open(_INDEX_PATH) as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {}


def _save_index(idx: dict) -> None:
    os.makedirs(_ADMIN_DIR, exist_ok=True)
    tmp_path = _INDEX_PATH + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(idx, f)
    os.replace(tmp_path, _INDEX_PATH)


def _touch_index(user_id: int, username: str | None = None) -> bool:
    """Returns True if this is the first time the bot has ever seen this user."""
    idx = _load_index()
    key = str(user_id)
    existing = idx.get(key)
    entry = existing or {"first_seen": time.time(), "username": None}
    if username:
        entry["username"] = username
    entry["last_seen"] = time.time()
    idx[key] = entry
    _save_index(idx)
    return existing is None


def touch_user(user_id: int, username: str | None = None) -> bool:
    """
    Record that this user is known to the bot, and (when given) their
    current @username -- call this from entry points where a username is
    actually available (e.g. /start), since _save() above already keeps
    last_seen fresh on every subscription write regardless of username.

    Returns True if this was the user's FIRST ever contact with the bot.
    /start uses that to decide whether a referral deep link counts: the
    index is the only record written for a user who has merely started the
    bot, so it's the one dependable "have we seen you before?" signal.
    """
    return _touch_index(user_id, username)


def all_user_ids() -> list[int]:
    return [int(k) for k in _load_index().keys()]


def find_user_id_by_username(username: str) -> int | None:
    username = username.lstrip("@").lower()
    for uid_str, entry in _load_index().items():
        if (entry.get("username") or "").lower() == username:
            return int(uid_str)
    return None


def get_username(user_id: int) -> str | None:
    """The @username last captured for this user (via touch_user), or None if never seen/not set."""
    return _load_index().get(str(user_id), {}).get("username")


# ---------------------------------------------------------------------------
# Plan management
# ---------------------------------------------------------------------------

def grant_premium(user_id: int, days: int, source: str = "admin") -> float:
    """
    Grant/extend Premium by `days`. If the user already has unexpired
    Premium, this EXTENDS from their current expiry rather than resetting
    the clock to now -- a renewal payment (or a referral bonus landing on
    top of a paid plan) should always add on top, never shorten what they
    already had. Returns the new premium_until timestamp.
    """
    sub = _load(user_id)
    now = time.time()
    base = sub["premium_until"] if (sub["plan"] == "premium" and sub["premium_until"] and sub["premium_until"] > now) else now
    sub["plan"] = "premium"
    sub["premium_until"] = base + days * 86400
    sub["premium_source"] = source
    _save(user_id, sub)
    return sub["premium_until"]


def revoke_premium(user_id: int) -> None:
    sub = _load(user_id)
    sub["plan"] = "free"
    sub["premium_until"] = None
    sub["premium_source"] = None
    _save(user_id, sub)


def set_blocked(user_id: int, blocked: bool = True) -> None:
    sub = _load(user_id)
    sub["blocked"] = blocked
    _save(user_id, sub)


def is_blocked(user_id: int) -> bool:
    return _load(user_id).get("blocked", False)


def set_language(user_id: int, language: str) -> None:
    sub = _load(user_id)
    sub["language"] = language
    _save(user_id, sub)


def get_language(user_id: int) -> str:
    # Normalized on read so a language later dropped from the offered list
    # resolves to English everywhere at once -- every AI prompt, every
    # translated label -- without a migration over stored records.
    return language.normalize(_load(user_id).get("language"))


# ---------------------------------------------------------------------------
# Quota enforcement -- called by chapter_flow.py/webapp_api.py right before
# an AI-costing action actually runs.
# ---------------------------------------------------------------------------

def check_and_consume(user_id: int, feature: str) -> None:
    """
    feature in {"summaries", "quizzes", "questions"}. Premium users always
    pass (their usage is still counted, for visibility on the admin/customer
    dashboards, just never enforced). Free users get FREE_LIMITS[feature]
    uses per UTC day; raises QuotaExceeded once that's used up.
    """
    consume(user_id, feature, 1)


# Guards every read-modify-write of a usage counter. Quota calls come from
# the event loop AND from worker threads (jobs), so without it two
# concurrent consumes can both read "2 used" and both write "3".
_usage_lock = threading.RLock()


def consume(user_id: int, feature: str, units: int = 1) -> None:
    """
    Take `units` from today's allowance for `feature`, or raise
    QuotaExceeded if a free user doesn't have that many left. A whole-book
    summary costs one unit per chapter, not one in total -- it is that many
    AI calls. Pair every consume with refund() on the failure path, so a
    request that fails (timeout, not found, AI error) costs the user nothing.
    """
    units = max(1, int(units))
    with _usage_lock:
        premium = is_premium(user_id)
        sub = _load(user_id)  # after is_premium(), which may have auto-downgraded and saved
        used = sub["usage"].get(feature, 0)
        if not premium and used + units > FREE_LIMITS[feature]:
            raise QuotaExceeded(feature, needed=units, remaining=max(0, FREE_LIMITS[feature] - used))
        sub["usage"][feature] = used + units
        _save(user_id, sub)


def refund(user_id: int, feature: str, units: int = 1) -> None:
    """Give back units taken by consume() for a request that then failed. Never goes below zero."""
    units = max(1, int(units))
    try:
        with _usage_lock:
            sub = _load(user_id)
            sub["usage"][feature] = max(0, sub["usage"].get(feature, 0) - units)
            _save(user_id, sub)
    except Exception:
        # A refund that fails must not turn the user's original error into
        # a different one -- they already have a failure to read.
        logger.exception("Quota refund failed for user %s (%s x%s)", user_id, feature, units)


class metered:
    """
    `with subscriptions.metered(uid, "questions"): ...` -- consumes on
    entry, refunds automatically if the block raises (including a timeout
    or cancellation). Call .keep() never; success is simply not raising.
    """

    def __init__(self, user_id: int, feature: str, units: int = 1):
        self.user_id, self.feature, self.units = user_id, feature, units

    def __enter__(self):
        consume(self.user_id, self.feature, self.units)
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            refund(self.user_id, self.feature, self.units)
        return False


# Shelf slots promised to uploads still in progress, per user. The capacity
# check used to run before a minutes-long download+split and the book was
# only added at the end, so five PDFs sent at once all passed a 2-book
# limit. In-process is enough: the bot and the mini app API share one
# process and one event loop.
_pending_shelf: dict[int, int] = {}
_pending_lock = threading.Lock()


class shelf_slot:
    """
    Reserve one shelf slot for an upload: `with subscriptions.shelf_slot(uid,
    current_count):` raises ShelfLimitReached if the shelf plus uploads
    already in flight is full, and releases the reservation on exit
    (the finished book then counts through current_count instead).
    """

    def __init__(self, user_id: int, current_count: int):
        self.user_id, self.current_count = user_id, current_count

    def __enter__(self):
        with _pending_lock:
            pending = _pending_shelf.get(self.user_id, 0)
            check_shelf_capacity(self.user_id, self.current_count + pending)
            _pending_shelf[self.user_id] = pending + 1
        return self

    def __exit__(self, exc_type, exc, tb):
        with _pending_lock:
            left = _pending_shelf.get(self.user_id, 1) - 1
            if left > 0:
                _pending_shelf[self.user_id] = left
            else:
                _pending_shelf.pop(self.user_id, None)
        return False


def shelf_limit(user_id: int) -> int | None:
    """How many books this user may keep on the shelf at once; None means unlimited (Premium/admin)."""
    return None if is_premium(user_id) else FREE_SHELF_BOOKS


def check_shelf_capacity(user_id: int, current_count: int) -> None:
    """
    Raise ShelfLimitReached if adding one more book would put a free user
    over FREE_SHELF_BOOKS. Premium users always pass.

    Takes the count rather than reading it so this module stays free of an
    import of library.py (which would be a cycle risk, and would drag a
    per-user registry read into a module that only deals in plans/usage).
    Callers pass len(library.list_books(user_id)).

    Nothing is consumed or written here -- unlike check_and_consume(), this
    is a standing limit on what's currently stored, re-evaluated from the
    live shelf each time, so deleting a book immediately frees the slot.
    """
    if is_premium(user_id):
        return
    if current_count >= FREE_SHELF_BOOKS:
        raise ShelfLimitReached(current_count)


def usage_summary(user_id: int) -> dict:
    """Free-tier usage counters plus each limit, for the 'My Plan' panel."""
    sub = _load(user_id)
    return {
        "usage": sub["usage"],
        "limits": FREE_LIMITS,
    }


# ---------------------------------------------------------------------------
# Payments (Telegram Stars)
# ---------------------------------------------------------------------------

def record_payment(user_id: int, stars: int, days: int, source: str = "stars", charge_id: str | None = None) -> None:
    """
    Append a payment to the user's history WITHOUT granting anything or
    checking for duplicates. Telegram Stars payments go through
    apply_payment() instead, which does both; this stays for callers that
    log a payment taken some other way.
    """
    sub = _load(user_id)
    entry = {"stars": stars, "days": days, "source": source, "ts": time.time()}
    if charge_id:
        entry["charge_id"] = charge_id
    sub["payments"].append(entry)
    _save(user_id, sub)


def find_payment(user_id: int, charge_id: str) -> dict | None:
    """
    The payment (not refund) entry recorded for this
    telegram_payment_charge_id, or None. Payments recorded before charge ids
    were stored have no "charge_id" and are never matched.
    """
    if not charge_id:
        return None
    for p in _load(user_id).get("payments", []):
        if p.get("charge_id") == charge_id and p.get("source") != "refund":
            return p
    return None


def apply_payment(user_id: int, charge_id: str, stars: int, days: int, source: str) -> bool:
    """
    Record a Telegram Stars payment and grant the Premium days it bought,
    exactly once per telegram_payment_charge_id. Returns False (changing
    nothing) when that charge is already on file.

    Why this exists rather than grant_premium() + record_payment(): Telegram
    re-delivers a successful_payment update if the bot didn't acknowledge
    it (a crash or restart mid-handler), and each delivery used to grant
    Premium again. The duplicate check, the premium extension and the
    history entry happen here in ONE load/save, so there's no point at
    which the grant has landed but the charge id that guards it hasn't.
    The extension rule is grant_premium()'s: on top of unexpired Premium,
    otherwise from now. `days` may be 0 (a 📚 Requested Book payment).

    Held under _usage_lock because quota consumes in worker threads
    read-modify-write this same file; without it a consume() that loaded
    before this save would write back a record missing the payment.
    """
    with _usage_lock:
        sub = _load(user_id)
        for p in sub["payments"]:
            if p.get("charge_id") == charge_id and p.get("source") != "refund":
                return False
        now = time.time()
        if days > 0:
            active = sub["plan"] == "premium" and sub["premium_until"] and sub["premium_until"] > now
            base = sub["premium_until"] if active else now
            sub["plan"] = "premium"
            sub["premium_until"] = base + days * 86400
            sub["premium_source"] = "stars"
        sub["payments"].append(
            {"stars": stars, "days": days, "source": source, "charge_id": charge_id, "ts": now}
        )
        _save(user_id, sub)
        return True


def record_refund(user_id: int, charge_id: str) -> dict | None:
    """
    Book-keeping for a Stars payment the admin has just refunded through
    Telegram (customer_flow.py's /refund): marks the original payment
    refunded, takes back the Premium days it bought, and appends a refund
    entry with NEGATIVE stars/days so get_revenue_stats() nets it out and
    the 🧾 history shows it.

    Premium is shortened by the refunded days rather than revoked outright
    (revoke_premium()), because the same expiry may also carry other paid
    renewals, referral bonuses or admin grants that this refund didn't
    pay for. If what's left is already in the past, the user drops to free.

    Returns a copy of the original payment entry, or None if no payment
    with that charge id is on file (the refund entry is still written, with
    0 stars, so the refund itself is never lost from the record).
    """
    with _usage_lock:
        sub = _load(user_id)
        original = None
        for p in sub["payments"]:
            if p.get("charge_id") == charge_id and p.get("source") != "refund":
                original = p
                break

        now = time.time()
        stars = days = 0
        if original is not None:
            original["refunded_at"] = now
            stars = original.get("stars", 0)
            days = original.get("days", 0)
            if days > 0 and sub["plan"] == "premium" and sub["premium_until"]:
                new_until = sub["premium_until"] - days * 86400
                if new_until <= now:
                    sub["plan"] = "free"
                    sub["premium_until"] = None
                    sub["premium_source"] = None
                else:
                    sub["premium_until"] = new_until

        sub["payments"].append(
            {
                "stars": -stars,
                "days": -days,
                "source": "refund",
                "charge_id": charge_id,
                "refund_of": original.get("source") if original else None,
                "ts": now,
            }
        )
        # The payment that earned a referral bonus is refunded: both bonuses
        # go too (they used to stay -- two accounts and one refund were 60
        # free days).
        referrer_id = sub.get("referred_by")
        if (
            original is not None and charge_id and sub.get("referral_credit_charge") == charge_id
            and not sub.get("referral_clawed_back")
        ):
            _shorten_premium(sub, REFERRAL_BONUS_DAYS, now)
            sub["referral_clawed_back"] = True
            if referrer_id:
                referrer = _load(referrer_id)
                _shorten_premium(referrer, REFERRAL_BONUS_DAYS, now)
                _save(referrer_id, referrer)
        _save(user_id, sub)
        return dict(original) if original is not None else None


def has_paid_premium(user_id: int) -> bool:
    """A Premium plan bought with Stars and not refunded: what lets a user register their own account."""
    return any(
        p.get("source") == "stars" and (p.get("days") or 0) > 0 and not p.get("refunded_at")
        for p in _load(user_id).get("payments") or []
    )


def has_ever_paid(user_id: int) -> bool:
    return bool(_load(user_id).get("payments"))


# ---------------------------------------------------------------------------
# Referrals
# ---------------------------------------------------------------------------

def get_referral_link_payload(user_id: int) -> str:
    """
    The bot's own Telegram id doubles as its referral code -- no separate
    opaque-code registry needed. bot.py builds the full t.me/<bot>?start=...
    deep link around this payload.
    """
    return f"ref_{user_id}"


def register_referral(new_user_id: int, referrer_id: int) -> bool:
    """
    Called from /start when a brand-new user arrives via a ?start=ref_<id>
    deep link. Only takes effect for a genuinely NEW user -- an existing
    user re-tapping a referral link doesn't retroactively get a referrer,
    and self-referral is rejected. Returns True if the referral was
    recorded.

    The subscription-file check below is NOT sufficient on its own to prove
    a user is new: someone who has only ever tapped /start has no
    subscription file at all (touch_user writes the users index, and it's
    _save that creates the file), so this check alone would let such a user
    be retroactively claimed by whoever's referral link they next tapped --
    miscrediting the referrer and skewing the admin's signup-source split.
    Callers must therefore gate on touch_user()'s "first ever contact"
    return value; this stays as a second line of defense.
    """
    if referrer_id == new_user_id:
        return False
    # Only a real user of this bot can be a referrer: any integer in the link
    # used to be accepted (a never-seen id got Premium and a phantom record,
    # and a group id the bot is in received the credit message).
    if not isinstance(referrer_id, int) or referrer_id <= 0 or referrer_id not in set(all_user_ids()):
        return False
    if os.path.exists(_sub_path(new_user_id)):
        return False
    sub = _default_sub(new_user_id)
    sub["referred_by"] = referrer_id
    _save(new_user_id, sub)
    return True


def maybe_credit_referral(user_id: int, charge_id: str | None = None) -> int | None:
    """
    Call right after a user's FIRST successful Stars payment. If they were
    referred and haven't been credited yet, grants REFERRAL_BONUS_DAYS of
    Premium to BOTH the referrer and this user, on top of whatever plan they
    just bought. Returns the referrer's user_id if a credit was granted,
    else None (not referred, or already credited).

    The credit is also logged onto the REFERRER's own record
    (referral_credits) -- not just applied via grant_premium -- so
    get_referral_stats()/get_referral_leaderboard() have an exact, permanent
    history of who each affiliate actually converted and how many bonus
    days they've earned, independent of whatever REFERRAL_BONUS_DAYS is
    configured to today.
    """
    sub = _load(user_id)
    if sub.get("referral_credited") or not sub.get("referred_by"):
        return None

    referrer_id = sub["referred_by"]
    grant_premium(referrer_id, REFERRAL_BONUS_DAYS, source="referral")
    grant_premium(user_id, REFERRAL_BONUS_DAYS, source="referral")

    referrer_sub = _load(referrer_id)
    referrer_sub.setdefault("referral_credits", []).append(
        {"ts": time.time(), "days": REFERRAL_BONUS_DAYS, "referred_user_id": user_id}
    )
    _save(referrer_id, referrer_sub)

    sub = _load(user_id)
    sub["referral_credited"] = True
    # Remembered so that refunding this payment takes both bonuses back.
    sub["referral_credit_charge"] = charge_id
    _save(user_id, sub)
    return referrer_id


def _shorten_premium(sub: dict, days: int, now: float) -> None:
    if days > 0 and sub.get("plan") == "premium" and sub.get("premium_until"):
        new_until = sub["premium_until"] - days * 86400
        if new_until <= now:
            sub["plan"] = "free"
            sub["premium_until"] = None
            sub["premium_source"] = None
        else:
            sub["premium_until"] = new_until


def get_referral_stats(user_id: int) -> dict:
    """
    Rolled-up numbers for the 🤝 Affiliate Program panel (customer_flow.py's
    handle_referral): how many people signed up via this user's link,
    how many of those went on to convert (first Premium payment, crediting
    the bonus), and the total bonus days earned from referrals so far.
    referred_count scans every user's record the same way get_bot_stats/
    get_revenue_stats already do -- fine at this bot's scale, and keeps this
    consistent with those rather than needing a separate reverse-index.
    """
    referred_count = 0
    for uid in all_user_ids():
        if uid == user_id:
            continue
        if _load(uid).get("referred_by") == user_id:
            referred_count += 1

    credits = _load(user_id).get("referral_credits", [])
    return {
        "referred_count": referred_count,
        "converted_count": len(credits),
        "bonus_days_earned": sum(c.get("days", 0) for c in credits),
    }


def get_referral_leaderboard(limit: int = 10) -> list[dict]:
    """
    Top affiliates by converted referrals, for the admin panel's
    "🤝 Top Referrers" view -- who's actually driving paying signups, not
    just link clicks (referred_count without a conversion is free to fake
    by spamming the link; converted_count means a real Premium purchase
    happened).
    """
    rows = []
    for uid in all_user_ids():
        credits = _load(uid).get("referral_credits", [])
        if not credits:
            continue
        rows.append(
            {
                "user_id": uid,
                "converted_count": len(credits),
                "bonus_days_earned": sum(c.get("days", 0) for c in credits),
            }
        )
    rows.sort(key=lambda r: r["converted_count"], reverse=True)
    return rows[:limit]


# ---------------------------------------------------------------------------
# Admin stats
# ---------------------------------------------------------------------------

def get_bot_stats() -> dict:
    """Rolled-up numbers for the admin panel's 📊 Bot-wide stats view."""
    ids = all_user_ids()
    now = time.time()
    premium_count = 0
    referred_signups = 0
    referral_conversions = 0
    for uid in ids:
        sub = _load(uid)
        if sub["plan"] == "premium" and sub["premium_until"] and sub["premium_until"] > now:
            premium_count += 1
        if sub.get("referred_by"):
            referred_signups += 1
        referral_conversions += len(sub.get("referral_credits", []))

    idx = _load_index()
    active_7d = sum(1 for e in idx.values() if now - e.get("last_seen", 0) <= 7 * 86400)
    active_30d = sum(1 for e in idx.values() if now - e.get("last_seen", 0) <= 30 * 86400)

    return {
        "total_users": len(ids),
        "premium_users": premium_count,
        "active_7d": active_7d,
        "active_30d": active_30d,
        # How each user arrived. There are exactly two routes into the bot:
        # a "?start=ref_<id>" referral deep link (register_referral stamps
        # referred_by), or anything else -- the plain t.me/<bot> link, a
        # search, a forward. So "direct" is the remainder by definition, not
        # a separately-tracked signal, and the two always sum to total_users.
        "referred_signups": referred_signups,
        "direct_signups": len(ids) - referred_signups,
        "referral_conversions": referral_conversions,
    }


def get_revenue_stats(period: str | None = None) -> dict:
    """Stars collected and payment count for one calendar month (default: current)."""
    period = period or _period_key()
    stars_total = 0
    payments_count = 0
    for uid in all_user_ids():
        for p in _load(uid).get("payments", []):
            if _period_key(p["ts"]) == period:
                stars_total += p["stars"]
                payments_count += 1
    return {"period": period, "stars_total": stars_total, "payments_count": payments_count}
