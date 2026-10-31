import re
import pytest
from unittest.mock import AsyncMock, MagicMock
from fastapi.testclient import TestClient
from telegram import Update, User, CallbackQuery
from aburide import db, bot, services as svc
from aburide.main import app

@pytest.fixture(autouse=True)
def setup_db(tmp_path):
    db_file = str(tmp_path / "e2e.db")
    db.configure(db_file)
    db.init()
    bot._load_stops()
    bot._user_state.clear()

@pytest.mark.anyio
async def test_complete_student_to_driver_e2e_lifecycle():
    client = TestClient(app)
    driver_headers = {"X-Token": "driver-a-token"}
    tg_id = 888777
    student_tg_user = User(id=tg_id, first_name="Zainab", is_bot=False)

    # 1. Student registers via Telegram /start
    start_msg = AsyncMock()
    start_update = MagicMock(spec=Update)
    start_update.effective_user = student_tg_user
    start_update.message = start_msg
    await bot.cmd_start(start_update, MagicMock())
    student = bot._get_user_by_tg(tg_id)
    assert student is not None
    assert student["balance"] == 50000

    # 2. Student books ride via Telegram: North Gate -> ICS
    ng_id = db.stop_id("North Gate")
    ics_id = db.stop_id("ICS")

    cb_query = AsyncMock(spec=CallbackQuery)
    cb_query.from_user = student_tg_user
    cb_query.answer = AsyncMock()
    cb_query.edit_message_text = AsyncMock()
    cb_update = MagicMock(spec=Update)
    cb_update.callback_query = cb_query

    # Step: select origin
    cb_query.data = f"origin:{ng_id}"
    await bot.handle_callback(cb_update, MagicMock())

    # Step: select destination
    cb_query.data = f"dest:{ics_id}"
    await bot.handle_callback(cb_update, MagicMock())

    # Step: confirm booking
    cb_query.data = f"confirm:{ng_id}:{ics_id}"
    await bot.handle_callback(cb_update, MagicMock())

    # Check student balance debited by N150 (15000 cents)
    student_after_booking = bot._get_user_by_tg(tg_id)
    assert student_after_booking["balance"] == 35000

    # 3. Driver checks dashboard - sees demand at North Gate
    state_res = client.get("/api/driver/state", headers=driver_headers)
    assert state_res.status_code == 200
    state = state_res.json()
    demand_at_ng = next((d for d in state["demand"] if d["stop_id"] == ng_id), None)
    assert demand_at_ng is not None
    assert demand_at_ng["count"] == 1
    assert demand_at_ng["stop"] == "North Gate"

    # 4. Driver declares current location at North Gate
    loc_res = client.post("/api/driver/location", json={"stop_id": ng_id}, headers=driver_headers)
    assert loc_res.status_code == 200

    # 5. Driver claims 1 seat at North Gate
    claim_res = client.post("/api/driver/claim", json={"origin_id": ng_id, "seats": 1}, headers=driver_headers)
    assert claim_res.status_code == 200
    claimed_holds = claim_res.json()
    assert len(claimed_holds) == 1
    hold_id = claimed_holds[0]["id"]
    expected_pin = claimed_holds[0]["pin"]

    # 6. Bot notification dispatcher delivers the arrival alert to Student
    bot_ctx = MagicMock()
    bot_mock = AsyncMock()
    bot_ctx.bot = bot_mock
    await bot._dispatch_notifications(bot_ctx)

    bot_mock.send_message.assert_called_once()
    sent_text = bot_mock.send_message.call_args[1]["text"]
    assert "Driver claimed your hold" in sent_text
    assert "North Gate" in sent_text
    assert f"{expected_pin[0]} {expected_pin[1]} {expected_pin[2]}" in sent_text

    # 7. Driver inspects manifest on dashboard
    state2 = client.get("/api/driver/state", headers=driver_headers).json()
    assert len(state2["manifest"]) == 1
    assert state2["manifest"][0]["hold_id"] == hold_id
    assert state2["manifest"][0]["status"] == "CLAIMED"
    assert state2["manifest"][0]["from"] == "North Gate"
    assert state2["manifest"][0]["to"] == "ICS"
    assert state2["driver"]["free_seats"] == 3  # Capacity 4 - 1 = 3

    # 8. Driver attempts invalid PIN boarding -> Rejected (400)
    bad_board = client.post("/api/driver/board", json={"hold_id": hold_id, "pin": "999"}, headers=driver_headers)
    assert bad_board.status_code == 400
    assert bad_board.json()["type"] == "BadPin"

    # 9. Driver boards with valid PIN -> Accepted, escrow paid to driver
    driver_balance_before = state2["driver"]["balance"]
    board_res = client.post("/api/driver/board", json={"hold_id": hold_id, "pin": expected_pin}, headers=driver_headers)
    assert board_res.status_code == 200
    assert board_res.json()["status"] == "BOARDED"

    driver_a_id = svc.driver_by_token("driver-a-token")
    assert svc.get_user(driver_a_id)["balance"] == driver_balance_before + 15000

    # 10. Driver completes the trip leg -> Dropped off at ICS
    comp_res = client.post("/api/driver/complete", json={"hold_id": hold_id}, headers=driver_headers)
    assert comp_res.status_code == 200
    assert comp_res.json()["status"] == "COMPLETED"

    # 11. Final state verification
    state_final = client.get("/api/driver/state", headers=driver_headers).json()
    assert state_final["manifest"] == []
    assert state_final["driver"]["free_seats"] == 4  # Seat freed
    assert state_final["driver"]["stop_id"] == ics_id  # Discrete position inferred to ICS

    # 12. Reaper check: ensure completed hold is never expired or refunded
    reaped = svc.reap_expired_holds(now=9999999999.0)
    assert reaped == 0
    student_final = bot._get_user_by_tg(tg_id)
    assert student_final["balance"] == 35000  # Student was not refunded
