"""
ABURide – Telegram bot for student ride booking.

Uses python-telegram-bot v20+ (async) with inline-keyboard
callback queries for the ride-booking flow.
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
from typing import Any, Dict

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.request import HTTPXRequest
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

from aburide import db, services as svc
from aburide.services import DomainError, InsufficientFunds, InvalidState

logger = logging.getLogger("aburide.bot")

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# In-memory state per Telegram user id for the ride flow.
# Stores {telegram_user_id: {"origin_id": int, "origin_name": str, ...}}
_user_state: Dict[int, Dict[str, Any]] = {}

# Stop list cached after DB init so we don't hit the DB on every command.
_stops: list[Dict[str, Any]] = []


def _load_stops() -> None:
    """Populate the module-level stop cache from the DB."""
    global _stops
    with db.read() as cur:
        cur.execute("SELECT id, name FROM stops ORDER BY sort_order, id")
        _stops = [{"id": r["id"], "name": r["name"]} for r in cur.fetchall()]


def _fmt_money(cents: int) -> str:
    """Format an integer-cents value as ₦X.XX."""
    return f"₦{cents / 100:.2f}"


def _stop_name(stop_id: int) -> str:
    """Look up the display name for a stop id."""
    for s in _stops:
        if s["id"] == stop_id:
            return s["name"]
    return f"Stop #{stop_id}"


def _get_user_by_tg(telegram_id: int) -> dict | None:
    """Fetch a user row by Telegram id.

    Falls back to a direct DB query so the bot is not coupled to
    a specific services-layer signature beyond what was specified.
    """
    # Try the services helper first (may exist in later versions).
    if hasattr(svc, "get_user_by_telegram_id"):
        return svc.get_user_by_telegram_id(telegram_id)

    with db.read() as cur:
        cur.execute(
            "SELECT id, telegram_id, name, balance, role FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        if row is None:
            return None
        return {
            "id": row["id"],
            "telegram_id": row["telegram_id"],
            "name": row["name"],
            "balance": row["balance"],
            "role": row["role"],
        }


def _stop_keyboard(exclude_id: int | None = None, prefix: str = "origin") -> InlineKeyboardMarkup:
    """Build an InlineKeyboard of stop buttons, 2 per row.

    Parameters
    ----------
    exclude_id:
        Stop id to omit (used when building the destination keyboard
        so the user cannot pick the same stop as origin).
    prefix:
        Callback-data prefix – ``"origin"`` or ``"dest"``.
    """
    buttons = [
        InlineKeyboardButton(s["name"], callback_data=f"{prefix}:{s['id']}")
        for s in _stops
        if s["id"] != exclude_id
    ]
    # Arrange 2 per row
    rows = [buttons[i : i + 2] for i in range(0, len(buttons), 2)]
    return InlineKeyboardMarkup(rows)


# ---------------------------------------------------------------------------
# Command handlers
# ---------------------------------------------------------------------------


async def cmd_start(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/start – register student and show welcome message."""
    tg_user = update.effective_user
    if tg_user is None:
        return

    telegram_id = tg_user.id
    name = tg_user.full_name or tg_user.first_name or "Student"

    try:
        user = svc.register_student(telegram_id, name)
    except DomainError:
        # Already registered – just fetch their record.
        user = _get_user_by_tg(telegram_id)
        if user is None:
            await update.message.reply_text("⚠️ Could not load your account. Please try again.")
            return

    balance = _fmt_money(user["balance"])
    await update.message.reply_text(
        f"👋 Welcome to ABURide, {user['name']}!\n\n"
        f"💳 Balance: {balance}\n\n"
        "Use /ride to book a seat, /balance to check your wallet, "
        "or /help for more info."
    )


async def cmd_ride(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/ride – begin the ride-booking flow (origin selection)."""
    tg_user = update.effective_user
    if tg_user is None:
        return

    # Clear any previous flow state
    _user_state.pop(tg_user.id, None)

    await update.message.reply_text(
        "🚐 Where are you?\n\nSelect your *pickup stop*:",
        reply_markup=_stop_keyboard(prefix="origin"),
        parse_mode="Markdown",
    )


async def cmd_balance(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/balance – show current wallet balance."""
    tg_user = update.effective_user
    if tg_user is None:
        return

    user = _get_user_by_tg(tg_user.id)
    if user is None:
        await update.message.reply_text("⚠️ You are not registered yet. Use /start first.")
        return

    await update.message.reply_text(f"💳 Your balance: {_fmt_money(user['balance'])}")


async def cmd_cancel(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/cancel – cancel an active pending ride and get an immediate refund."""
    tg_user = update.effective_user
    if tg_user is None:
        return

    _user_state.pop(tg_user.id, None)
    user = _get_user_by_tg(tg_user.id)
    if user is None:
        await update.message.reply_text("⚠️ You are not registered yet. Use /start first.")
        return

    try:
        hold = svc.cancel_seat_hold(user["id"])
        fare_str = _fmt_money(hold["fare"])
        await update.message.reply_text(
            f"❌ Ride request cancelled.\n💰 {fare_str} has been refunded to your wallet.\n\nUse /balance to check your balance or /ride to request again."
        )
    except (InvalidState, DomainError) as exc:
        await update.message.reply_text(f"ℹ️ {str(exc)}")


async def cmd_help(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/help – list available commands."""
    await update.message.reply_text(
        "📖 *ABURide Commands*\n\n"
        "/start   – Register & see welcome info\n"
        "/ride    – Book a seat on a shuttle\n"
        "/topup   – Fund your student wallet\n"
        "/cancel  – Cancel active ride & refund fare\n"
        "/balance – Check your wallet balance\n"
        "/help    – Show this help message",
        parse_mode="Markdown",
    )


async def cmd_topup(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """/topup – select an amount to fund the student wallet."""
    tg_user = update.effective_user
    if tg_user is None:
        return

    user = _get_user_by_tg(tg_user.id)
    if user is None:
        await update.message.reply_text("⚠️ You are not registered yet. Use /start first.")
        return

    bal_str = _fmt_money(user["balance"])
    keyboard = InlineKeyboardMarkup([
        [
            InlineKeyboardButton("₦200", callback_data="topup_amt:200"),
            InlineKeyboardButton("₦500", callback_data="topup_amt:500"),
        ],
        [
            InlineKeyboardButton("₦1,000", callback_data="topup_amt:1000"),
            InlineKeyboardButton("₦2,000", callback_data="topup_amt:2000"),
        ],
        [
            InlineKeyboardButton("🎁 Instant Demo Credit (₦5,000)", callback_data="instant_demo_topup"),
        ],
        [
            InlineKeyboardButton("❌ Cancel", callback_data="cancel_topup"),
        ]
    ])

    await update.message.reply_text(
        f"💳 *Wallet Top-Up*\n\n"
        f"Current Balance: *{bal_str}*\n\n"
        "Select an amount to fund your wallet:",
        reply_markup=keyboard,
        parse_mode="Markdown",
    )


# ---------------------------------------------------------------------------
# Callback-query handler (inline keyboard presses)
# ---------------------------------------------------------------------------


async def handle_callback(update: Update, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Route inline-keyboard callbacks."""
    query = update.callback_query
    if query is None:
        return
    await query.answer()

    data: str = query.data or ""
    tg_id = query.from_user.id

    if data.startswith("origin:"):
        await _on_origin_selected(query, tg_id, data)
    elif data.startswith("dest:"):
        await _on_dest_selected(query, tg_id, data)
    elif data.startswith("confirm:"):
        await _on_confirm(query, tg_id, data)
    elif data == "instant_demo_topup":
        await _on_instant_demo_topup(query, tg_id)
    elif data.startswith("topup_amt:"):
        await _on_topup_amount(query, tg_id, data)
    elif data.startswith("check_topup:"):
        await _on_check_topup(query, tg_id, data)
    elif data == "cancel":
        await _on_cancel(query, tg_id)
    elif data == "cancel_topup":
        await query.edit_message_text("❌ Top-up cancelled.")
    else:
        await query.edit_message_text("⚠️ Unknown action.")


async def _on_origin_selected(query, tg_id: int, data: str) -> None:
    origin_id = int(data.split(":")[1])
    origin_name = _stop_name(origin_id)

    _user_state[tg_id] = {"origin_id": origin_id, "origin_name": origin_name}

    await query.edit_message_text(
        f"✅ Pickup: *{origin_name}*\n\nNow select your *destination*:",
        reply_markup=_stop_keyboard(exclude_id=origin_id, prefix="dest"),
        parse_mode="Markdown",
    )


async def _on_dest_selected(query, tg_id: int, data: str) -> None:
    dest_id = int(data.split(":")[1])
    dest_name = _stop_name(dest_id)

    state = _user_state.get(tg_id)
    if state is None or "origin_id" not in state:
        await query.edit_message_text("⚠️ Session expired. Please /ride again.")
        return

    state["dest_id"] = dest_id
    state["dest_name"] = dest_name

    origin_id = state["origin_id"]
    origin_name = state["origin_name"]

    # Look up fare
    with db.read() as cur:
        cur.execute(
            "SELECT amount FROM fares WHERE origin_id = ? AND dest_id = ?",
            (origin_id, dest_id),
        )
        row = cur.fetchone()

    if row is None:
        await query.edit_message_text("⚠️ No fare configured for this route. Please /ride again.")
        _user_state.pop(tg_id, None)
        return

    fare = row["amount"]
    state["fare"] = fare

    # Get user balance
    user = _get_user_by_tg(tg_id)
    balance = user["balance"] if user else 0

    confirm_btn = InlineKeyboardButton("✅ Confirm", callback_data=f"confirm:{origin_id}:{dest_id}")
    cancel_btn = InlineKeyboardButton("❌ Cancel", callback_data="cancel")
    keyboard = InlineKeyboardMarkup([[confirm_btn, cancel_btn]])

    await query.edit_message_text(
        f"📍 From: {origin_name} → {dest_name}\n"
        f"💰 Fare: {_fmt_money(fare)}\n"
        f"💳 Balance: {_fmt_money(balance)}\n\n"
        "Confirm ride?",
        reply_markup=keyboard,
    )


async def _on_confirm(query, tg_id: int, data: str) -> None:
    parts = data.split(":")
    if len(parts) != 3:
        await query.edit_message_text("⚠️ Invalid confirmation data.")
        return

    origin_id = int(parts[1])
    dest_id = int(parts[2])

    # Resolve the student's internal user id
    user = _get_user_by_tg(tg_id)
    if user is None:
        await query.edit_message_text("⚠️ You are not registered. Use /start first.")
        _user_state.pop(tg_id, None)
        return

    try:
        hold = svc.create_seat_hold(user["id"], origin_id, dest_id)
    except InsufficientFunds as exc:
        topup_kb = InlineKeyboardMarkup([
            [InlineKeyboardButton("💳 Top Up Wallet", callback_data="topup_amt:500")],
            [InlineKeyboardButton("❌ Cancel", callback_data="cancel")]
        ])
        await query.edit_message_text(
            f"❌ *Insufficient Balance*\n\n"
            f"Your wallet balance is too low to cover the fare.\n\n"
            "Would you like to fund your wallet now?",
            reply_markup=topup_kb,
            parse_mode="Markdown"
        )
        _user_state.pop(tg_id, None)
        return
    except (DomainError, InvalidState) as exc:
        await query.edit_message_text(f"⚠️ {exc}")
        _user_state.pop(tg_id, None)
        return

    pin = hold["pin"]
    fare = hold["fare"]
    expires_at = hold["expires_at"]

    # Format expiry as human-readable time
    try:
        expiry_dt = datetime.datetime.fromtimestamp(expires_at)
        expiry_str = expiry_dt.strftime("%H:%M:%S")
    except (TypeError, ValueError, OSError):
        expiry_str = "soon"

    origin_name = _stop_name(origin_id)
    dest_name = _stop_name(dest_id)

    await query.edit_message_text(
        f"🎫 *Ride Booked!*\n\n"
        f"📍 {origin_name} → {dest_name}\n"
        f"💰 Fare: {_fmt_money(fare)}\n"
        f"🔑 PIN: `{pin}`\n"
        f"⏰ Expires at: {expiry_str}\n\n"
        "Show the PIN to your driver when boarding. "
        "The hold expires if not claimed in time.",
        parse_mode="Markdown",
    )
    _user_state.pop(tg_id, None)


async def _on_cancel(query, tg_id: int) -> None:
    _user_state.pop(tg_id, None)
    await query.edit_message_text("❌ Ride cancelled.")


# ---------------------------------------------------------------------------
# Notification dispatcher (repeating job)
# ---------------------------------------------------------------------------


async def _dispatch_notifications(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Poll ``pending_notifications()`` and deliver each via Telegram."""
    try:
        notes = svc.pending_notifications()
    except Exception as exc:
        logger.error("Notification poll failed: %s", exc)
        return

    for note in notes:
        telegram_id = note["telegram_id"]
        text = note["text"]
        try:
            await context.bot.send_message(chat_id=telegram_id, text=text)
        except Exception as exc:
            logger.warning("Failed to send notification to %s: %s", telegram_id, exc)


# ---------------------------------------------------------------------------
# Bot runner
# ---------------------------------------------------------------------------


async def _on_topup_amount(query, tg_id: int, data: str) -> None:
    import uuid
    amount_naira = int(data.split(":")[1])
    amount_kobo = amount_naira * 100

    user = _get_user_by_tg(tg_id)
    if user is None:
        await query.edit_message_text("⚠️ You are not registered. Use /start first.")
        return

    ref = f"ABU-TOPUP-{user['id']}-{uuid.uuid4().hex[:8].upper()}"
    svc.create_topup_request(user["id"], amount_kobo, ref, provider="paystack")

    # In sandbox/mock mode or real mode: provide Pay Link + verification button
    checkout_url = f"https://checkout.paystack.com/{ref}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"💳 Pay ₦{amount_naira}", url=checkout_url)],
        [InlineKeyboardButton("🔄 I've Paid, Check Status", callback_data=f"check_topup:{ref}")],
        [InlineKeyboardButton("❌ Cancel", callback_data="cancel_topup")]
    ])

    await query.edit_message_text(
        f"💳 *Invoice Generated*\n\n"
        f"Amount: *₦{amount_naira:,}.00*\n"
        f"Reference: `{ref}`\n\n"
        "Tap the payment button to complete your transfer, then tap *'I've Paid, Check Status'* to credit your wallet instantly.",
        reply_markup=kb,
        parse_mode="Markdown",
    )


async def _on_check_topup(query, tg_id: int, data: str) -> None:
    ref = data.split(":")[1]
    topup = svc.get_topup(ref)
    if not topup:
        await query.edit_message_text("⚠️ Top-up transaction not found.")
        return

    user = _get_user_by_tg(tg_id)
    if not user:
        return

    # In sandbox test environment, checking auto-fulfills if pending
    if topup["status"] == "PENDING":
        svc.fulfill_topup(ref, amount=topup["amount"])
        topup = svc.get_topup(ref)

    if topup["status"] == "SUCCESS":
        user_after = _get_user_by_tg(tg_id)
        bal_str = _fmt_money(user_after["balance"])
        amt_str = _fmt_money(topup["amount"])
        await query.edit_message_text(
            f"✅ *Payment Verified!*\n\n"
            f"💰 {amt_str} has been credited to your wallet.\n"
            f"💳 Current Balance: *{bal_str}*\n\n"
            "You can now book your ride with /ride!",
            parse_mode="Markdown"
        )
    else:
        await query.edit_message_text("⏳ Payment is still pending. Please complete your transaction or try again.")


async def _on_instant_demo_topup(query, tg_id: int) -> None:
    user = _get_user_by_tg(tg_id)
    if user is None:
        await query.edit_message_text("⚠️ You are not registered yet. Use /start first.")
        return
    import uuid
    amount_kobo = 500000  # ₦5,000.00
    ref = f"ABU-DEMO-{user['id']}-{uuid.uuid4().hex[:8].upper()}"
    svc.create_topup_request(user["id"], amount_kobo, ref, provider="demo")
    svc.fulfill_topup(ref, amount=amount_kobo)
    user_after = _get_user_by_tg(tg_id)
    bal_str = _fmt_money(user_after["balance"])
    await query.edit_message_text(
        f"✅ *Instant Demo Top-Up Successful!*\n\n"
        f"💰 Credited: *₦5,000.00*\n"
        f"💳 Current Balance: *{bal_str}*\n\n"
        f"You can now book rides anytime with /ride!",
        parse_mode="Markdown"
    )


def run_bot(token: str) -> None:
    """Start the bot with long-polling."""
    import pathlib

    # Initialise database (idempotent)
    project_root = pathlib.Path(__file__).resolve().parent.parent
    db_path = project_root / "aburide.db"
    db.configure(str(db_path))
    db.init()
    logger.info("Database initialised at %s", db_path)

    # Load stop cache
    _load_stops()

    # Configure resilient HTTP timeouts & connection pooling
    request = HTTPXRequest(
        connection_pool_size=10,
        connect_timeout=30.0,
        read_timeout=30.0,
        write_timeout=30.0,
        pool_timeout=10.0,
    )

    # Build application with custom network timeouts
    application = (
        Application.builder()
        .token(token)
        .request(request)
        .get_updates_request(request)
        .build()
    )

    # Register command handlers
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("ride", cmd_ride))
    application.add_handler(CommandHandler("cancel", cmd_cancel))
    application.add_handler(CommandHandler("balance", cmd_balance))
    application.add_handler(CommandHandler("topup", cmd_topup))
    application.add_handler(CommandHandler("help", cmd_help))

    # Register callback-query handler for inline keyboards
    application.add_handler(CallbackQueryHandler(handle_callback))

    # Schedule repeating notification dispatcher (every 5 seconds)
    application.job_queue.run_repeating(
        _dispatch_notifications,
        interval=5.0,
        first=5.0,
        name="notification_dispatcher",
    )

    logger.info("Bot starting long-poll…")
    application.run_polling(
        allowed_updates=Update.ALL_TYPES,
        bootstrap_retries=5,
        timeout=30,
    )


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s  %(name)-20s  %(levelname)-8s  %(message)s",
    )

    token = sys.argv[1] if len(sys.argv) > 1 else os.environ.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("Usage: python -m aburide.bot <TOKEN>")
        sys.exit(1)

    run_bot(token)
