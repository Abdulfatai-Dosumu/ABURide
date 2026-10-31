"""
Tests for Admin Dashboard API and Driver Lifecycle Management.
"""

import pytest
from fastapi.testclient import TestClient
from aburide import db, services as svc
from aburide.main import app

ADMIN_TOKEN = "admin-secret-token"


@pytest.fixture(autouse=True)
def setup_admin_db(tmp_path):
    db.configure(str(tmp_path / "admin_test.db"))
    db.init()


def test_admin_stats_and_driver_listing():
    client = TestClient(app)

    # Missing token -> 401
    res = client.get("/api/admin/stats")
    assert res.status_code == 401

    # Valid token -> 200
    res = client.get("/api/admin/stats", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert res.status_code == 200
    data = res.json()
    assert "total_drivers" in data
    assert data["total_drivers"] >= 2
    assert "students_waiting" in data


def test_admin_add_driver_and_authenticate():
    client = TestClient(app)

    # Add keke driver
    new_driver = {
        "name": "Driver Danladi",
        "vehicle_type": "keke",
        "capacity": 3,
        "plate": "ABU-K-888",
        "token": "driver-danladi-tok"
    }
    res = client.post("/api/admin/drivers/add", json=new_driver, headers={"X-Admin-Token": ADMIN_TOKEN})
    assert res.status_code == 200
    driver_info = res.json()
    assert driver_info["name"] == "Driver Danladi"
    assert driver_info["vehicle_type"] == "keke"

    # Authenticate new driver
    auth_res = client.get("/api/driver/state", headers={"X-Token": "driver-danladi-tok"})
    assert auth_res.status_code == 200
    state = auth_res.json()
    assert state["driver"]["vehicle_type"] == "keke"
    assert state["driver"]["plate"] == "ABU-K-888"


def test_admin_suspend_and_reinstate_driver():
    client = TestClient(app)
    driver_id = svc.driver_by_token("driver-a-token")

    # Suspend driver
    res = client.post(f"/api/admin/drivers/{driver_id}/suspend", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert res.status_code == 200

    # Suspended driver should now receive 403 Forbidden
    driver_res = client.get("/api/driver/state", headers={"X-Token": "driver-a-token"})
    assert driver_res.status_code == 403
    assert "suspended" in driver_res.json()["error"].lower()

    # Reinstate driver
    res = client.post(f"/api/admin/drivers/{driver_id}/reinstate", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert res.status_code == 200

    # Can now access state again
    driver_res = client.get("/api/driver/state", headers={"X-Token": "driver-a-token"})
    assert driver_res.status_code == 200


def test_admin_remove_driver():
    client = TestClient(app)
    driver_id = svc.driver_by_token("driver-b-token")

    res = client.delete(f"/api/admin/drivers/{driver_id}", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert res.status_code == 200
    assert res.json()["deleted"] is True

    # Token is cleared, state gives 401
    driver_res = client.get("/api/driver/state", headers={"X-Token": "driver-b-token"})
    assert driver_res.status_code == 401
