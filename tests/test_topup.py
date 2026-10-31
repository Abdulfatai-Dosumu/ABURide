"""
Tests for Track B1: Wallet Top-Up Schema, Ledger, and Idempotency.
"""

import pytest
from aburide import db, services as svc
from aburide.services import InvalidState


def test_create_and_fulfill_topup_credits_wallet(tmp_path):
    db.configure(str(tmp_path / "topup_test.db"))
    db.init()

    # Register student
    student = svc.register_student(999111, "Amina Sani")
    initial_balance = student["balance"]

    # Create topup request of N1,000 (100,000 cents)
    ref = "ABU-TOPUP-TEST-001"
    topup = svc.create_topup_request(student["id"], 100_000, ref, provider="paystack")

    assert topup["status"] == "PENDING"
    assert topup["amount"] == 100_000
    assert topup["reference"] == ref

    # Fulfill topup
    fulfilled = svc.fulfill_topup(ref, amount=100_000)
    assert fulfilled["status"] == "SUCCESS"

    # Verify user balance credited
    updated_user = svc.get_user(student["id"])
    assert updated_user["balance"] == initial_balance + 100_000


def test_topup_idempotency_prevents_double_crediting(tmp_path):
    db.configure(str(tmp_path / "idempotent_test.db"))
    db.init()

    student = svc.register_student(999222, "Aliyu Bello")
    initial_balance = student["balance"]

    ref = "ABU-TOPUP-DUP-002"
    svc.create_topup_request(student["id"], 50_000, ref)

    # First fulfillment
    svc.fulfill_topup(ref, amount=50_000)
    user_after_first = svc.get_user(student["id"])
    assert user_after_first["balance"] == initial_balance + 50_000

    # Duplicate fulfillment (e.g. replayed webhook)
    dup = svc.fulfill_topup(ref, amount=50_000)
    assert dup["status"] == "SUCCESS"

    # Balance must remain exactly credited once
    user_after_second = svc.get_user(student["id"])
    assert user_after_second["balance"] == initial_balance + 50_000


def test_topup_rejects_amount_mismatch(tmp_path):
    db.configure(str(tmp_path / "mismatch_test.db"))
    db.init()

    student = svc.register_student(999333, "Fatima Umar")
    ref = "ABU-TOPUP-MISMATCH-003"
    svc.create_topup_request(student["id"], 50_000, ref)

    with pytest.raises(ValueError, match="Amount mismatch"):
        svc.fulfill_topup(ref, amount=20_000)


def test_student_simulator_topup_flow(tmp_path):
    from fastapi.testclient import TestClient
    from aburide.main import app

    db.configure(str(tmp_path / "sim_topup.db"))
    db.init()
    client = TestClient(app)

    # 1. Start student
    res = client.post("/api/bot/message", json={"telegram_id": 888777, "text": "/start", "name": "Zainab Sim"})
    assert res.status_code == 200
    assert "Welcome to ABURide" in res.json()["text"]

    # 2. Trigger /topup command
    res = client.post("/api/bot/message", json={"telegram_id": 888777, "text": "/topup"})
    assert res.status_code == 200
    data = res.json()
    assert "Wallet Top-Up" in data["text"]
    assert len(data["keyboards"]) >= 2

    # 3. Choose amount (₦500)
    res = client.post("/api/bot/callback", json={"telegram_id": 888777, "data": "topup_amt:500"})
    assert res.status_code == 200
    data = res.json()
    assert "Invoice Generated" in data["text"]
    check_btn = [b for row in data["keyboards"] for b in row if "check_topup:" in b.get("data", "")][0]
    ref = check_btn["data"].split(":")[1]

    # 4. Confirm payment status
    res = client.post("/api/bot/callback", json={"telegram_id": 888777, "data": f"check_topup:{ref}"})
    assert res.status_code == 200
    assert "Payment Verified" in res.json()["text"]
    assert "1000.00" in res.json()["text"]  # 500 bonus + 500 top-up
