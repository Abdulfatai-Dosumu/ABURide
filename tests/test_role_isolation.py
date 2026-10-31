"""
Tests for Isolated Role-Based Access, Authentication Hardening, and Rate Limiting.
"""

import pytest
from fastapi.testclient import TestClient
from aburide import db, services as svc
from aburide.main import app, _failed_login_attempts

DRIVER_TOKEN = "driver-a-token"
ADMIN_TOKEN = "admin-secret-token"


@pytest.fixture(autouse=True)
def setup_isolation_db(tmp_path):
    db.configure(str(tmp_path / "isolation_test.db"))
    db.init()
    _failed_login_attempts.clear()


def test_driver_cannot_access_admin_api():
    """A driver token calling admin endpoints must be rejected server-side."""
    client = TestClient(app)
    res = client.get("/api/admin/stats", headers={"X-Admin-Token": DRIVER_TOKEN})
    assert res.status_code == 403
    assert "driver credentials cannot access admin" in res.json()["error"].lower()

    res2 = client.get("/api/admin/drivers", headers={"X-Admin-Token": DRIVER_TOKEN})
    assert res2.status_code == 403


def test_admin_cannot_access_driver_api():
    """An admin token calling driver endpoints must be rejected server-side."""
    client = TestClient(app)
    res = client.get("/api/driver/state", headers={"X-Token": ADMIN_TOKEN})
    assert res.status_code == 403
    assert "admin credentials cannot access driver" in res.json()["error"].lower()

    res2 = client.post("/api/driver/location", json={"stop_id": 1}, headers={"X-Token": ADMIN_TOKEN})
    assert res2.status_code == 403


def test_role_checked_login_endpoint():
    """Login form must succeed only for matching role with generic errors on mismatch."""
    client = TestClient(app)

    # 1. Driver authenticating to driver portal -> SUCCESS
    res = client.post("/api/auth/login", json={"token": DRIVER_TOKEN, "role": "driver"})
    assert res.status_code == 200
    data = res.json()
    assert data["ok"] is True
    assert data["role"] == "driver"

    # 2. Driver token submitted to admin portal -> GENERIC 401 (never reveals role existence)
    res_cross_driver = client.post("/api/auth/login", json={"token": DRIVER_TOKEN, "role": "admin"})
    assert res_cross_driver.status_code == 401
    assert res_cross_driver.json()["error"] == "Invalid credentials."

    # 3. Admin authenticating to admin portal -> SUCCESS
    res_admin = client.post("/api/auth/login", json={"token": ADMIN_TOKEN, "role": "admin"})
    assert res_admin.status_code == 200
    assert res_admin.json()["role"] == "admin"

    # 4. Admin token submitted to driver portal -> GENERIC 401
    res_cross_admin = client.post("/api/auth/login", json={"token": ADMIN_TOKEN, "role": "driver"})
    assert res_cross_admin.status_code == 401
    assert res_cross_admin.json()["error"] == "Invalid credentials."

    # 5. Non-existent token -> GENERIC 401
    res_fake = client.post("/api/auth/login", json={"token": "completely-bogus-token", "role": "driver"})
    assert res_fake.status_code == 401
    assert res_fake.json()["error"] == "Invalid credentials."


def test_suspended_driver_login_and_api_blocked():
    """Suspended drivers are blocked at login and from API calls."""
    client = TestClient(app)
    driver_id = svc.driver_by_token(DRIVER_TOKEN)
    svc.suspend_driver(driver_id, reason="Safety review")

    # Login rejected with suspension notice
    res_login = client.post("/api/auth/login", json={"token": DRIVER_TOKEN, "role": "driver"})
    assert res_login.status_code == 403
    assert "suspended" in res_login.json()["error"].lower()

    # Driver API state rejected with 403
    res_api = client.get("/api/driver/state", headers={"X-Token": DRIVER_TOKEN})
    assert res_api.status_code == 403
    assert "suspended" in res_api.json()["error"].lower()


def test_login_rate_limiting():
    """Five failed login attempts from an IP locks out further attempts with 429."""
    client = TestClient(app)

    for i in range(5):
        res = client.post("/api/auth/login", json={"token": f"wrong-pass-{i}", "role": "driver"})
        assert res.status_code == 401

    # 6th attempt should be blocked by rate-limiter
    res_locked = client.post("/api/auth/login", json={"token": DRIVER_TOKEN, "role": "driver"})
    assert res_locked.status_code == 429
    assert "too many failed login attempts" in res_locked.json()["error"].lower()


def test_route_isolation_and_stealth_admin():
    """Test that driver cockpit is publicly served while admin routes are stealth hidden."""
    client = TestClient(app)

    # 1. Gateway index serves driver cockpit with zero admin exposure
    res_gate = client.get("/")
    assert res_gate.status_code == 200
    assert "text/html" in res_gate.headers["content-type"]
    assert "ABURide Driver" in res_gate.text
    assert "Demand Heat Map" in res_gate.text
    assert "Driver sign" in res_gate.text
    assert "Campus Transport Admin" not in res_gate.text

    # 2. Driver Cockpit serves driver sign-in
    res_driver = client.get("/driver")
    assert res_driver.status_code == 200
    assert "Driver sign-in" in res_driver.text

    # 3. Unauthenticated access to /admin returns stealth 404 (hidden from drivers & non-admins)
    res_admin_anon = client.get("/admin")
    assert res_admin_anon.status_code == 404

    # 4. Driver token access to /admin returns stealth 404
    res_admin_driver = client.get("/admin", headers={"X-Admin-Token": DRIVER_TOKEN})
    assert res_admin_driver.status_code == 404

    # 5. Verified admin with token gets full admin dashboard
    res_admin_auth = client.get("/admin", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert res_admin_auth.status_code == 200
    assert "Fleet Administration" in res_admin_auth.text

    # 6. Admin login session via cookie works cleanly
    login_res = client.post("/api/admin/auth/login", json={"token": ADMIN_TOKEN})
    assert login_res.status_code == 200
    assert "admin_session" in login_res.cookies

    # Access /admin using session cookie
    res_cookie_admin = client.get("/admin", cookies=login_res.cookies)
    assert res_cookie_admin.status_code == 200
    assert "Fleet Administration" in res_cookie_admin.text


def test_admin_network_restriction():
    """Admin routes return stealth 404 when requested from unauthorized network IPs."""
    # Simulate an unauthorized client IP
    unauth_client = TestClient(app, client=("198.51.100.24", 54321))

    # Even with valid admin credentials, unauthorized IP receives stealth 404
    res_page = unauth_client.get("/admin", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert res_page.status_code == 404

    res_login_page = unauth_client.get("/admin/login")
    assert res_login_page.status_code == 404

    res_api = unauth_client.get("/api/admin/stats", headers={"X-Admin-Token": ADMIN_TOKEN})
    assert res_api.status_code == 404
