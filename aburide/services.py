"""
ABURide – Business logic / service layer.

All money values are INTEGER CENTS (1 Naira = 100 cents).
All write operations use ``db.tx()`` (BEGIN EXCLUSIVE) for concurrency safety.
"""

from __future__ import annotations

import math
import random
import time
from typing import Any
import datetime

from . import db

# ---------------------------------------------------------------------------
# Domain exceptions
# ---------------------------------------------------------------------------


class DomainError(Exception):
    """Base class for all business-rule violations."""


class InsufficientFunds(DomainError):
    """Student balance too low to cover the fare."""


class InvalidState(DomainError):
    """Operation not allowed in the current state (e.g. duplicate hold)."""


class CapacityExceeded(DomainError):
    """Driver vehicle has no free seats for the requested count."""


class NoDemand(DomainError):
    """No pending holds at the requested stop."""


class BadPin(DomainError):
    """PIN supplied by the driver does not match the hold's PIN."""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _row_to_dict(row) -> dict[str, Any]:
    """Convert a sqlite3.Row to a plain dict."""
    if row is None:
        return {}
    return dict(row)


def _generate_pin() -> str:
    """Return a zero-padded 3-digit random PIN string."""
    return f"{random.randint(0, 999):03d}"


# ---------------------------------------------------------------------------
# User management
# ---------------------------------------------------------------------------

_WELCOME_BONUS = 50_000  # N500.00 in cents


def register_student(telegram_id: int, name: str) -> dict[str, Any]:
    """
    Register a new student or return the existing record.

    New students receive a welcome bonus of N500 (50 000 cents).
    """
    with db.tx() as cur:
        cur.execute(
            "SELECT id, telegram_id, name, balance, role FROM users WHERE telegram_id = ?",
            (telegram_id,),
        )
        row = cur.fetchone()
        if row is not None:
            return _row_to_dict(row)

        now = time.time()
        cur.execute(
            "INSERT INTO users (telegram_id, name, role, balance, created_at) "
            "VALUES (?, ?, 'student', ?, ?)",
            (telegram_id, name, _WELCOME_BONUS, now),
        )
        user_id = cur.lastrowid
        return {
            "id": user_id,
            "telegram_id": telegram_id,
            "name": name,
            "balance": _WELCOME_BONUS,
            "role": "student",
        }


def driver_by_token(token: str) -> int:
    """Return the user-id of a seeded driver given their auth token."""
    with db.read() as cur:
        cur.execute("SELECT id FROM users WHERE token = ? AND role = 'driver'", (token,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No driver with token {token!r}")
        return row["id"]


def get_user(user_id: int) -> dict[str, Any]:
    """Fetch a user record by primary key."""
    with db.read() as cur:
        cur.execute(
            "SELECT id, telegram_id, name, balance, role FROM users WHERE id = ?",
            (user_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No user with id {user_id}")
        return _row_to_dict(row)


def authenticate_driver(token: str) -> tuple[int, str] | tuple[None, str]:
    """
    Authenticate driver token.
    Returns:
      (driver_id, "ok") on success.
      (None, "cross_role") if token belongs to an admin or non-driver.
      (None, "suspended") if driver account is suspended.
      (None, "invalid") if token not found.
    """
    with db.read() as cur:
        cur.execute(
            "SELECT id, role, is_active FROM users WHERE token = ?",
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return None, "invalid"
        if row["role"] != "driver":
            return None, "cross_role"
        if not row["is_active"]:
            return None, "suspended"
        return row["id"], "ok"


def authenticate_admin_with_status(token: str) -> tuple[int | None, str]:
    """
    Authenticate admin token with detailed status.
    Returns:
      (admin_id, "ok") on success.
      (None, "cross_role") if token belongs to a driver or non-admin.
      (None, "suspended") if admin account is deactivated.
      (None, "invalid") if token not found.
    """
    with db.read() as cur:
        cur.execute(
            "SELECT id, role, is_active FROM users WHERE token = ?",
            (token,),
        )
        row = cur.fetchone()
        if not row:
            return None, "invalid"
        if row["role"] != "admin":
            return None, "cross_role"
        if not row["is_active"]:
            return None, "suspended"
        return row["id"], "ok"


def authenticate_admin(token: str) -> int | None:
    """Return admin user-id if valid, else None."""
    admin_id, status = authenticate_admin_with_status(token)
    return admin_id if status == "ok" else None


# ---------------------------------------------------------------------------
# Seat holds
# ---------------------------------------------------------------------------


def create_seat_hold(
    student_id: int,
    origin_id: int,
    dest_id: int,
    *,
    now: float | None = None,
    ttl: int = 180,
) -> dict[str, Any]:
    """
    Create a new seat hold (escrow).

    Debits the student's balance immediately; the funds sit in escrow until
    the hold is either completed (driver credited) or expired/cancelled
    (student refunded).
    """
    if now is None:
        now = time.time()

    with db.tx() as cur:
        # Guard: no open (PENDING / CLAIMED) hold for this student
        cur.execute(
            "SELECT id FROM holds WHERE student_id = ? AND status IN ('PENDING', 'CLAIMED')",
            (student_id,),
        )
        if cur.fetchone() is not None:
            raise InvalidState("Student already has an active hold")

        # Look up fare
        cur.execute(
            "SELECT amount FROM fares WHERE origin_id = ? AND dest_id = ?",
            (origin_id, dest_id),
        )
        fare_row = cur.fetchone()
        if fare_row is None:
            raise ValueError(f"No fare defined for route {origin_id} -> {dest_id}")
        fare: int = fare_row["amount"]

        # Balance check
        cur.execute("SELECT balance FROM users WHERE id = ?", (student_id,))
        user_row = cur.fetchone()
        if user_row is None:
            raise ValueError(f"No user with id {student_id}")
        if user_row["balance"] < fare:
            raise InsufficientFunds("Balance too low")

        # Debit (escrow)
        cur.execute(
            "UPDATE users SET balance = balance - ? WHERE id = ?",
            (fare, student_id),
        )

        # Generate PIN & insert hold
        pin = _generate_pin()
        expires_at = now + ttl
        cur.execute(
            "INSERT INTO holds "
            "(student_id, origin_id, dest_id, status, pin, fare, created_at, expires_at) "
            "VALUES (?, ?, ?, 'PENDING', ?, ?, ?, ?)",
            (student_id, origin_id, dest_id, pin, fare, now, expires_at),
        )
        hold_id = cur.lastrowid

        return {
            "id": hold_id,
            "student_id": student_id,
            "origin_id": origin_id,
            "dest_id": dest_id,
            "status": "PENDING",
            "pin": pin,
            "fare": fare,
            "created_at": now,
            "expires_at": expires_at,
        }


def cancel_seat_hold(student_id: int, *, now: float | None = None) -> dict[str, Any]:
    """
    Cancel an active PENDING hold for a student and immediately refund
    the escrowed fare to their balance.
    """
    if now is None:
        now = time.time()

    with db.tx() as cur:
        cur.execute(
            "SELECT * FROM holds WHERE student_id = ? AND status = 'PENDING'",
            (student_id,),
        )
        row = cur.fetchone()
        if row is None:
            raise InvalidState("No active pending ride to cancel")

        hold = _row_to_dict(row)
        cur.execute(
            "UPDATE holds SET status = 'CANCELLED' WHERE id = ?",
            (hold["id"],),
        )
        cur.execute(
            "UPDATE users SET balance = balance + ? WHERE id = ?",
            (hold["fare"], student_id),
        )

        cur.execute("SELECT telegram_id FROM users WHERE id = ?", (student_id,))
        u_row = cur.fetchone()
        if u_row and u_row["telegram_id"]:
            fare_str = f"₦{hold['fare'] / 100:.2f}"
            cur.execute(
                "INSERT INTO notifications (telegram_id, text, sent, created_at) "
                "VALUES (?, ?, 0, ?)",
                (
                    u_row["telegram_id"],
                    f"❌ Ride Request Cancelled\n\nYour fare of {fare_str} has been refunded to your wallet.",
                    now,
                ),
            )

        hold["status"] = "CANCELLED"
        return hold


# ---------------------------------------------------------------------------
# Driver operations
# ---------------------------------------------------------------------------


def claim_seats(
    driver_id: int,
    origin_id: int,
    seat_count: int,
    *,
    now: float | None = None,
) -> list[dict[str, Any]]:
    """
    Claim up to *seat_count* PENDING holds at *origin_id* for a driver.

    Runs inside an EXCLUSIVE transaction so two drivers cannot claim the
    same hold.
    """
    if now is None:
        now = time.time()

    with db.tx() as cur:
        # Vehicle capacity check
        cur.execute("SELECT capacity FROM vehicles WHERE driver_id = ?", (driver_id,))
        veh = cur.fetchone()
        if veh is None:
            raise ValueError(f"No vehicle registered for driver {driver_id}")
        capacity: int = veh["capacity"]

        cur.execute(
            "SELECT COUNT(*) AS cnt FROM holds "
            "WHERE driver_id = ? AND status IN ('CLAIMED', 'BOARDED')",
            (driver_id,),
        )
        occupied: int = cur.fetchone()["cnt"]
        free_seats = capacity - occupied

        if seat_count > free_seats:
            raise CapacityExceeded(
                f"Requested {seat_count} but only {free_seats} free seats"
            )

        # Grab available PENDING holds at this stop (FIFO)
        cur.execute(
            "SELECT * FROM holds WHERE origin_id = ? AND status = 'PENDING' "
            "ORDER BY created_at ASC LIMIT ?",
            (origin_id, seat_count),
        )
        pending = cur.fetchall()
        if not pending:
            raise NoDemand("No pending holds at this stop")

        claimed: list[dict[str, Any]] = []
        for row in pending:
            hold = _row_to_dict(row)
            hold_id = hold["id"]
            cur.execute(
                "UPDATE holds SET status = 'CLAIMED', driver_id = ?, claimed_at = ? "
                "WHERE id = ?",
                (driver_id, now, hold_id),
            )
            hold["status"] = "CLAIMED"
            hold["driver_id"] = driver_id
            hold["claimed_at"] = now

            # Fetch stop name for notification
            cur.execute("SELECT name FROM stops WHERE id = ?", (hold["origin_id"],))
            stop_name = cur.fetchone()["name"]

            # Fetch student telegram_id for notification
            cur.execute("SELECT telegram_id FROM users WHERE id = ?", (hold["student_id"],))
            student_row = cur.fetchone()
            tg_id = student_row["telegram_id"]

            pin = hold["pin"]
            notif_text = (
                f"\U0001f690 Driver claimed your hold! "
                f"Vehicle arriving at {stop_name}. "
                f"Present PIN: {pin[0]} {pin[1]} {pin[2]}"
            )
            cur.execute(
                "INSERT INTO notifications (telegram_id, text, sent, created_at) "
                "VALUES (?, ?, 0, ?)",
                (tg_id, notif_text, now),
            )

            claimed.append(hold)

        # Discrete position inference – driver is picking up at this origin
        _upsert_driver_location(cur, driver_id, origin_id, now)

        return claimed


def board_passenger(
    driver_id: int,
    hold_id: int,
    pin: str,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """
    Board a passenger after verifying the hold's PIN.

    Credits the fare to the driver's balance (out of escrow).
    """
    if now is None:
        now = time.time()

    with db.tx() as cur:
        cur.execute("SELECT * FROM holds WHERE id = ?", (hold_id,))
        row = cur.fetchone()
        if row is None:
            raise InvalidState(f"Hold {hold_id} not found")

        hold = _row_to_dict(row)
        if hold["driver_id"] != driver_id:
            raise InvalidState("Hold does not belong to this driver")
        if hold["status"] != "CLAIMED":
            raise InvalidState(f"Hold status is {hold['status']}, expected CLAIMED")
        if hold["pin"] != pin:
            raise BadPin("Incorrect PIN")

        # Transition to BOARDED
        cur.execute(
            "UPDATE holds SET status = 'BOARDED', boarded_at = ? WHERE id = ?",
            (now, hold_id),
        )

        # Discrete position inference – driver is boarding at origin
        _upsert_driver_location(cur, driver_id, hold["origin_id"], now)

        # Credit driver (escrow -> driver balance)
        cur.execute(
            "UPDATE users SET balance = balance + ? WHERE id = ?",
            (hold["fare"], driver_id),
        )

        # Send boarding confirmation notification to student
        cur.execute("SELECT name FROM stops WHERE id = ?", (hold["dest_id"],))
        dest_name = cur.fetchone()["name"]
        cur.execute("SELECT name FROM users WHERE id = ?", (driver_id,))
        driver_name = cur.fetchone()["name"]
        cur.execute("SELECT telegram_id FROM users WHERE id = ?", (hold["student_id"],))
        student_row = cur.fetchone()
        if student_row and student_row["telegram_id"]:
            board_text = (
                f"✅ Boarding Confirmed!\n\n"
                f"Welcome aboard! {driver_name} has verified your PIN.\n"
                f"🚐 En route to: {dest_name}. Sit back and enjoy the ride!"
            )
            cur.execute(
                "INSERT INTO notifications (telegram_id, text, sent, created_at) "
                "VALUES (?, ?, 0, ?)",
                (student_row["telegram_id"], board_text, now),
            )

        hold["status"] = "BOARDED"
        hold["boarded_at"] = now
        return hold


def complete_leg(
    driver_id: int,
    hold_id: int,
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """
    Mark a BOARDED hold as COMPLETED and infer driver position from
    the hold's destination stop.
    """
    if now is None:
        now = time.time()

    with db.tx() as cur:
        cur.execute("SELECT * FROM holds WHERE id = ?", (hold_id,))
        row = cur.fetchone()
        if row is None:
            raise InvalidState(f"Hold {hold_id} not found")

        hold = _row_to_dict(row)
        if hold["driver_id"] != driver_id:
            raise InvalidState("Hold does not belong to this driver")
        if hold["status"] != "BOARDED":
            raise InvalidState(f"Hold status is {hold['status']}, expected BOARDED")

        cur.execute(
            "UPDATE holds SET status = 'COMPLETED', completed_at = ? WHERE id = ?",
            (now, hold_id),
        )

        # Discrete position inference – move driver to the destination
        _upsert_driver_location(cur, driver_id, hold["dest_id"], now)

        # Send arrival and thank you notification to student
        cur.execute("SELECT name FROM stops WHERE id = ?", (hold["dest_id"],))
        dest_name = cur.fetchone()["name"]
        cur.execute("SELECT telegram_id FROM users WHERE id = ?", (hold["student_id"],))
        student_row = cur.fetchone()
        if student_row and student_row["telegram_id"]:
            arrival_text = (
                f"🎉 Arrived at {dest_name}!\n\n"
                f"You have arrived safely at your destination.\n"
                f"🙏 Thank you for riding with ABURide! Have a wonderful day on campus! 🎓✨"
            )
            cur.execute(
                "INSERT INTO notifications (telegram_id, text, sent, created_at) "
                "VALUES (?, ?, 0, ?)",
                (student_row["telegram_id"], arrival_text, now),
            )

        hold["status"] = "COMPLETED"
        hold["completed_at"] = now
        return hold


# ---------------------------------------------------------------------------
# Expiry / reaper
# ---------------------------------------------------------------------------


def reap_expired_holds(*, now: float | None = None) -> int:
    """
    Expire all PENDING holds past their ``expires_at`` and refund the
    escrowed fare to each student. Returns the count of expired holds.
    Sends an auto-refund notification to the student on Telegram.
    """
    if now is None:
        now = time.time()

    with db.tx() as cur:
        cur.execute(
            "SELECT h.id, h.student_id, h.fare, u.telegram_id, u.balance, s.name AS origin_name "
            "FROM holds h "
            "JOIN users u ON u.id = h.student_id "
            "JOIN stops s ON s.id = h.origin_id "
            "WHERE h.status = 'PENDING' AND h.expires_at <= ?",
            (now,),
        )
        expired = cur.fetchall()
        for row in expired:
            hold_id = row["id"]
            student_id = row["student_id"]
            fare = row["fare"]
            tg_id = row["telegram_id"]
            new_bal = row["balance"] + fare
            origin_name = row["origin_name"]

            cur.execute(
                "UPDATE holds SET status = 'EXPIRED' WHERE id = ?",
                (hold_id,),
            )
            cur.execute(
                "UPDATE users SET balance = balance + ? WHERE id = ?",
                (fare, student_id),
            )

            # Insert refund notification for student
            if tg_id:
                fare_str = f"₦{fare / 100:.2f}"
                bal_str = f"₦{new_bal / 100:.2f}"
                refund_text = (
                    f"⏱️ Ride Request Expired & Refunded\n\n"
                    f"No driver claimed your ride from {origin_name} within 3 minutes.\n"
                    f"💰 Your fare of {fare_str} has been automatically refunded to your wallet.\n"
                    f"💳 Current Balance: {bal_str}\n\n"
                    f"Use /ride to try again whenever you are ready!"
                )
                cur.execute(
                    "INSERT INTO notifications (telegram_id, text, sent, created_at) "
                    "VALUES (?, ?, 0, ?)",
                    (tg_id, refund_text, now),
                )

        return len(expired)


# ---------------------------------------------------------------------------
# Driver dashboard / state
# ---------------------------------------------------------------------------


def driver_state(driver_id: int, *, now: float | None = None) -> dict[str, Any]:
    """
    Assemble the complete driver dashboard state.
    """
    if now is None:
        now = time.time()

    with db.read() as cur:
        # Driver info
        cur.execute(
            "SELECT u.id, u.name, u.balance, v.capacity, v.vehicle_type, v.plate, dl.stop_id "
            "FROM users u "
            "JOIN vehicles v ON v.driver_id = u.id "
            "LEFT JOIN driver_locations dl ON dl.driver_id = u.id "
            "WHERE u.id = ?",
            (driver_id,),
        )
        drv = cur.fetchone()
        if drv is None:
            raise ValueError(f"No driver with id {driver_id}")
        drv = _row_to_dict(drv)

        capacity: int = drv["capacity"]
        vehicle_type: str = drv.get("vehicle_type") or "bus"
        plate: str = drv.get("plate") or ""
        driver_stop_id = drv["stop_id"]  # may be None

        # Occupied seats
        cur.execute(
            "SELECT COUNT(*) AS cnt FROM holds "
            "WHERE driver_id = ? AND status IN ('CLAIMED', 'BOARDED')",
            (driver_id,),
        )
        occupied: int = cur.fetchone()["cnt"]
        free_seats = capacity - occupied

        # All stops
        cur.execute("SELECT id, name, lat, lon, sort_order FROM stops ORDER BY sort_order")
        all_stops = [_row_to_dict(s) for s in cur.fetchall()]

        # Build lookups
        sort_lookup: dict[int, int] = {s["id"]: s["sort_order"] for s in all_stops}
        coords_lookup: dict[int, tuple[float, float]] = {
            s["id"]: (s["lat"] or 0.0, s["lon"] or 0.0) for s in all_stops
        }

        # Demand: pending holds grouped by origin stop
        cur.execute(
            "SELECT h.origin_id, s.name AS stop_name, "
            "       COUNT(*) AS cnt, "
            "       MIN(h.created_at) AS oldest "
            "FROM holds h "
            "JOIN stops s ON s.id = h.origin_id "
            "WHERE h.status = 'PENDING' AND h.expires_at > ? "
            "GROUP BY h.origin_id",
            (now,),
        )
        demand_rows = cur.fetchall()

        demand: list[dict[str, Any]] = []
        driver_sort = sort_lookup.get(driver_stop_id, 0) if driver_stop_id else 0
        for r in demand_rows:
            r = _row_to_dict(r)
            stop_sort = sort_lookup.get(r["origin_id"], 0)
            distance_score = abs(driver_sort - stop_sort) if driver_stop_id else 0
            wait_min = (now - r["oldest"]) / 60.0
            score = (r["cnt"] * 10) + (wait_min * 2) - (distance_score * 1)
            c_lat, c_lon = coords_lookup.get(r["origin_id"], (0.0, 0.0))
            demand.append({
                "stop_id": r["origin_id"],
                "stop": r["stop_name"],
                "count": r["cnt"],
                "wait_min": round(wait_min, 1),
                "distance_km": distance_score,  # simplified proxy
                "score": round(score, 1),
                "lat": c_lat,
                "lon": c_lon,
            })
        demand.sort(key=lambda d: d["score"], reverse=True)

        # Individual student dots for heat map rendering
        cur.execute(
            "SELECT h.id, h.origin_id, h.created_at, s.name AS stop_name, s.lat, s.lon "
            "FROM holds h "
            "JOIN stops s ON s.id = h.origin_id "
            "WHERE h.status = 'PENDING' AND h.expires_at > ? "
            "ORDER BY h.created_at ASC",
            (now,),
        )
        dot_rows = cur.fetchall()
        student_dots: list[dict[str, Any]] = []
        for row in dot_rows:
            r = _row_to_dict(row)
            # Deterministic angular dispersion (~10-25m) so clustered students at a stop don't overlap completely
            angle = (r["id"] * 137.5) % 360
            rad = math.radians(angle)
            jitter_dist = 0.00018 * ((r["id"] % 3) + 1) * 0.5
            j_lat = r["lat"] + math.sin(rad) * jitter_dist
            j_lon = r["lon"] + math.cos(rad) * jitter_dist
            student_dots.append({
                "hold_id": r["id"],
                "stop_id": r["origin_id"],
                "stop_name": r["stop_name"],
                "lat": round(j_lat, 6),
                "lon": round(j_lon, 6),
                "wait_min": round((now - r["created_at"]) / 60.0, 1),
            })

        # Manifest: active passengers with destination metadata
        cur.execute(
            "SELECT h.id AS hold_id, u.name AS student, "
            "       h.origin_id, h.dest_id, "
            "       so.name AS 'from', sd.name AS 'to', "
            "       sd.lat AS dest_lat, sd.lon AS dest_lon, "
            "       h.fare, h.status, h.pin "
            "FROM holds h "
            "JOIN users u ON u.id = h.student_id "
            "JOIN stops so ON so.id = h.origin_id "
            "JOIN stops sd ON sd.id = h.dest_id "
            "WHERE h.driver_id = ? AND h.status IN ('CLAIMED', 'BOARDED') "
            "ORDER BY h.claimed_at",
            (driver_id,),
        )
        manifest = [_row_to_dict(m) for m in cur.fetchall()]

        total_waiting = sum(d["count"] for d in demand)

        return {
            "driver": {
                "id": drv["id"],
                "name": drv["name"],
                "stop_id": driver_stop_id,
                "capacity": capacity,
                "free_seats": free_seats,
                "balance": drv["balance"],
                "earnings": drv["balance"],
                "vehicle_type": vehicle_type,
                "plate": plate,
                "total_waiting": total_waiting,
            },
            "stops": [
                {"id": s["id"], "name": s["name"], "lat": s["lat"], "lon": s["lon"]}
                for s in all_stops
            ],
            "demand": demand,
            "manifest": manifest,
            "student_dots": student_dots,
        }


# ---------------------------------------------------------------------------
# Driver location
# ---------------------------------------------------------------------------


def set_driver_location(driver_id: int, stop_id: int) -> None:
    """Upsert the driver's discrete location to a stop."""
    now = time.time()
    with db.tx() as cur:
        _upsert_driver_location(cur, driver_id, stop_id, now)


def _upsert_driver_location(
    cur, driver_id: int, stop_id: int, now: float
) -> None:
    """Internal helper – upsert within an existing transaction."""
    cur.execute(
        "INSERT INTO driver_locations (driver_id, stop_id, updated_at) "
        "VALUES (?, ?, ?) "
        "ON CONFLICT(driver_id) DO UPDATE SET stop_id = excluded.stop_id, "
        "updated_at = excluded.updated_at",
        (driver_id, stop_id, now),
    )


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------


def pending_notifications() -> list[dict[str, Any]]:
    """
    Fetch all unsent notifications, mark them sent, and return them.
    """
    with db.tx() as cur:
        cur.execute(
            "SELECT id, telegram_id, text FROM notifications WHERE sent = 0"
        )
        rows = cur.fetchall()
        result: list[dict[str, Any]] = []
        for r in rows:
            r = _row_to_dict(r)
            cur.execute("UPDATE notifications SET sent = 1 WHERE id = ?", (r["id"],))
            result.append({"telegram_id": r["telegram_id"], "text": r["text"]})
        return result


# ---------------------------------------------------------------------------
# Wallet Top-Ups & Ledger
# ---------------------------------------------------------------------------


def create_topup_request(
    user_id: int,
    amount: int,
    reference: str,
    provider: str = "paystack",
    *,
    now: float | None = None,
) -> dict[str, Any]:
    """
    Register a PENDING top-up request.

    Parameters
    ----------
    user_id: int
        User requesting wallet credit.
    amount: int
        Amount in integer cents/kobo (e.g. 50000 = ₦500.00). Must be > 0.
    reference: str
        Globally unique merchant reference.
    provider: str
        Payment provider name (e.g., 'paystack', 'manual').
    """
    if amount <= 0:
        raise ValueError("Top-up amount must be strictly greater than 0")
    if now is None:
        now = time.time()

    with db.tx() as cur:
        cur.execute("SELECT id FROM users WHERE id = ?", (user_id,))
        if cur.fetchone() is None:
            raise ValueError(f"No user with id {user_id}")

        cur.execute(
            "INSERT INTO topups (user_id, amount, reference, provider, status, created_at) "
            "VALUES (?, ?, ?, ?, 'PENDING', ?)",
            (user_id, amount, reference, provider, now),
        )
        topup_id = cur.lastrowid

        return {
            "id": topup_id,
            "user_id": user_id,
            "amount": amount,
            "reference": reference,
            "provider": provider,
            "status": "PENDING",
            "created_at": now,
        }


def fulfill_topup(
    reference: str,
    *,
    amount: int | None = None,
    now: float | None = None,
) -> dict[str, Any]:
    """
    Fulfill a pending top-up atomically and credit the user's wallet.

    Idempotent: If the top-up is already marked SUCCESS, returns the existing record
    without duplicating the credit.
    """
    if now is None:
        now = time.time()

    with db.tx() as cur:
        cur.execute("SELECT * FROM topups WHERE reference = ?", (reference,))
        row = cur.fetchone()
        if row is None:
            raise ValueError(f"No topup found with reference {reference!r}")

        topup = _row_to_dict(row)

        # Idempotency guard: already credited
        if topup["status"] == "SUCCESS":
            return topup

        if topup["status"] != "PENDING":
            raise InvalidState(f"Topup is in state {topup['status']}, cannot fulfill")

        # Amount verification guard (if passed from webhook)
        if amount is not None and topup["amount"] != amount:
            raise ValueError(
                f"Amount mismatch: expected {topup['amount']}, received {amount}"
            )

        # Credit user wallet atomically
        cur.execute(
            "UPDATE users SET balance = balance + ? WHERE id = ?",
            (topup["amount"], topup["user_id"]),
        )

        # Mark top-up as SUCCESS
        cur.execute(
            "UPDATE topups SET status = 'SUCCESS', completed_at = ? WHERE id = ?",
            (now, topup["id"]),
        )

        # Enqueue wallet top-up receipt notification
        cur.execute("SELECT telegram_id, balance FROM users WHERE id = ?", (topup["user_id"],))
        user_row = cur.fetchone()
        if user_row and user_row["telegram_id"]:
            amt_str = f"₦{topup['amount'] / 100:.2f}"
            bal_str = f"₦{user_row['balance'] / 100:.2f}"
            receipt_text = (
                f"💳 Wallet Credited Successfully!\n\n"
                f"Amount: {amt_str}\n"
                f"Reference: `{reference}`\n"
                f"New Balance: {bal_str}\n\n"
                f"Use /ride to book your trip across campus!"
            )
            cur.execute(
                "INSERT INTO notifications (telegram_id, text, sent, created_at) "
                "VALUES (?, ?, 0, ?)",
                (user_row["telegram_id"], receipt_text, now),
            )

        topup["status"] = "SUCCESS"
        topup["completed_at"] = now
        return topup


def get_topup(reference: str) -> dict[str, Any] | None:
    """Fetch top-up record by unique reference."""
    with db.read() as cur:
        cur.execute("SELECT * FROM topups WHERE reference = ?", (reference,))
        row = cur.fetchone()
        return _row_to_dict(row) if row else None


# ---------------------------------------------------------------------------
# Admin Management Operations
# ---------------------------------------------------------------------------


def get_admin_dashboard_stats() -> dict[str, Any]:
    """Aggregate real-time metrics for campus fleet management."""
    now = time.time()
    with db.read() as cur:
        # Drivers counts
        cur.execute("SELECT COUNT(*) AS total FROM users WHERE role = 'driver' AND is_active >= 0")
        total_drivers = cur.fetchone()["total"]

        cur.execute("SELECT COUNT(*) AS suspended FROM users WHERE role = 'driver' AND is_active = 0")
        suspended_drivers = cur.fetchone()["suspended"]

        # Online drivers: updated position in last 5 minutes (300s) and active
        cur.execute(
            "SELECT COUNT(DISTINCT dl.driver_id) AS online "
            "FROM driver_locations dl "
            "JOIN users u ON u.id = dl.driver_id "
            "WHERE u.is_active = 1 AND (? - dl.updated_at) < 300",
            (now,),
        )
        online_drivers = cur.fetchone()["online"]

        # Total students waiting
        cur.execute(
            "SELECT COUNT(*) AS waiting FROM holds WHERE status = 'PENDING' AND expires_at > ?",
            (now,),
        )
        students_waiting = cur.fetchone()["waiting"]

        return {
            "total_drivers": total_drivers,
            "online_drivers": online_drivers,
            "suspended_drivers": suspended_drivers,
            "students_waiting": students_waiting,
        }


def list_drivers_admin(
    *,
    status_filter: str | None = None,
    vehicle_filter: str | None = None,
    search: str | None = None,
) -> list[dict[str, Any]]:
    """List drivers with their fleet operational state."""
    now = time.time()
    with db.read() as cur:
        cur.execute(
            "SELECT u.id, u.name, u.token, u.balance, u.is_active, u.created_at, "
            "       v.capacity, v.vehicle_type, v.plate, "
            "       dl.stop_id, dl.updated_at, "
            "       s.name AS current_stop_name "
            "FROM users u "
            "LEFT JOIN vehicles v ON v.driver_id = u.id "
            "LEFT JOIN driver_locations dl ON dl.driver_id = u.id "
            "LEFT JOIN stops s ON s.id = dl.stop_id "
            "WHERE u.role = 'driver' AND u.is_active >= 0 "
            "ORDER BY u.created_at DESC"
        )
        rows = cur.fetchall()

    drivers = []
    for r in rows:
        d = _row_to_dict(r)
        # Determine status: Suspended vs Online (active within 5 min) vs Offline
        if d["is_active"] == 0:
            status = "Suspended"
        elif d.get("updated_at") and (now - d["updated_at"]) < 300:
            status = "Online"
        else:
            status = "Offline"

        d["status"] = status
        d["vehicle_type"] = d.get("vehicle_type") or "bus"
        d["plate"] = d.get("plate") or "N/A"
        d["current_location"] = d.get("current_stop_name") or "Idle / Station"
        d["last_active"] = (
            datetime.datetime.fromtimestamp(d["updated_at"]).strftime("%H:%M:%S")
            if d.get("updated_at")
            else "Never"
        )

        # Apply in-memory filters
        if status_filter and status_filter.lower() != "all":
            if d["status"].lower() != status_filter.lower():
                continue
        if vehicle_filter and vehicle_filter.lower() != "all":
            if d["vehicle_type"].lower() != vehicle_filter.lower():
                continue
        if search:
            q = search.lower()
            if q not in d["name"].lower() and q not in d["plate"].lower():
                continue

        drivers.append(d)

    return drivers


def add_driver(
    name: str,
    vehicle_type: str,
    capacity: int,
    plate: str,
    token: str | None = None,
) -> dict[str, Any]:
    """Add a new campus driver with allocated vehicle."""
    import secrets

    if not name.strip():
        raise ValueError("Driver name is required")
    if vehicle_type not in ("bus", "keke"):
        raise ValueError("Vehicle type must be 'bus' or 'keke'")
    if capacity <= 0:
        raise ValueError("Capacity must be positive")

    token = token.strip() if token else f"driver-{secrets.token_hex(4)}"
    plate = plate.strip().upper() if plate else "ABU-CAMPUS"

    now = time.time()
    with db.tx() as cur:
        # Check token unique
        cur.execute("SELECT id FROM users WHERE token = ?", (token,))
        if cur.fetchone():
            raise ValueError(f"Driver token {token!r} already in use")

        cur.execute(
            "INSERT INTO users (telegram_id, name, role, balance, token, is_active, created_at) "
            "VALUES (NULL, ?, 'driver', 0, ?, 1, ?)",
            (name.strip(), token, now),
        )
        driver_id = cur.lastrowid

        cur.execute(
            "INSERT INTO vehicles (driver_id, capacity, vehicle_type, plate) "
            "VALUES (?, ?, ?, ?)",
            (driver_id, capacity, vehicle_type, plate),
        )

        return {
            "id": driver_id,
            "name": name.strip(),
            "token": token,
            "vehicle_type": vehicle_type,
            "capacity": capacity,
            "plate": plate,
            "is_active": 1,
            "created_at": now,
        }


def suspend_driver(driver_id: int, reason: str | None = None) -> dict[str, Any]:
    """Suspend a driver from operating on campus."""
    with db.tx() as cur:
        cur.execute("SELECT id, name, is_active FROM users WHERE id = ? AND role = 'driver'", (driver_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"No driver with ID {driver_id}")

        cur.execute("UPDATE users SET is_active = 0 WHERE id = ?", (driver_id,))
        return {"id": driver_id, "name": row["name"], "status": "Suspended", "reason": reason}


def reinstate_driver(driver_id: int) -> dict[str, Any]:
    """Reinstate a suspended driver."""
    with db.tx() as cur:
        cur.execute("SELECT id, name, is_active FROM users WHERE id = ? AND role = 'driver'", (driver_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"No driver with ID {driver_id}")

        cur.execute("UPDATE users SET is_active = 1 WHERE id = ?", (driver_id,))
        return {"id": driver_id, "name": row["name"], "status": "Active"}


def remove_driver(driver_id: int) -> dict[str, Any]:
    """Soft-delete a driver from the system."""
    with db.tx() as cur:
        cur.execute("SELECT id, name FROM users WHERE id = ? AND role = 'driver'", (driver_id,))
        row = cur.fetchone()
        if not row:
            raise ValueError(f"No driver with ID {driver_id}")

        # Set is_active to -1 (soft deleted)
        cur.execute("UPDATE users SET is_active = -1, token = NULL WHERE id = ?", (driver_id,))
        return {"id": driver_id, "name": row["name"], "deleted": True}
