import itertools, pytest
from aburide import db, services as svc

_tg = itertools.count(1000)

@pytest.fixture(autouse=True)
def fresh_db(tmp_path):
    db.configure(str(tmp_path / "t.db")); db.init()

@pytest.fixture
def drivers():
    return svc.driver_by_token("driver-a-token"), svc.driver_by_token("driver-b-token")

def students(n):
    return [svc.register_student(next(_tg), f"S{i}") for i in range(n)]

def hold(student, o="North Gate", d="Main Gate", **kw):
    return svc.create_seat_hold(student["id"], db.stop_id(o), db.stop_id(d), **kw)
