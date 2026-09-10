"""
Free/Premium plan tracking -- the foundation the admin panel, customer "My
Plan" panel, Telegram Stars payments, and every AI-costing feature's quota
check all sit on top of.

Storage layout (small per-purpose JSON files, same pattern as library.py):
  STORAGE_DIR/{user_id}/subscription.json   -- one user's plan/usage/trials
  STORAGE_DIR/_admin/users_index.json       -- {user_id: {first_seen, last_seen, username}}
                                                for admin stats/broadcast/lookup, since there's
                                                otherwise no way to enumerate "every user who has
                                                ever talked to this bot" from per-user files alone.

Design notes:
  - Free-tier usage counters (summaries/quizzes/questions) reset automatically
    the first time they're touched in a new calendar month (UTC) -- no cron
    job needed, see _period_key()/_load().
  - Premium is a plain expiry timestamp (premium_until), extended (not
    replaced) by a renewal/grant that arrives before the current one lapses
    -- see grant_premium(). is_premium() lazily downgrades an expired user
    back to "free" the next time anything checks their status.
  - ECG/lab interpretation use a SEPARATE one-shot trial mechanism
    (trial_used) rather than the monthly counters: once ever, not once a
    month, per the "give one free try" ask.
"""

import json
import logging
import os
import time

from config import (
    STORAGE_DIR,
    ADMIN_USER_IDS,
    FREE_MONTHLY_SUMMARIES,
    FREE_MONTHLY_QUIZZES,
    FREE_MONTHLY_QUESTIONS,
    ECG_FREE_TRIALS,
    LAB_FREE_TRIALS,
    REFERRAL_BONUS_DAYS,
)


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_USER_IDS

logger = logging.getLogger(__name__)

_ADMIN_DIR = os.path.join(STORAGE_DIR, "_admin")
_INDEX_PATH = os.path.join(_ADMIN_DIR, "users_index.json")

FREE_LIMITS = {
    "summaries": FREE_MONTHLY_SUMMARIES,
    "quizzes": FREE_MONTHLY_QUIZZES,
    "questions": FREE_MONTHLY_QUESTIONS,
}
FEATURE_LABELS = {
    "summaries": "AI chapter summaries",
    "quizzes": "quizzes",
    "questions": "Ask-AI questions",
}
TRIAL_LIMITS = {"ecg": ECG_FREE_TRIALS, "lab": LAB_FREE_TRIALS}
TRIAL_LABELS = {"ecg": "ECG interpretation", "lab": "lab interpretation"}


class QuotaExceeded(Exception):
    """Raised when a FREE user has hit their monthly cap on a Claude-token-heavy feature."""

    def __init__(self, feature: str):
        self.feature = feature
        super().__init__(
            f"You've used all {FREE_LIMITS.get(feature, '?')} free {FEATURE_LABELS.get(feature, feature)} "
            "this month. Upgrade to Premium (⭐ My Plan) for unlimited access, or wait for next month's reset."
        )


class PremiumRequired(Exception):
    """Raised when a FREE user has used up their one-time trial of a Premium-only feature."""

    def __init__(self, feature: str):
        self.feature = feature
        label = TRIAL_LABELS.get(feature, feature)
        super().__init__(
            f"You've used your one free {label} trial. {label.capitalize()} is a Premium feature -- "
            "upgrade via ⭐ My Plan to keep using it."
        )


# ---------------------------------------------------------------------------
# Per-user record
# ---------------------------------------------------------------------------

def _sub_path(user_id: int) -> str:
    return os.path.join(STORAGE_DIR, str(user_id), "subscription.json")


def _period_key(ts: float | None = None) -> str:
    t = time.gmtime(ts if ts is not None else time.time())
    return f"{t.tm_year:04d}-{t.tm_mon:02d}"


def _default_sub(user_id: int) -> dict:
    return {
        "user_id": user_id,
        "plan": "free",
        "premium_until": None,
        "premium_source": None,
        "usage_period": _period_key(),
        "usage": {"summaries": 0, "quizzes": 0, "questions": 0},
        "trial_used": {"ecg": False, "lab": False},
        "language": "English",
        "referred_by": None,
        "referral_credited": False,
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
    for k, v in default.items():
        data.setdefault(k, v)
    # usage/trial_used are nested dicts -- setdefault above only fills them in
    # wholesale if the WHOLE key was missing (an older record), not if just
    # one sub-field is missing (a newer field added to a still-existing record).
    for k, v in default["usage"].items():
        data["usage"].setdefault(k, v)
    for k, v in default["trial_used"].items():
        data["trial_used"].setdefault(k, v)

    if data.get("usage_period") != _period_key():
        data["usage_period"] = _period_key()
        data["usage"] = {"summaries": 0, "quizzes": 0, "questions": 0}
    return data


def _save(user_id: int, data: dict) -> None:
    path = _sub_path(user_id)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp_path = path + ".tmp"
    with open(tmp_path, "w") as f:
        json.dump(data, f)
    os.replace(tmp_path, path)
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
    check_and_consume_trial, can_use_trial_or_premium) funnels through this
    one function. Auto-downgrades (and persists) an expired plan back to
    free for everyone else.
    """
    if is_admin(user_id):
        return True
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


def _touch_index(user_id: int, username: str | None = None) -> None:
    idx = _load_index()
    key = str(user_id)
    entry = idx.get(key) or {"first_seen": time.time(), "username": None}
    if username:
        entry["username"] = username
    entry["last_seen"] = time.time()
    idx[key] = entry
    _save_index(idx)


def touch_user(user_id: int, username: str | None = None) -> None:
    """
    Record that this user is known to the bot, and (when given) their
    current @username -- call this from entry points where a username is
    actually available (e.g. /start), since _save() above already keeps
    last_seen fresh on every subscription write regardless of username.
    """
    _touch_index(user_id, username)


def all_user_ids() -> list[int]:
    return [int(k) for k in _load_index().keys()]


def find_user_id_by_username(username: str) -> int | None:
    username = username.lstrip("@").lower()
    for uid_str, entry in _load_index().items():
        if (entry.get("username") or "").lower() == username:
            return int(uid_str)
    return None


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
    return _load(user_id).get("language", "English")


# ---------------------------------------------------------------------------
# Quota enforcement -- called by chapter_flow.py/webapp_api.py right before
# an AI-costing action actually runs.
# ---------------------------------------------------------------------------

def check_and_consume(user_id: int, feature: str) -> None:
    """
    feature in {"summaries", "quizzes", "questions"}. Premium users always
    pass (their usage is still counted, for visibility on the admin/customer
    dashboards, just never enforced). Free users get FREE_LIMITS[feature]
    uses per calendar month; raises QuotaExceeded once that's used up.
    """
    sub = _load(user_id)
    premium = is_premium(user_id)
    if premium:
        sub = _load(user_id)  # is_premium() may have just auto-downgraded/saved -- reload to stay consistent

    used = sub["usage"].get(feature, 0)
    if not premium and used >= FREE_LIMITS[feature]:
        raise QuotaExceeded(feature)

    sub["usage"][feature] = used + 1
    _save(user_id, sub)


def can_use_trial_or_premium(user_id: int, feature: str) -> bool:
    """
    Read-only peek (never consumes the trial) -- use this BEFORE prompting
    the user for input (e.g. "send a photo"), so a user who's already used
    their trial sees the upgrade prompt immediately instead of being asked
    to upload a photo first and only THEN told no. Call
    check_and_consume_trial() at the point the input is actually submitted.
    """
    if is_premium(user_id):
        return True
    return not _load(user_id)["trial_used"].get(feature, False)


def check_and_consume_trial(user_id: int, feature: str) -> None:
    """
    feature in {"ecg", "lab"}. Premium users always pass. Free users get
    exactly TRIAL_LIMITS[feature] uses EVER (not per month) before
    PremiumRequired is raised.
    """
    sub = _load(user_id)
    if is_premium(user_id):
        return

    sub = _load(user_id)
    if not sub["trial_used"].get(feature, False):
        sub["trial_used"][feature] = True
        _save(user_id, sub)
        return
    raise PremiumRequired(feature)


def usage_summary(user_id: int) -> dict:
    """Free-tier usage counters plus each limit, for the 'My Plan' panel."""
    sub = _load(user_id)
    return {
        "usage": sub["usage"],
        "limits": FREE_LIMITS,
        "trial_used": sub["trial_used"],
    }


# ---------------------------------------------------------------------------
# Payments (Telegram Stars)
# ---------------------------------------------------------------------------

def record_payment(user_id: int, stars: int, days: int, source: str = "stars") -> None:
    sub = _load(user_id)
    sub["payments"].append({"stars": stars, "days": days, "source": source, "ts": time.time()})
    _save(user_id, sub)


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
    deep link. Only takes effect for a genuinely NEW user (no subscription
    record yet) -- an existing user re-tapping a referral link doesn't
    retroactively get a referrer, and self-referral is rejected. Returns
    True if the referral was recorded.
    """
    if referrer_id == new_user_id:
        return False
    if os.path.exists(_sub_path(new_user_id)):
        return False
    sub = _default_sub(new_user_id)
    sub["referred_by"] = referrer_id
    _save(new_user_id, sub)
    return True


def maybe_credit_referral(user_id: int) -> int | None:
    """
    Call right after a user's FIRST successful Stars payment. If they were
    referred and haven't been credited yet, grants REFERRAL_BONUS_DAYS of
    Premium to BOTH the referrer and this user, on top of whatever plan they
    just bought. Returns the referrer's user_id if a credit was granted,
    else None (not referred, or already credited).
    """
    sub = _load(user_id)
    if sub.get("referral_credited") or not sub.get("referred_by"):
        return None

    referrer_id = sub["referred_by"]
    grant_premium(referrer_id, REFERRAL_BONUS_DAYS, source="referral")
    grant_premium(user_id, REFERRAL_BONUS_DAYS, source="referral")

    sub = _load(user_id)
    sub["referral_credited"] = True
    _save(user_id, sub)
    return referrer_id


# ---------------------------------------------------------------------------
# Admin stats
# ---------------------------------------------------------------------------

def get_bot_stats() -> dict:
    """Rolled-up numbers for the admin panel's 📊 Bot-wide stats view."""
    ids = all_user_ids()
    now = time.time()
    premium_count = 0
    for uid in ids:
        sub = _load(uid)
        if sub["plan"] == "premium" and sub["premium_until"] and sub["premium_until"] > now:
            premium_count += 1

    idx = _load_index()
    active_7d = sum(1 for e in idx.values() if now - e.get("last_seen", 0) <= 7 * 86400)
    active_30d = sum(1 for e in idx.values() if now - e.get("last_seen", 0) <= 30 * 86400)

    return {
        "total_users": len(ids),
        "premium_users": premium_count,
        "active_7d": active_7d,
        "active_30d": active_30d,
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
