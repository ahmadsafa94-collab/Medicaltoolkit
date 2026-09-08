"""
Validates Telegram Mini App "initData" -- the signed payload Telegram's
WebApp JS SDK hands the frontend when it opens, which the frontend must
forward on every API call (see webapp/app.js's fetch wrapper).

This is the ONLY trustworthy way to know which real Telegram user is
calling the Book Shelf API. The mini app is just a web page loaded in an
in-app browser -- without verifying this signature, anyone who obtained the
page's URL could open it directly and claim to be any user id they like by
just... saying so in a request. Every API endpoint in webapp_api.py must
authenticate through validate_init_data(); nothing should ever trust a
plain, unsigned user id from a request body or query string.

Algorithm is Telegram's own, documented at:
https://core.telegram.org/bots/webapps#validating-data-received-via-the-mini-app
"""

import hashlib
import hmac
import json
import time
from urllib.parse import parse_qsl

from config import TELEGRAM_BOT_TOKEN

# initData is minted once when the Mini App is opened. Reject anything older
# than this so a leaked/logged initData string (e.g. from a proxy log)
# can't be replayed indefinitely -- generous enough that a long reading or
# quiz session in one opening of the app never gets logged out mid-way.
MAX_INIT_DATA_AGE_SECONDS = 24 * 60 * 60


class AuthError(Exception):
    pass


def _secret_key() -> bytes:
    return hmac.new(b"WebAppData", TELEGRAM_BOT_TOKEN.encode(), hashlib.sha256).digest()


def validate_init_data(init_data: str) -> dict:
    """
    Verify the HMAC signature and freshness of a raw initData string and
    return the authenticated Telegram user dict (at minimum {"id": int, ...}).
    Raises AuthError on ANY failure -- callers must turn that into an HTTP
    401 and never fall back to trusting an unsigned/unverified user id.
    """
    if not init_data:
        raise AuthError("Missing Telegram sign-in data -- please reopen the Book Shelf from the bot.")

    try:
        pairs = dict(parse_qsl(init_data, strict_parsing=True))
    except ValueError:
        raise AuthError("Malformed Telegram sign-in data.")

    received_hash = pairs.pop("hash", None)
    if not received_hash:
        raise AuthError("Sign-in data is missing its signature.")

    data_check_string = "\n".join(f"{k}={v}" for k, v in sorted(pairs.items()))
    computed_hash = hmac.new(_secret_key(), data_check_string.encode(), hashlib.sha256).hexdigest()

    if not hmac.compare_digest(computed_hash, received_hash):
        raise AuthError("Sign-in data failed its signature check -- this request did not come from Telegram.")

    auth_date = pairs.get("auth_date")
    if not auth_date or not auth_date.isdigit():
        raise AuthError("Sign-in data is missing a valid timestamp.")
    age = time.time() - int(auth_date)
    if age > MAX_INIT_DATA_AGE_SECONDS:
        raise AuthError("This session has expired -- please reopen the Book Shelf from the bot.")
    if age < -300:  # allow a little clock skew, but a far-future auth_date is nonsensical
        raise AuthError("Sign-in data's timestamp is in the future.")

    user_raw = pairs.get("user")
    if not user_raw:
        raise AuthError("Sign-in data is missing user info.")
    try:
        user = json.loads(user_raw)
    except json.JSONDecodeError:
        raise AuthError("Sign-in data's user field is not valid JSON.")

    if "id" not in user or not isinstance(user["id"], int):
        raise AuthError("Sign-in data's user is missing a valid id.")

    return user
