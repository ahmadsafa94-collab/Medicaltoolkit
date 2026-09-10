"""
Fixed (persistent) keyboard shown at the bottom of the chat, plus the
inline-search trigger button used for drug-name autocomplete.
"""

from aiogram.types import (
    ReplyKeyboardMarkup,
    KeyboardButton,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
)

BTN_SHELF = "📚 BOOK SHELF"
BTN_CLINICAL_TOOLS = "🩺 Clinical Tools"
BTN_STUDY_TOOLS = "🧠 Study Tools"
BTN_MY_PLAN = "⭐ My Plan"
BTN_ADMIN = "🛠 Admin Panel"
BTN_FEEDBACK = "🐞 Report a problem"
BTN_SUPPORT = "🆘 Support"


def main_menu_kb(webapp_url: str = "", is_admin: bool = False) -> ReplyKeyboardMarkup:
    """
    Built as a function (not a module-level constant) so the Book Shelf row
    can be left out entirely when webapp_url isn't configured yet.

    This is the bot's single surface for "what can I do here". Deliberately
    small: almost everything that used to be its own fixed-keyboard button
    now lives one tap deeper, under one of two inline submenus, so this top
    level stays scannable as more features get added:

      - BTN_CLINICAL_TOOLS opens clinical_tools_kb() below: ECG/lab
        interpretation, calculators, Ask About Drugs, drug interactions, and
        drug lookup.
      - BTN_STUDY_TOOLS opens study_tools_kb() below: flashcards, OSCE
        practice, the PDF splitter, notes, Ask My Books, and bookmarks.

    BTN_SHELF ("📚 BOOK SHELF") is deliberately first and alone on its own
    row -- the closest a plain-text ReplyKeyboardMarkup button (no bold/size
    control in the Bot API) can get to looking "big" is a full-width row of
    its own with no sibling button splitting it, plus the all-caps label.
    BTN_MY_PLAN opens the customer subscription panel (customer_flow.py).
    BTN_ADMIN only appears for Telegram user ids in config.ADMIN_USER_IDS
    (admin_flow.py) -- and for those same ids, subscriptions.is_premium()
    always returns True, so nothing under either submenu is ever
    quota-limited or gated behind Premium for an admin. BTN_FEEDBACK opens
    the same "message the admin" prompt as /feedback (customer_flow.py's
    _prompt_feedback) for reporting a bug or suggesting something -- logged
    durably (admin_log.record_report) as well as forwarded live, so it backs
    the admin panel's "🐞 Reported problems" view. BTN_SUPPORT
    (bot.py's btn_support) opens a direct Telegram chat with the admin (a
    URL button, config.SUPPORT_ADMIN_USERNAME) when that's configured, or
    falls back to the same live-forward prompt as Report a problem
    (customer_flow.py's _prompt_support) when it isn't.

    IMPORTANT: the Book Shelf button here is a PLAIN text button, not a
    `web_app` KeyboardButton. An earlier version attached web_app directly
    to this reply-keyboard button, which is the officially-documented way
    to launch a Mini App -- but confirmed in production (Android, Telegram
    9.6) that Telegram sends an empty initData for a web_app launched from
    a custom reply keyboard, while a web_app button attached to an actual
    message (an InlineKeyboardButton) works correctly. Tapping this button
    now just triggers open_book_shelf() in bot.py, which sends a fresh
    message with a real inline web_app button -- see that handler for the
    actual Mini App launch. The bot's Chat Menu Button (set once at startup
    via bot.set_chat_menu_button, also a web_app launch) is the other,
    redundant, confirmed-working entry point.
    """
    rows = []
    if webapp_url:
        rows.append([KeyboardButton(text=BTN_SHELF)])
    rows.append([KeyboardButton(text=BTN_CLINICAL_TOOLS), KeyboardButton(text=BTN_STUDY_TOOLS)])
    rows.append([KeyboardButton(text=BTN_MY_PLAN), KeyboardButton(text=BTN_SUPPORT)])
    rows.append([KeyboardButton(text=BTN_FEEDBACK)])
    if is_admin:
        rows.append([KeyboardButton(text=BTN_ADMIN)])
    return ReplyKeyboardMarkup(keyboard=rows, resize_keyboard=True, is_persistent=True)


def drug_sections_kb(cache_id: str, sections_available: list[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """
    Buttons for each available label section (Dosage, Contraindications, etc.)
    plus a 'show everything' option. Tapping one sends just that section.
    sections_available: list of (concept_key, field_label, emoji) tuples.
    """
    rows = []
    row = []
    for concept, field_label, emoji in sections_available:
        row.append(
            InlineKeyboardButton(
                text=f"{emoji} {field_label}",
                callback_data=f"sec:{cache_id}:{concept}",
            )
        )
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)

    rows.append([InlineKeyboardButton(text="📋 Show everything", callback_data=f"sec:{cache_id}:_all")])
    rows.append([InlineKeyboardButton(text="🤖 Ask AI about this drug", callback_data=f"dqa:ask:{cache_id}")])
    rows.append([InlineKeyboardButton(text="🧮 Calculate dose by renal function", callback_data=f"rc:start:{cache_id}")])
    rows.append([InlineKeyboardButton(text="🔖 Bookmark this drug", callback_data=f"bm:add:{cache_id}")])
    rows.append([InlineKeyboardButton(text="📄 Export as PDF", callback_data=f"pdfexp:{cache_id}")])

    return InlineKeyboardMarkup(inline_keyboard=rows)


def recent_list_kb(names: list[str], source: str) -> InlineKeyboardMarkup:
    """
    Tap-to-relookup buttons for /recent and /bookmarks. source is "recent" or
    "bookmark" -- the handler re-fetches that list by index at tap time
    (rather than encoding the drug name itself in callback_data), so a long
    combination-drug name can never blow past Telegram's 64-byte
    callback_data limit.
    """
    rows = [
        [InlineKeyboardButton(text=name, callback_data=f"redo:{source}:{i}")]
        for i, name in enumerate(names)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def renal_mode_kb() -> InlineKeyboardMarkup:
    """
    First step of the renal-calculator flow: either the user already has a
    lab value (eGFR or CrCl -- these are NOT the same measurement, so which
    one matters), or wants the bot to calculate one from patient parameters.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="💧 I know the eGFR (mL/min/1.73m²)", callback_data="rc:mode:egfr_direct")],
            [InlineKeyboardButton(text="💧 I know the CrCl (mL/min)", callback_data="rc:mode:crcl_direct")],
            [InlineKeyboardButton(text="🧮 Calculate eGFR (CKD-EPI)", callback_data="rc:mode:egfr_calc")],
            [InlineKeyboardButton(text="🧮 Calculate CrCl (Cockcroft-Gault)", callback_data="rc:mode:crcl_calc")],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="rc:cancel")],
        ]
    )


def renal_sex_kb() -> InlineKeyboardMarkup:
    """Sex selection -- required as a direct input by both the CKD-EPI and Cockcroft-Gault equations."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="Male", callback_data="rc:sex:M"),
                InlineKeyboardButton(text="Female", callback_data="rc:sex:F"),
            ],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="rc:cancel")],
        ]
    )


def renal_unit_kb() -> InlineKeyboardMarkup:
    """
    Serum creatinine unit selection. US labs typically report mg/dL; many
    other countries (including Lebanon) report umol/L -- these differ by a
    factor of ~88.4, so getting this wrong would badly skew the result.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="mg/dL (US-style)", callback_data="rc:unit:mgdl"),
                InlineKeyboardButton(text="µmol/L (SI/most other countries)", callback_data="rc:unit:umol"),
            ],
            [InlineKeyboardButton(text="❌ Cancel", callback_data="rc:cancel")],
        ]
    )


def renal_cancel_kb() -> InlineKeyboardMarkup:
    """Just a cancel button, shown alongside plain-text entry prompts (age, weight, creatinine, direct value)."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="rc:cancel")]])


def calc_menu_kb(calculators: list[tuple[str, str, str]]) -> InlineKeyboardMarkup:
    """
    Top-level /calculators menu. calculators: list of (calc_id, title, emoji)
    tuples, 2 buttons per row, in whatever order the caller passes.
    """
    rows = []
    row = []
    for calc_id, title, emoji in calculators:
        row.append(InlineKeyboardButton(text=f"{emoji} {title}", callback_data=f"cf:start:{calc_id}"))
        if len(row) == 2:
            rows.append(row)
            row = []
    if row:
        rows.append(row)
    return InlineKeyboardMarkup(inline_keyboard=rows)


def calc_cancel_kb() -> InlineKeyboardMarkup:
    """Cancel button shown alongside a calculator's plain-text number-entry prompts."""
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="❌ Cancel", callback_data="cf:cancel")]])


def interaction_menu_kb(drug_count: int) -> InlineKeyboardMarkup:
    """
    Buttons shown alongside the interaction checker's free-text 'type a drug
    name to add it' prompt. 'Check interactions' only appears once there are
    at least 2 drugs (nothing to cross-check with just 1).
    """
    rows = []
    if drug_count > 0:
        rows.append([InlineKeyboardButton(text="🗑 Remove last", callback_data="ix:remove_last")])
    if drug_count >= 2:
        rows.append([InlineKeyboardButton(text="✅ Check Interactions", callback_data="ix:check")])
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="ix:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def interaction_confirm_kb() -> InlineKeyboardMarkup:
    """
    Shown when a typed drug name didn't match the FDA database directly but
    interaction_ai.resolve_drug_name() found a likely candidate (a typo fix
    or a brand->generic resolution) -- the user confirms before it's added,
    rather than it being added silently on an AI guess.
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="✅ Yes, that's it", callback_data="ix:confirm:yes"),
                InlineKeyboardButton(text="❌ No", callback_data="ix:confirm:no"),
            ]
        ]
    )


def chapter_ai_kb(cache_id: str) -> InlineKeyboardMarkup:
    """Buttons attached to a just-sent chapter PDF: on-demand AI summary / self-test quiz / mnemonics for that chapter."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="📝 Summarize", callback_data=f"chai:sum:{cache_id}"),
                InlineKeyboardButton(text="❓ Quiz me", callback_data=f"chai:quiz:{cache_id}"),
            ],
            [InlineKeyboardButton(text="🧠 Mnemonics", callback_data=f"chai:mnem:{cache_id}")],
        ]
    )


def calc_choice_kb(field_index: int, options: list[tuple[str, object]]) -> InlineKeyboardMarkup:
    """
    Buttons for a calculator's choice/yes-no field. options: list of
    (label, value) tuples -- the value itself never goes in callback_data
    (only its index does), so it can be any type (bool, str, etc.).
    field_index is included so a stale tap from an earlier step (or an
    earlier calculator run) can be detected and rejected.
    """
    rows = [
        [InlineKeyboardButton(text=label, callback_data=f"cf:ans:{field_index}:{i}")]
        for i, (label, _value) in enumerate(options)
    ]
    rows.append([InlineKeyboardButton(text="❌ Cancel", callback_data="cf:cancel")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def make_searchable_kb(pending_id: str) -> InlineKeyboardMarkup:
    """Shown after chapter-splitting finishes, offering to index the whole book for Q&A ('Ask my book')."""
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🔍 Make this book searchable (Ask AI)", callback_data=f"index:{pending_id}")]
        ]
    )


def book_picker_kb(books: dict) -> InlineKeyboardMarkup:
    """One button per indexed book. books: {book_id: {"title": ..., "num_chunks": ...}}"""
    rows = [
        [InlineKeyboardButton(text=f"📖 {info['title']}", callback_data=f"askbook:{book_id}")]
        for book_id, info in books.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def drug_search_inline_kb(bot_username: str) -> InlineKeyboardMarkup:
    """
    A button that, when tapped, switches the chat's text input into inline
    mode pre-filled with '@yourbot '. Typing after that triggers Telegram's
    live inline-query suggestions (see bot.py's inline_query handler).
    """
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(
                    text="🔍 Start typing a drug name...",
                    switch_inline_query_current_chat="",
                )
            ]
        ]
    )


# ---------------------------------------------------------------------------
# Clinical Tools (ECG/lab interpretation, calculators, drug lookup/interactions)
# ---------------------------------------------------------------------------

def clinical_tools_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🫀 ECG Interpretation", callback_data="study:ecg")],
            [InlineKeyboardButton(text="🧪 Lab Interpretation", callback_data="study:lab")],
            [InlineKeyboardButton(text="🧮 Calculators", callback_data="clin:calc")],
            [InlineKeyboardButton(text="💊 Ask About Drugs", callback_data="study:drugqa")],
            [InlineKeyboardButton(text="🔀 Drug Interactions", callback_data="clin:interactions")],
            [InlineKeyboardButton(text="💊 Drug Lookup", callback_data="clin:dose")],
        ]
    )


# ---------------------------------------------------------------------------
# Study Tools (flashcards, OSCE, PDF splitter, notes, Ask My Books, bookmarks)
# ---------------------------------------------------------------------------

def study_tools_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="🗂 Flashcards", callback_data="study:flashcards")],
            [InlineKeyboardButton(text="🩺 OSCE Practice", callback_data="study:osce")],
            [InlineKeyboardButton(text="✂️ PDF Splitter", callback_data="study:pdfsplit")],
            [InlineKeyboardButton(text="📓 My Notes", callback_data="study:notes")],
            [InlineKeyboardButton(text="💬 Ask My Books", callback_data="study:askbooks")],
            [InlineKeyboardButton(text="🔖 Bookmarks", callback_data="study:bookmarks")],
        ]
    )


def lab_input_mode_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="⌨️ Type values", callback_data="lab:mode:text")],
            [InlineKeyboardButton(text="📷 Send a photo", callback_data="lab:mode:photo")],
        ]
    )


# ---------------------------------------------------------------------------
# Customer "My Plan" panel
# ---------------------------------------------------------------------------

def my_plan_kb(is_premium: bool, premium_stars: int, yearly_stars: int) -> InlineKeyboardMarkup:
    rows = []
    if not is_premium:
        rows.append([InlineKeyboardButton(text=f"⭐ Upgrade -- {premium_stars} Stars/month", callback_data="plan:buy:month")])
        rows.append([InlineKeyboardButton(text=f"⭐ Upgrade -- {yearly_stars} Stars/year", callback_data="plan:buy:year")])
        rows.append([InlineKeyboardButton(text="💳 Pay another way", callback_data="plan:contact_admin")])
    rows.append([InlineKeyboardButton(text="🎁 Invite a friend", callback_data="plan:referral")])
    rows.append([InlineKeyboardButton(text="🧾 Payment history", callback_data="plan:history")])
    rows.append([InlineKeyboardButton(text="🌐 Language", callback_data="plan:language")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Admin panel
# ---------------------------------------------------------------------------

def admin_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📊 Stats", callback_data="admin:stats")],
            [InlineKeyboardButton(text="💳 Subscriptions", callback_data="admin:subs")],
            [InlineKeyboardButton(text="💰 Cost dashboard", callback_data="admin:cost")],
            [InlineKeyboardButton(text="📢 Broadcast", callback_data="admin:broadcast")],
            [InlineKeyboardButton(text="🧪 Test functionality", callback_data="admin:test")],
            [InlineKeyboardButton(text="🪵 Recent errors", callback_data="admin:errors")],
            [InlineKeyboardButton(text="🐞 Reported problems", callback_data="admin:reports")],
        ]
    )


def admin_lookup_result_kb(target_id: int, blocked: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton(text="+7d", callback_data=f"admin:grant:{target_id}:7"),
            InlineKeyboardButton(text="+30d", callback_data=f"admin:grant:{target_id}:30"),
            InlineKeyboardButton(text="+365d", callback_data=f"admin:grant:{target_id}:365"),
        ],
        [InlineKeyboardButton(text="🚫 Revoke premium", callback_data=f"admin:revoke:{target_id}")],
    ]
    if blocked:
        rows.append([InlineKeyboardButton(text="✅ Unblock", callback_data=f"admin:block:{target_id}:off")])
    else:
        rows.append([InlineKeyboardButton(text="🚫 Block", callback_data=f"admin:block:{target_id}:on")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


# ---------------------------------------------------------------------------
# Flashcards
# ---------------------------------------------------------------------------

def flashcard_book_picker_kb(books: dict) -> InlineKeyboardMarkup:
    """books: {book_id: {"title": ...}}"""
    rows = [
        [InlineKeyboardButton(text=f"📖 {info['title']}", callback_data=f"flash:book:{book_id}")]
        for book_id, info in books.items()
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def flashcard_book_menu_kb(book_id: str, due_count: int, has_deck: bool) -> InlineKeyboardMarkup:
    rows = []
    if due_count:
        rows.append([InlineKeyboardButton(text=f"▶️ Review due cards ({due_count})", callback_data=f"flash:review:{book_id}")])
    rows.append([InlineKeyboardButton(text="➕ Generate cards from a chapter", callback_data=f"flash:genpick:{book_id}")])
    if has_deck:
        rows.append([InlineKeyboardButton(text="📤 Export to Anki (.apkg)", callback_data=f"flash:export:{book_id}")])
        rows.append([InlineKeyboardButton(text="🗑 Delete this deck", callback_data=f"flash:delete:{book_id}")])
    return InlineKeyboardMarkup(inline_keyboard=rows)


def flashcard_chapter_picker_kb(book_id: str, chapters: list[dict]) -> InlineKeyboardMarkup:
    rows = [
        [InlineKeyboardButton(text=ch["title"][:60], callback_data=f"flash:gen:{book_id}:{i}")]
        for i, ch in enumerate(chapters)
    ]
    return InlineKeyboardMarkup(inline_keyboard=rows)


def flashcard_reveal_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[InlineKeyboardButton(text="🔎 Show answer", callback_data="flash:reveal")]])


def flashcard_rate_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [
                InlineKeyboardButton(text="❌ Again", callback_data="flash:rate:0"),
                InlineKeyboardButton(text="😕 Hard", callback_data="flash:rate:3"),
                InlineKeyboardButton(text="🙂 Good", callback_data="flash:rate:4"),
                InlineKeyboardButton(text="😎 Easy", callback_data="flash:rate:5"),
            ]
        ]
    )


def admin_test_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        inline_keyboard=[
            [InlineKeyboardButton(text="📝 Chapter summary", callback_data="admin:testrun:summary")],
            [InlineKeyboardButton(text="❓ Chapter quiz", callback_data="admin:testrun:quiz")],
            [InlineKeyboardButton(text="🧠 Mnemonics", callback_data="admin:testrun:mnemonics")],
            [InlineKeyboardButton(text="🧪 Lab interpretation", callback_data="admin:testrun:lab")],
            [InlineKeyboardButton(text="🫀 ECG vision call", callback_data="admin:testrun:ecg")],
        ]
    )
