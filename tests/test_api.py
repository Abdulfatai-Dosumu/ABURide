import pytest
from fastapi.testclient import TestClient
from aburide import db, services as svc
from aburide.main import app

@pytest.fixture(autouse=True)
def init_db(tmp_path):
    db.configure(str(tmp_path / "api_test.db"))
    db.init()

def test_index_serves_driver_html():
    client = TestClient(app)
    res = client.get("/")
    assert res.status_code == 200
    assert "text/html" in res.headers["content-type"]
    assert "ABURide Driver" in res.text
    assert "Demand Heat Map" in res.text
    assert "Driver sign" in res.text

def test_list_stops():
    client = TestClient(app)
    res = client.get("/api/stops")
    assert res.status_code == 200
    stops = res.json()
    assert len(stops) == 5
    names = [s["name"] for s in stops]
    assert "North Gate" in names
    assert "Main Gate" in names
    assert "Complex" in names
    assert "Ribadu" in names
    assert "ICS" in names

def test_unauthorized_driver_access():
    client = TestClient(app)
    # Missing token
    assert client.get("/api/driver/state").status_code == 401
    # Invalid token
    assert client.get("/api/driver/state", headers={"X-Token": "invalid-token"}).status_code == 401

def test_authorized_driver_state():
    client = TestClient(app)
    res = client.get("/api/driver/state", headers={"X-Token": "driver-a-token"})
    assert res.status_code == 200
    data = res.json()
    assert data["driver"]["name"] == "Driver Abdullahi"
    assert data["driver"]["capacity"] == 4
    assert data["driver"]["free_seats"] == 4
    assert "ETag" in res.headers

def test_heatmap_student_dots_in_driver_state():
    client = TestClient(app)
    driver_headers = {"X-Token": "driver-a-token"}
    ng_id = db.stop_id("North Gate")
    ics_id = db.stop_id("ICS")

    # Initially 0 student dots
    res0 = client.get("/api/driver/state", headers=driver_headers).json()
    assert res0["student_dots"] == []

    # Create 2 student holds at North Gate
    s1 = svc.register_student(1111, "Student 1")
    s2 = svc.register_student(2222, "Student 2")
    svc.create_seat_hold(s1["id"], ng_id, ics_id)
    svc.create_seat_hold(s2["id"], ng_id, ics_id)

    res1 = client.get("/api/driver/state", headers=driver_headers).json()
    dots = res1["student_dots"]
    assert len(dots) == 2
    for dot in dots:
        assert dot["stop_id"] == ng_id
        assert dot["stop_name"] == "North Gate"
        assert abs(dot["lat"] - 11.1620) < 0.001
        assert abs(dot["lon"] - 7.6340) < 0.001
        assert "wait_min" in dot

    # Driver claims 2 seats -> dots disappear from waiting queue
    client.post("/api/driver/claim", json={"origin_id": ng_id, "seats": 2}, headers=driver_headers)
    res2 = client.get("/api/driver/state", headers=driver_headers).json()
    assert res2["student_dots"] == []

def test_student_simulator_endpoints():
    client = TestClient(app)
    # Check page loads
    res = client.get("/student")
    assert res.status_code == 200
    assert "ABURide Bot" in res.text

    # /start
    res_start = client.post("/api/bot/message", json={"telegram_id": 999111, "text": "/start", "name": "Fatima"}).json()
    assert "Welcome to ABURide" in res_start["text"]
    assert "₦500.00" in res_start["text"]

    # /ride
    res_ride = client.post("/api/bot/message", json={"telegram_id": 999111, "text": "/ride"}).json()
    assert "pickup stop" in res_ride["text"]
    assert len(res_ride["keyboards"]) >= 2

    # origin callback
    ng_id = db.stop_id("North Gate")
    mg_id = db.stop_id("Main Gate")
    res_orig = client.post("/api/bot/callback", json={"telegram_id": 999111, "data": f"origin:{ng_id}"}).json()
    assert "Pickup: *North Gate*" in res_orig["text"]

    # dest callback
    res_dest = client.post("/api/bot/callback", json={"telegram_id": 999111, "data": f"dest:{mg_id}"}).json()
    assert "North Gate" in res_dest["text"] and "Main Gate" in res_dest["text"]
    assert "₦150.00" in res_dest["text"]

    # confirm callback
    res_conf = client.post("/api/bot/callback", json={"telegram_id": 999111, "data": f"confirm:{ng_id}:{mg_id}"}).json()
    assert "Ride Booked!" in res_conf["text"]
    assert "PIN:" in res_conf["text"]
