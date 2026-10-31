"""
ABURide – FastAPI application entry point and routes.

Provides the driver-facing REST API, background hold reaper,
and serves the static driver dashboard.
"""

import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import pathlib
import time
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse
from starlette.responses import FileResponse

from aburide import db
from aburide import services as svc
from aburide.services import DomainError

logger = logging.getLogger("aburide")

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
PROJECT_ROOT = pathlib.Path(__file__).resolve().parent.parent
DB_PATH = PROJECT_ROOT / "aburide.db"
STATIC_DIR = pathlib.Path(__file__).resolve().parent / "static"

# ---------------------------------------------------------------------------
# Background reaper
# ---------------------------------------------------------------------------

async def reaper_loop() -> None:
    """Periodically expire stale seat-holds every 10 seconds."""
    while True:
        try:
            count = svc.reap_expired_holds()
            if count:
                logger.info(f"Reaped {count} expired holds")
        except Exception as e:
            logger.error(f"Reaper error: {e}")
        await asyncio.sleep(10)

# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Startup: Database & Reaper
    db.configure(str(DB_PATH))
    db.init()
    logger.info("Database initialised at %s", DB_PATH)

    reaper_task = asyncio.create_task(reaper_loop())
    logger.info("Hold-reaper background task started")

    # Optional auto-boot Telegram bot inside the same process if token is set
    bot_app = None
    bot_token = os.environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if bot_token:
        try:
            from aburide import bot as abubot
            from telegram import Update
            logger.info("TELEGRAM_BOT_TOKEN found; initializing embedded Telegram Bot…")
            bot_app = abubot.create_bot_application(bot_token)
            await bot_app.initialize()
            await bot_app.start()
            await bot_app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                bootstrap_retries=5,
                timeout=30,
            )
            logger.info("Embedded Telegram Bot polling started successfully!")
        except Exception as e:
            logger.error("Failed to start embedded Telegram Bot: %s", e)
            bot_app = None

    yield

    # Shutdown: Stop embedded Telegram bot
    if bot_app is not None:
        try:
            logger.info("Stopping embedded Telegram Bot…")
            await bot_app.updater.stop()
            await bot_app.stop()
            await bot_app.shutdown()
            logger.info("Embedded Telegram Bot stopped")
        except Exception as e:
            logger.error("Error shutting down Telegram Bot: %s", e)

    # Stop reaper task
    reaper_task.cancel()
    try:
        await reaper_task
    except asyncio.CancelledError:
        pass
    logger.info("Hold-reaper stopped")

# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

app = FastAPI(title="ABURide", lifespan=lifespan)

# ---------------------------------------------------------------------------
# Rate Limiting, Network Restrictions & Identity Verification
# ---------------------------------------------------------------------------

_failed_login_attempts: dict[str, list[float]] = {}
_RATE_LIMIT_WINDOW_SECS = 60.0
_MAX_FAILED_LOGIN_ATTEMPTS = 5

ADMIN_SESSION_SECRET = os.getenv("ADMIN_SESSION_SECRET", "aburide-secure-admin-session-secret-2026").encode()
ADMIN_ALLOWED_IPS_RAW = os.getenv("ADMIN_ALLOWED_IPS", "127.0.0.1,::1,localhost,testclient")


def _is_ip_allowed(client_ip: str) -> bool:
    """Enforce strict network restrictions for admin infrastructure."""
    if not client_ip:
        return False
    if client_ip in ("testclient", "127.0.0.1", "::1", "localhost"):
        return True
    allowed_list = [x.strip() for x in ADMIN_ALLOWED_IPS_RAW.split(",") if x.strip()]
    if client_ip in allowed_list:
        return True
    for item in allowed_list:
        try:
            if "/" in item and ipaddress.ip_address(client_ip) in ipaddress.ip_network(item, strict=False):
                return True
        except ValueError:
            pass
    return False


def _create_admin_session_cookie(admin_id: int) -> str:
    timestamp = int(time.time())
    payload = f"{admin_id}:{timestamp}"
    signature = hmac.new(ADMIN_SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    return f"{payload}:{signature}"


def _verify_admin_session_cookie(cookie_val: Optional[str]) -> Optional[int]:
    if not cookie_val:
        return None
    parts = cookie_val.split(":")
    if len(parts) != 3:
        return None
    admin_id_str, timestamp_str, sig = parts
    payload = f"{admin_id_str}:{timestamp_str}"
    expected_sig = hmac.new(ADMIN_SESSION_SECRET, payload.encode(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(sig, expected_sig):
        return None
    try:
        ts = int(timestamp_str)
        if time.time() - ts > 86400:  # 24-hour expiration
            return None
        admin_id = int(admin_id_str)
    except ValueError:
        return None

    with db.read() as cur:
        cur.execute("SELECT id, is_active, role FROM users WHERE id = ?", (admin_id,))
        row = cur.fetchone()
        if row and row["role"] == "admin" and row["is_active"]:
            return admin_id
    return None


def _check_rate_limit(client_ip: str) -> None:
    now = time.time()
    attempts = _failed_login_attempts.get(client_ip, [])
    attempts = [t for t in attempts if now - t < _RATE_LIMIT_WINDOW_SECS]
    _failed_login_attempts[client_ip] = attempts
    if len(attempts) >= _MAX_FAILED_LOGIN_ATTEMPTS:
        raise _http_error(429, "Too many failed login attempts. Please wait 60 seconds before trying again.")


def _record_failed_attempt(client_ip: str) -> None:
    now = time.time()
    attempts = _failed_login_attempts.get(client_ip, [])
    attempts.append(now)
    _failed_login_attempts[client_ip] = attempts


def _reset_failed_attempts(client_ip: str) -> None:
    _failed_login_attempts.pop(client_ip, None)


def get_driver(request: Request) -> int:
    """Extract X-Token header, authenticate, and return driver user-id.

    Raises a 401/403 JSONResponse (via exception) on failure.
    """
    token: Optional[str] = request.headers.get("X-Token")
    if not token:
        raise _http_error(401, "Missing X-Token header")
    driver_id, status = svc.authenticate_driver(token)
    if status == "cross_role":
        raise _http_error(403, "Admin credentials cannot access driver operations.")
    if status == "suspended":
        raise _http_error(403, "Your driver account has been suspended by campus transport admin.")
    if driver_id is None:
        raise _http_error(401, "Invalid token")
    return driver_id


def get_admin(request: Request) -> int:
    """Extract and authenticate admin identity with network restriction and session checks."""
    client_ip = request.client.host if request.client else "unknown"
    if not _is_ip_allowed(client_ip):
        raise _http_error(404, "Not Found")

    # 1. Direct API header check
    token: Optional[str] = request.headers.get("X-Admin-Token")
    if token:
        admin_id, status = svc.authenticate_admin_with_status(token)
        if status == "cross_role":
            raise _http_error(403, "Driver credentials cannot access admin resources.")
        if not admin_id:
            raise _http_error(403, "Invalid or unauthorized admin token")
        return admin_id

    # 2. Signed HttpOnly cookie session check
    cookie_session = request.cookies.get("admin_session")
    admin_id = _verify_admin_session_cookie(cookie_session)
    if not admin_id:
        raise _http_error(401, "Admin authentication required")
    return admin_id


class _HTTPException(Exception):
    """Lightweight carrier so get_driver() can abort the request."""
    def __init__(self, status: int, detail: str):
        self.status = status
        self.detail = detail


def _http_error(status: int, detail: str):
    return _HTTPException(status, detail)

# ---------------------------------------------------------------------------
# Global exception handlers
# ---------------------------------------------------------------------------

@app.exception_handler(_HTTPException)
async def _handle_http_exception(request: Request, exc: _HTTPException):
    return JSONResponse(
        status_code=exc.status,
        content={"error": exc.detail},
    )


@app.exception_handler(DomainError)
async def _handle_domain_error(request: Request, exc: DomainError):
    return JSONResponse(
        status_code=400,
        content={"type": type(exc).__name__, "error": str(exc)},
    )

# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

_sim_user_state: dict[int, dict] = {}


def _fmt_money(cents: int) -> str:
    return f"₦{cents / 100:.2f}"


def _stop_name_by_id(stop_id: int) -> str:
    with db.read() as cur:
        cur.execute("SELECT name FROM stops WHERE id = ?", (stop_id,))
        row = cur.fetchone()
        return row["name"] if row else f"Stop #{stop_id}"


@app.post("/api/auth/login")
async def auth_login(request: Request):
    """Role-checked authentication with brute-force throttling and generic errors."""
    client_ip = request.client.host if request.client else "unknown"
    _check_rate_limit(client_ip)

    try:
        data = await request.json()
    except Exception:
        data = {}

    token = str(data.get("token") or "").strip()
    role = str(data.get("role") or "").strip().lower()

    if not token or role not in ("driver", "admin"):
        _record_failed_attempt(client_ip)
        raise _http_error(400, "Invalid request payload.")

    with db.read() as cur:
        cur.execute("SELECT id, name, role, is_active FROM users WHERE token = ?", (token,))
        user = cur.fetchone()

    # Never reveal whether an account exists under another role
    if not user or user["role"] != role:
        _record_failed_attempt(client_ip)
        raise _http_error(401, "Invalid credentials.")

    if role == "driver" and not user["is_active"]:
        raise _http_error(403, "Your driver account has been suspended by campus transport administration.")

    if not user["is_active"]:
        raise _http_error(403, "Account is inactive. Contact administration.")

    _reset_failed_attempts(client_ip)
    return {
        "ok": True,
        "role": user["role"],
        "name": user["name"],
        "token": token,
    }


@app.get("/")
@app.get("/driver")
async def index_or_driver():
    """Serve the Driver Cockpit dashboard. Completely hides administrative features."""
    html_path = STATIC_DIR / "driver.html"
    return FileResponse(str(html_path), media_type="text/html")


@app.get("/student")
@app.get("/telegram")
async def student_page():
    """Serve the interactive Telegram Student Bot interface & simulator."""
    html_path = STATIC_DIR / "student.html"
    return FileResponse(str(html_path), media_type="text/html")


@app.post("/api/bot/message")
async def bot_message(request: Request):
    """Interactive Telegram Bot command simulator."""
    data = await request.json()
    tg_id = data.get("telegram_id", 555888)
    text = data.get("text", "").strip()
    name = data.get("name", "Student")

    if text == "/start":
        user = svc.register_student(tg_id, name)
        balance = _fmt_money(user["balance"])
        return {
            "text": f"👋 Welcome to ABURide, {user['name']}!\n\n💳 Balance: {balance}\n\nUse /ride to book a seat, /balance to check your wallet, or /help for more info.",
            "keyboards": []
        }
    elif text == "/ride":
        _sim_user_state.pop(tg_id, None)
        with db.read() as cur:
            cur.execute("SELECT id, name FROM stops ORDER BY sort_order, id")
            stops = cur.fetchall()
        btns = [{"text": s["name"], "data": f"origin:{s['id']}"} for s in stops]
        rows = [btns[i:i+2] for i in range(0, len(btns), 2)]
        return {
            "text": "🚐 Where are you?\n\nSelect your *pickup stop*:",
            "keyboards": rows
        }
    elif text == "/cancel":
        with db.read() as cur:
            cur.execute("SELECT id FROM users WHERE telegram_id = ?", (tg_id,))
            user_row = cur.fetchone()
        if not user_row:
            return {"text": "⚠️ Student not registered. Tap /start first.", "keyboards": []}
        try:
            hold = svc.cancel_seat_hold(user_row["id"])
            fare_str = _fmt_money(hold["fare"])
            return {"text": f"❌ Ride cancelled.\n💰 {fare_str} has been refunded to your wallet.\n\nTap /ride to request again.", "keyboards": []}
        except Exception as exc:
            return {"text": f"ℹ️ {str(exc)}", "keyboards": []}
    elif text == "/balance":
        with db.read() as cur:
            cur.execute("SELECT balance FROM users WHERE telegram_id = ?", (tg_id,))
            row = cur.fetchone()
        bal = _fmt_money(row["balance"]) if row else "₦0.00"
        return {"text": f"💳 Your balance: {bal}", "keyboards": []}
    elif text == "/topup":
        with db.read() as cur:
            cur.execute("SELECT balance FROM users WHERE telegram_id = ?", (tg_id,))
            row = cur.fetchone()
        bal = _fmt_money(row["balance"]) if row else "₦0.00"
        kb = [
            [{"text": "₦200", "data": "topup_amt:200"}, {"text": "₦500", "data": "topup_amt:500"}],
            [{"text": "₦1,000", "data": "topup_amt:1000"}, {"text": "₦2,000", "data": "topup_amt:2000"}],
            [{"text": "❌ Cancel", "data": "cancel_topup"}]
        ]
        return {
            "text": f"💳 *Wallet Top-Up*\n\nCurrent Balance: *{bal}*\n\nSelect an amount to fund your wallet:",
            "keyboards": kb
        }
    elif text == "/help":
        return {
            "text": "📖 *ABURide Commands*\n\n/start   – Register & see welcome info\n/ride    – Book a seat on a shuttle\n/topup   – Fund your student wallet\n/cancel  – Cancel active ride & refund fare\n/balance – Check your wallet balance\n/help    – Show this help message",
            "keyboards": []
        }
    else:
        return {"text": "⚠️ Unrecognized command. Tap /ride to book or /help.", "keyboards": []}


@app.post("/api/bot/callback")
async def bot_callback(request: Request):
    """Interactive Telegram Bot inline keyboard callback simulator."""
    data = await request.json()
    tg_id = data.get("telegram_id", 555888)
    cb_data = data.get("data", "")

    if cb_data.startswith("origin:"):
        origin_id = int(cb_data.split(":")[1])
        origin_name = _stop_name_by_id(origin_id)
        _sim_user_state[tg_id] = {"origin_id": origin_id, "origin_name": origin_name}

        with db.read() as cur:
            cur.execute("SELECT id, name FROM stops WHERE id != ? ORDER BY sort_order, id", (origin_id,))
            stops = cur.fetchall()
        btns = [{"text": s["name"], "data": f"dest:{s['id']}"} for s in stops]
        rows = [btns[i:i+2] for i in range(0, len(btns), 2)]
        return {
            "text": f"✅ Pickup: *{origin_name}*\n\nNow select your *destination*:",
            "keyboards": rows
        }

    elif cb_data.startswith("dest:"):
        dest_id = int(cb_data.split(":")[1])
        dest_name = _stop_name_by_id(dest_id)
        state = _sim_user_state.get(tg_id, {})
        origin_id = state.get("origin_id")
        origin_name = state.get("origin_name")

        if not origin_id:
            return {"text": "⚠️ Session expired. Please tap /ride again.", "keyboards": []}

        state["dest_id"] = dest_id
        state["dest_name"] = dest_name

        with db.read() as cur:
            cur.execute("SELECT amount FROM fares WHERE origin_id = ? AND dest_id = ?", (origin_id, dest_id))
            fare_row = cur.fetchone()
            cur.execute("SELECT balance FROM users WHERE telegram_id = ?", (tg_id,))
            user_row = cur.fetchone()

        fare = fare_row["amount"] if fare_row else 15000
        balance = user_row["balance"] if user_row else 0

        kb = [[
            {"text": "✅ Confirm Ride", "data": f"confirm:{origin_id}:{dest_id}"},
            {"text": "❌ Cancel", "data": "cancel"}
        ]]
        return {
            "text": f"📍 From: *{origin_name}* → *{dest_name}*\n💰 Fare: {_fmt_money(fare)}\n💳 Balance: {_fmt_money(balance)}\n\nConfirm ride?",
            "keyboards": kb
        }

    elif cb_data.startswith("confirm:"):
        parts = cb_data.split(":")
        origin_id = int(parts[1])
        dest_id = int(parts[2])

        with db.read() as cur:
            cur.execute("SELECT id FROM users WHERE telegram_id = ?", (tg_id,))
            user_row = cur.fetchone()

        if not user_row:
            return {"text": "⚠️ Student not registered. Tap /start first.", "keyboards": []}

        try:
            hold = svc.create_seat_hold(user_row["id"], origin_id, dest_id)
        except svc.InsufficientFunds:
            return {
                "text": "❌ *Insufficient Balance*\n\nYour wallet balance is too low to cover the fare.\n\nWould you like to top up now?",
                "keyboards": [
                    [{"text": "💳 Top Up Wallet", "data": "topup_amt:500"}],
                    [{"text": "❌ Cancel", "data": "cancel"}]
                ]
            }
        except Exception as e:
            return {"text": f"❌ {str(e)}", "keyboards": []}

        import datetime
        expiry_str = datetime.datetime.fromtimestamp(hold["expires_at"]).strftime("%H:%M:%S")
        origin_name = _stop_name_by_id(origin_id)
        dest_name = _stop_name_by_id(dest_id)
        _sim_user_state.pop(tg_id, None)

        return {
            "text": f"🎫 *Ride Booked!*\n\n📍 {origin_name} → {dest_name}\n💰 Fare: {_fmt_money(hold['fare'])}\n🔑 PIN: `{hold['pin']}`\n⏰ Expires at: {expiry_str}\n\nShow this PIN to your driver when boarding. The hold expires in 3 minutes if not claimed.",
            "keyboards": []
        }

    elif cb_data.startswith("topup_amt:"):
        import uuid
        amount_naira = int(cb_data.split(":")[1])
        amount_kobo = amount_naira * 100

        with db.read() as cur:
            cur.execute("SELECT id FROM users WHERE telegram_id = ?", (tg_id,))
            user_row = cur.fetchone()

        if not user_row:
            return {"text": "⚠️ Student not registered. Tap /start first.", "keyboards": []}

        ref = f"ABU-TOPUP-{user_row['id']}-{uuid.uuid4().hex[:8].upper()}"
        svc.create_topup_request(user_row["id"], amount_kobo, ref, provider="paystack")

        return {
            "text": f"💳 *Invoice Generated*\n\nAmount: *₦{amount_naira:,}.00*\nReference: `{ref}`\n\nTap *'Check Status'* below to simulate sandbox payment verification:",
            "keyboards": [
                [{"text": f"💳 Pay ₦{amount_naira}", "data": f"pay_link:{ref}"}],
                [{"text": "🔄 Check Status", "data": f"check_topup:{ref}"}],
                [{"text": "❌ Cancel", "data": "cancel_topup"}]
            ]
        }

    elif cb_data.startswith("check_topup:") or cb_data.startswith("pay_link:"):
        ref = cb_data.split(":")[1]
        topup = svc.get_topup(ref)
        if not topup:
            return {"text": "⚠️ Topup not found.", "keyboards": []}

        if topup["status"] == "PENDING":
            svc.fulfill_topup(ref, amount=topup["amount"])
            topup = svc.get_topup(ref)

        with db.read() as cur:
            cur.execute("SELECT balance FROM users WHERE id = ?", (topup["user_id"],))
            user_row = cur.fetchone()

        amt_str = _fmt_money(topup["amount"])
        bal_str = _fmt_money(user_row["balance"])

        return {
            "text": f"✅ *Payment Verified!*\n\n💰 {amt_str} has been credited to your wallet.\n💳 Current Balance: *{bal_str}*\n\nTap /ride to book your trip!",
            "keyboards": []
        }

    elif cb_data in ("cancel", "cancel_topup"):
        _sim_user_state.pop(tg_id, None)
        return {"text": "❌ Action cancelled.", "keyboards": []}

    return {"text": "⚠️ Unknown action.", "keyboards": []}


@app.get("/api/bot/notifications")
async def bot_notifications(telegram_id: int):
    """Retrieve notifications sent to this student."""
    with db.read() as cur:
        cur.execute(
            "SELECT id, text, created_at FROM notifications WHERE telegram_id = ? ORDER BY id DESC LIMIT 5",
            (telegram_id,)
        )
        rows = cur.fetchall()
    return [{"id": r["id"], "text": r["text"], "created_at": r["created_at"]} for r in rows]


@app.get("/api/driver/state")
async def driver_state(request: Request):
    """Return full driver state with ETag / 304 support."""
    driver_id = get_driver(request)
    state = svc.driver_state(driver_id)

    body = json.dumps(state, default=str, sort_keys=True)
    etag = hashlib.md5(body.encode()).hexdigest()

    if_none_match = request.headers.get("If-None-Match")
    if if_none_match and if_none_match.strip('"') == etag:
        return Response(status_code=304, headers={"ETag": f'"{etag}"'})

    return JSONResponse(
        content=state,
        headers={"ETag": f'"{etag}"'},
    )


@app.post("/api/driver/location")
async def set_location(request: Request):
    """Set the driver's current stop location."""
    driver_id = get_driver(request)
    data = await request.json()
    stop_id: int = data["stop_id"]
    svc.set_driver_location(driver_id, stop_id)
    return {"ok": True}


@app.post("/api/driver/claim")
async def claim_seats(request: Request):
    """Claim pending seat-holds at an origin stop."""
    driver_id = get_driver(request)
    data = await request.json()
    origin_id: int = data["origin_id"]
    seats: int = data["seats"]
    holds = svc.claim_seats(driver_id, origin_id, seats)
    return holds


@app.post("/api/driver/board")
async def board_passenger(request: Request):
    """Board a passenger by verifying their PIN."""
    driver_id = get_driver(request)
    data = await request.json()
    hold_id: int = data["hold_id"]
    pin: str = data["pin"]
    result = svc.board_passenger(driver_id, hold_id, pin)
    return result


@app.post("/api/driver/complete")
async def complete_leg(request: Request):
    """Mark a trip leg as completed."""
    driver_id = get_driver(request)
    data = await request.json()
    hold_id: int = data["hold_id"]
    result = svc.complete_leg(driver_id, hold_id)
    return result


@app.post("/api/student/hold")
async def create_hold(request: Request):
    """Create a seat-hold (testing / Telegram bot convenience)."""
    data = await request.json()
    student_id: int = data["student_id"]
    origin_id: int = data["origin_id"]
    dest_id: int = data["dest_id"]
    hold = svc.create_seat_hold(student_id, origin_id, dest_id)
    return hold


@app.get("/api/stops")
async def list_stops():
    """Return all bus-stops as [{id, name}, …]."""
    with db.read() as cur:
        cur.execute("SELECT id, name FROM stops ORDER BY id")
        rows = cur.fetchall()
    return [{"id": r["id"], "name": r["name"]} for r in rows]


# ---------------------------------------------------------------------------
# Admin API Routes & Stealth HTML Serving
# ---------------------------------------------------------------------------


@app.get("/admin")
async def admin_page(request: Request):
    """Serve the Admin Fleet Dashboard strictly to verified admins from allowed IPs.
    Any non-admin, driver, or unauthenticated visitor receives 404 Not Found (stealth hidden).
    """
    client_ip = request.client.host if request.client else "unknown"
    if not _is_ip_allowed(client_ip):
        return Response(status_code=404, content="404 Not Found", media_type="text/plain")

    # Strict server-side authorization check
    admin_id = None
    header_token = request.headers.get("X-Admin-Token")
    if header_token:
        admin_id = svc.authenticate_admin(header_token)
    if not admin_id:
        cookie_session = request.cookies.get("admin_session")
        admin_id = _verify_admin_session_cookie(cookie_session)

    if not admin_id:
        # Stealth 404: completely hidden from unauthenticated visitors and drivers
        return Response(status_code=404, content="404 Not Found", media_type="text/plain")

    html_path = STATIC_DIR / "admin.html"
    return FileResponse(str(html_path), media_type="text/html")


@app.get("/admin/login")
async def admin_login_page(request: Request):
    """Dedicated restricted verification portal for administrators."""
    client_ip = request.client.host if request.client else "unknown"
    if not _is_ip_allowed(client_ip):
        return Response(status_code=404, content="404 Not Found", media_type="text/plain")

    html_path = STATIC_DIR / "admin_login.html"
    return FileResponse(str(html_path), media_type="text/html")


@app.post("/api/admin/auth/login")
async def admin_auth_login(request: Request):
    """Strong identity verification issuing signed HttpOnly session cookie."""
    client_ip = request.client.host if request.client else "unknown"
    if not _is_ip_allowed(client_ip):
        raise _http_error(404, "Not Found")
    _check_rate_limit(client_ip)

    try:
        data = await request.json()
    except Exception:
        data = {}

    token = str(data.get("token") or "").strip()
    if not token:
        _record_failed_attempt(client_ip)
        raise _http_error(400, "Token required.")

    with db.read() as cur:
        cur.execute("SELECT id, name, role, is_active FROM users WHERE token = ?", (token,))
        user = cur.fetchone()

    if not user or user["role"] != "admin" or not user["is_active"]:
        _record_failed_attempt(client_ip)
        raise _http_error(401, "Invalid credentials.")

    _reset_failed_attempts(client_ip)
    cookie_val = _create_admin_session_cookie(user["id"])
    response = JSONResponse(content={"ok": True, "name": user["name"], "redirect": "/admin"})
    response.set_cookie(
        key="admin_session",
        value=cookie_val,
        httponly=True,
        samesite="strict",
        max_age=86400,
        path="/admin",
    )
    return response


@app.post("/api/admin/auth/logout")
async def admin_auth_logout(request: Request):
    """Clear admin session cookie and invalidate session."""
    response = JSONResponse(content={"ok": True, "redirect": "/"})
    response.delete_cookie(key="admin_session", path="/admin")
    return response


@app.get("/api/admin/stats")
async def admin_stats(request: Request):
    """Retrieve campus fleet statistical KPI metrics."""
    get_admin(request)
    return svc.get_admin_dashboard_stats()


@app.get("/api/admin/drivers")
async def admin_list_drivers(
    request: Request,
    status: Optional[str] = None,
    vehicle_type: Optional[str] = None,
    search: Optional[str] = None,
):
    """List drivers with current operational status."""
    get_admin(request)
    return svc.list_drivers_admin(
        status_filter=status,
        vehicle_filter=vehicle_type,
        search=search,
    )


@app.post("/api/admin/drivers/add")
async def admin_add_driver(request: Request):
    """Add a new driver and vehicle to the campus fleet."""
    get_admin(request)
    data = await request.json()
    driver = svc.add_driver(
        name=data["name"],
        vehicle_type=data.get("vehicle_type", "bus"),
        capacity=int(data.get("capacity", 4)),
        plate=data.get("plate", ""),
        token=data.get("token"),
    )
    return driver


@app.post("/api/admin/drivers/{driver_id}/suspend")
async def admin_suspend_driver(driver_id: int, request: Request):
    """Suspend a driver."""
    get_admin(request)
    try:
        data = await request.json()
    except Exception:
        data = {}
    reason = data.get("reason") if isinstance(data, dict) else None
    return svc.suspend_driver(driver_id, reason=reason)


@app.post("/api/admin/drivers/{driver_id}/reinstate")
async def admin_reinstate_driver(driver_id: int, request: Request):
    """Reinstate a suspended driver."""
    get_admin(request)
    return svc.reinstate_driver(driver_id)


@app.delete("/api/admin/drivers/{driver_id}")
async def admin_remove_driver(driver_id: int, request: Request):
    """Soft-delete a driver."""
    get_admin(request)
    return svc.remove_driver(driver_id)
