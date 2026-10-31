import re, threading, time
import pytest
from fastapi.testclient import TestClient
from aburide import db, services as svc
from aburide.main import app
from conftest import students, hold

NG = lambda: db.stop_id("North Gate")


def _race(fns):
    barrier, out = threading.Barrier(len(fns)), [None] * len(fns)
    def run(i):
        barrier.wait()
        try: out[i] = len(fns[i]())
        except svc.DomainError as e: out[i] = e
    ts = [threading.Thread(target=run, args=(i,)) for i in range(len(fns))]
    [t.start() for t in ts]; [t.join() for t in ts]
    return out


def test_two_drivers_race_for_three_students(drivers):
    a, b = drivers
    for _ in range(15):                                    # repeat to shake out interleavings
        with db.tx() as c: c.execute("DELETE FROM holds")  # fresh round (students re-used)
        ss = students(3)
        for s in ss: hold(s)
        out = _race([lambda: svc.claim_seats(a, NG(), 2), lambda: svc.claim_seats(b, NG(), 2)])
        assert sorted(out) == [1, 2], out
        with db.read() as c:
            st = [r[0] for r in c.execute("SELECT status FROM holds")]
        assert st.count("CLAIMED") == 3 and st.count("PENDING") == 0     # nothing double-claimed
        with db.tx() as c: c.execute("DELETE FROM holds")


def test_capacity_race_never_overbooks(drivers):
    a, _ = drivers                                         # capacity 4, 6 waiting, two threads want 3 each
    for s in students(6): hold(s)
    out = _race([lambda: svc.claim_seats(a, NG(), 3)] * 2)
    assert sorted(map(str, out))[0].startswith("3") and any(isinstance(o, svc.CapacityExceeded) for o in out)
    with db.read() as c:
        assert c.execute("SELECT COUNT(*) FROM holds WHERE driver_id=?", (a,)).fetchone()[0] <= 4


def test_expiry_refunds_exact_fare(drivers):
    s, = students(1); t0 = time.time()
    before = svc.get_user(s["id"])["balance"]
    h = hold(s, now=t0, ttl=3)
    assert svc.get_user(s["id"])["balance"] == before - h["fare"]
    assert svc.driver_state(drivers[0], now=t0 + 1)["demand"][0]["count"] == 1
    assert svc.reap_expired_holds(now=t0 + 2) == 0         # not yet
    assert svc.reap_expired_holds(now=t0 + 3) == 1
    assert svc.get_user(s["id"])["balance"] == before
    assert svc.driver_state(drivers[0], now=t0 + 3)["demand"] == []
    with db.read() as c:
        assert c.execute("SELECT status FROM holds").fetchone()[0] == "EXPIRED"
    assert svc.reap_expired_holds(now=t0 + 99) == 0        # idempotent: no double refund
    assert svc.get_user(s["id"])["balance"] == before
    with pytest.raises(svc.NoDemand):                      # expired holds can't be claimed
        svc.claim_seats(drivers[0], NG(), 1, now=t0 + 3)


def test_capacity_invariant_http_400(drivers):
    for s in students(6): hold(s)
    r = TestClient(app).post("/api/driver/claim", json={"origin_id": NG(), "seats": 5},
                             headers={"X-Token": "driver-a-token"})
    assert r.status_code == 400 and r.json()["type"] == "CapacityExceeded"
    with pytest.raises(svc.CapacityExceeded): svc.claim_seats(drivers[0], NG(), 5)


def test_insufficient_funds_and_single_open_hold():
    s, = students(1)
    hold(s)
    with pytest.raises(svc.InvalidState): hold(s)
    p, = students(1)
    with db.tx() as c: c.execute("UPDATE users SET balance=100 WHERE id=?", (p["id"],))
    with pytest.raises(svc.InsufficientFunds): hold(p)


def test_end_to_end_flow(drivers):
    a, _ = drivers; cl = TestClient(app); H = {"X-Token": "driver-a-token"}
    assert cl.get("/api/driver/state").status_code == 401
    s, = students(1)                                        # "Telegram /ride" -> hold
    h = hold(s, "Ribadu", "ICS")
    cl.post("/api/driver/location", json={"stop_id": db.stop_id("Main Gate")}, headers=H)
    r = cl.get("/api/driver/state", headers=H); st = r.json()
    assert st["demand"][0]["stop"] == "Ribadu" and st["demand"][0]["count"] == 1
    assert cl.get("/api/driver/state", headers={**H, "If-None-Match": r.headers["etag"]}).status_code == 304
    assert cl.post("/api/driver/claim", json={"origin_id": db.stop_id("Ribadu"), "seats": 1}, headers=H).status_code == 200
    msgs = svc.pending_notifications()
    assert msgs and "claimed your hold" in msgs[0]["text"] and msgs[0]["telegram_id"] == s["telegram_id"]
    pin = re.search(r"PIN: (\d \d \d)", msgs[0]["text"]).group(1).replace(" ", "")
    hid = cl.get("/api/driver/state", headers=H).json()["manifest"][0]["hold_id"]
    bad = "000" if pin != "000" else "111"
    assert cl.post("/api/driver/board", json={"hold_id": hid, "pin": bad}, headers=H).status_code == 400
    assert cl.post("/api/driver/complete", json={"hold_id": hid}, headers=H).status_code == 400   # can't skip boarding
    assert cl.post("/api/driver/board", json={"hold_id": hid, "pin": pin}, headers=H).status_code == 200
    assert svc.get_user(a)["balance"] == h["fare"]          # escrow released to driver
    assert cl.post("/api/driver/complete", json={"hold_id": hid}, headers=H).status_code == 200
    fin = cl.get("/api/driver/state", headers=H).json()
    assert fin["manifest"] == [] and fin["driver"]["free_seats"] == 4
    assert fin["driver"]["stop_id"] == db.stop_id("ICS")    # position inferred from discrete transitions
    assert svc.reap_expired_holds(now=time.time() + 9999) == 0  # completed holds never refunded
