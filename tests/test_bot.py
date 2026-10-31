import pytest
from unittest.mock import AsyncMock, MagicMock
from aburide import db, bot, services as svc
from telegram import Update, User, Message, CallbackQuery, Chat

@pytest.fixture(autouse=True)
def init_test_db(tmp_path):
    db.configure(str(tmp_path / "bot_test.db"))
    db.init()
    bot._load_stops()
    bot._user_state.clear()

@pytest.mark.anyio
async def test_bot_start_command():
    user = User(id=123456, first_name="TestStudent", is_bot=False)
    message = AsyncMock(spec=Message)
    message.reply_text = AsyncMock()
    update = MagicMock(spec=Update)
    update.effective_user = user
    update.message = message
    ctx = MagicMock()

    await bot.cmd_start(update, ctx)
    message.reply_text.assert_called_once()
    reply = message.reply_text.call_args[0][0]
    assert "Welcome to ABURide" in reply
    assert "₦500.00" in reply

    # Verify user exists in database
    student = bot._get_user_by_tg(123456)
    assert student is not None
    assert student["balance"] == 50000

@pytest.mark.anyio
async def test_bot_ride_full_booking_flow():
    user = User(id=999, first_name="Amina", is_bot=False)
    svc.register_student(999, "Amina")

    # 1. /ride command
    message = AsyncMock(spec=Message)
    message.reply_text = AsyncMock()
    update = MagicMock(spec=Update)
    update.effective_user = user
    update.message = message
    ctx = MagicMock()

    await bot.cmd_ride(update, ctx)
    message.reply_text.assert_called_once()
    assert "Select your *pickup stop*" in message.reply_text.call_args[0][0]

    # 2. Select origin (North Gate)
    ng_id = db.stop_id("North Gate")
    cb_query = AsyncMock(spec=CallbackQuery)
    cb_query.from_user = user
    cb_query.data = f"origin:{ng_id}"
    cb_query.answer = AsyncMock()
    cb_query.edit_message_text = AsyncMock()
    cb_update = MagicMock(spec=Update)
    cb_update.callback_query = cb_query

    await bot.handle_callback(cb_update, ctx)
    cb_query.edit_message_text.assert_called_once()
    assert "Pickup: *North Gate*" in cb_query.edit_message_text.call_args[0][0]

    # 3. Select dest (Main Gate)
    mg_id = db.stop_id("Main Gate")
    cb_query.data = f"dest:{mg_id}"
    cb_query.edit_message_text = AsyncMock()

    await bot.handle_callback(cb_update, ctx)
    cb_query.edit_message_text.assert_called_once()
    dest_text = cb_query.edit_message_text.call_args[0][0]
    assert "From: North Gate → Main Gate" in dest_text
    assert "₦150.00" in dest_text

    # 4. Confirm booking
    cb_query.data = f"confirm:{ng_id}:{mg_id}"
    cb_query.edit_message_text = AsyncMock()

    await bot.handle_callback(cb_update, ctx)
    cb_query.edit_message_text.assert_called_once()
    confirm_text = cb_query.edit_message_text.call_args[0][0]
    assert "Ride Booked!" in confirm_text
    assert "PIN:" in confirm_text

    # Verify hold in database
    student = bot._get_user_by_tg(999)
    assert student["balance"] == 50000 - 15000  # Debited to escrow
    with db.read() as cur:
        cur.execute("SELECT * FROM holds WHERE student_id = ?", (student["id"],))
        h = cur.fetchone()
        assert h is not None
        assert h["status"] == "PENDING"
        assert h["origin_id"] == ng_id
        assert h["dest_id"] == mg_id

@pytest.mark.anyio
async def test_bot_notification_dispatcher():
    # Insert a pending notification
    now = 123456789.0
    with db.tx() as cur:
        cur.execute(
            "INSERT INTO notifications (telegram_id, text, sent, created_at) VALUES (?, ?, 0, ?)",
            (999, "Your shuttle is here!", now),
        )

    bot_mock = AsyncMock()
    bot_mock.send_message = AsyncMock()
    ctx = MagicMock()
    ctx.bot = bot_mock

    await bot._dispatch_notifications(ctx)
    bot_mock.send_message.assert_called_once_with(chat_id=999, text="Your shuttle is here!")

    # Verify notification marked as sent
    with db.read() as cur:
        cur.execute("SELECT sent FROM notifications WHERE telegram_id = ?", (999,))
        assert cur.fetchone()["sent"] == 1

@pytest.mark.anyio
async def test_bot_cancel_command():
    user = User(id=888111, first_name="Bilal", is_bot=False)
    student = svc.register_student(888111, "Bilal")
    ng_id = db.stop_id("North Gate")
    mg_id = db.stop_id("Main Gate")

    # Create a hold
    svc.create_seat_hold(student["id"], ng_id, mg_id)
    assert svc.get_user(student["id"])["balance"] == 35000

    message = AsyncMock(spec=Message)
    message.reply_text = AsyncMock()
    update = MagicMock(spec=Update)
    update.effective_user = user
    update.message = message
    ctx = MagicMock()

    await bot.cmd_cancel(update, ctx)
    message.reply_text.assert_called_once()
    reply = message.reply_text.call_args[0][0]
    assert "Ride request cancelled" in reply
    assert "refunded" in reply

    # Balance refunded to 50000
    assert svc.get_user(student["id"])["balance"] == 50000

@pytest.mark.anyio
async def test_notifications_on_expiry_boarding_and_arrival():
    # 1. Test 3-minute expiry refund notification
    s1 = svc.register_student(1001, "Student 1")
    t0 = 1000.0
    h1 = svc.create_seat_hold(s1["id"], db.stop_id("North Gate"), db.stop_id("ICS"), now=t0, ttl=180)

    # Reaper sweeps at t0 + 180 (3 minutes)
    svc.reap_expired_holds(now=t0 + 180)
    notifs = svc.pending_notifications()
    assert len(notifs) == 1
    assert notifs[0]["telegram_id"] == 1001
    assert "Expired & Refunded" in notifs[0]["text"]
    assert "₦150.00 has been automatically refunded" in notifs[0]["text"]

    # 2. Test boarding notification
    s2 = svc.register_student(1002, "Student 2")
    driver_id = svc.driver_by_token("driver-a-token")
    h2 = svc.create_seat_hold(s2["id"], db.stop_id("North Gate"), db.stop_id("ICS"))
    svc.claim_seats(driver_id, db.stop_id("North Gate"), 1)
    # Clear claim notification
    svc.pending_notifications()

    # Board passenger
    svc.board_passenger(driver_id, h2["id"], h2["pin"])
    board_notifs = svc.pending_notifications()
    assert len(board_notifs) == 1
    assert board_notifs[0]["telegram_id"] == 1002
    assert "Boarding Confirmed" in board_notifs[0]["text"]
    assert "Welcome aboard" in board_notifs[0]["text"]
    assert "ICS" in board_notifs[0]["text"]

    # 3. Test arrival and thank you message
    svc.complete_leg(driver_id, h2["id"])
    arrival_notifs = svc.pending_notifications()
    assert len(arrival_notifs) == 1
    assert arrival_notifs[0]["telegram_id"] == 1002
    assert "Arrived at ICS" in arrival_notifs[0]["text"]
    assert "Thank you for riding with ABURide" in arrival_notifs[0]["text"]

