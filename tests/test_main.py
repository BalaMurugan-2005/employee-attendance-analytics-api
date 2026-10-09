"""
Tests for the Employee Attendance & Analytics API.

These tests run against a real MongoDB instance (test database).
They cover CRUD operations, business rules R1-R10, edge cases, and analytics.

Prerequisites:
  - MongoDB 6.0+ running on localhost:27017 (or set MONGO_URI)
  - pip install pytest httpx

Usage:
  pytest tests/test_main.py -v
"""
import os
import pytest
from datetime import datetime, timezone, timedelta
from fastapi.testclient import TestClient

# Set test database BEFORE importing the app
os.environ["MONGO_DB"] = "attendance_test_db"

from app.main import app, db, IST, epoch_ms_to_datetime, datetime_to_epoch_ms

client = TestClient(app)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------
@pytest.fixture(autouse=True)
def clean_db():
    """Clear all collections before each test for isolation."""
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    yield
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})


def make_epoch_ms(year, month, day, hour, minute, second=0, tz=IST):
    """Helper: create epoch milliseconds from IST components."""
    dt = datetime(year, month, day, hour, minute, second, tzinfo=tz)
    return int(dt.timestamp() * 1000)


def create_employee(emp_code="EMP0001", name="Asha Rao", email="asha@example.com",
                     department="Engineering", shift_start="09:30", shift_end="18:30",
                     joined_on="2026-01-05"):
    """Helper: create an employee via the API."""
    return client.post("/employees", json={
        "emp_code": emp_code,
        "name": name,
        "email": email,
        "department": department,
        "shift_start": shift_start,
        "shift_end": shift_end,
        "joined_on": joined_on,
    })


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------
class TestHealth:
    def test_health_ok(self):
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.json() == {"status": "ok"}


# ---------------------------------------------------------------------------
# Employee CRUD
# ---------------------------------------------------------------------------
class TestEmployees:
    def test_create_employee_success(self):
        resp = create_employee()
        assert resp.status_code == 201
        data = resp.json()
        assert data["emp_code"] == "EMP0001"
        assert data["name"] == "Asha Rao"
        assert data["department"] == "Engineering"
        assert data["shift_start"] == "09:30"
        assert data["shift_end"] == "18:30"
        assert data["joined_on"] == "2026-01-05"
        # created_at should be epoch milliseconds (integer)
        assert isinstance(data["created_at"], int)
        assert data["created_at"] > 100_000_000_000

    def test_create_duplicate_employee_409(self):
        create_employee()
        resp = create_employee()
        assert resp.status_code == 409

    def test_create_invalid_emp_code_422(self):
        resp = client.post("/employees", json={
            "emp_code": "INVALID",
            "name": "Test",
            "email": "test@test.com",
            "department": "Eng",
            "joined_on": "2026-01-01",
        })
        assert resp.status_code == 422

    def test_create_shift_start_equals_end_422(self):
        resp = client.post("/employees", json={
            "emp_code": "EMP0001",
            "name": "Test",
            "email": "test@test.com",
            "department": "Eng",
            "shift_start": "09:30",
            "shift_end": "09:30",
            "joined_on": "2026-01-01",
        })
        assert resp.status_code == 422

    def test_list_employees_empty(self):
        resp = client.get("/employees")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_employees_sorted_by_emp_code(self):
        create_employee("EMP0003", "C", "c@t.com", "Eng")
        create_employee("EMP0001", "A", "a@t.com", "Eng")
        create_employee("EMP0002", "B", "b@t.com", "Sales")

        resp = client.get("/employees")
        data = resp.json()
        assert data["total"] == 3
        assert [e["emp_code"] for e in data["items"]] == ["EMP0001", "EMP0002", "EMP0003"]

    def test_list_employees_filter_department(self):
        create_employee("EMP0001", "A", "a@t.com", "Engineering")
        create_employee("EMP0002", "B", "b@t.com", "Sales")

        resp = client.get("/employees", params={"department": "Sales"})
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["emp_code"] == "EMP0002"

    def test_list_employees_pagination(self):
        for i in range(1, 6):
            create_employee(f"EMP000{i}", f"E{i}", f"e{i}@t.com", "Eng")

        resp = client.get("/employees", params={"page": 2, "page_size": 2})
        data = resp.json()
        assert data["total"] == 5
        assert data["page"] == 2
        assert data["page_size"] == 2
        assert len(data["items"]) == 2
        assert data["items"][0]["emp_code"] == "EMP0003"


# ---------------------------------------------------------------------------
# Punch-In
# ---------------------------------------------------------------------------
class TestPunchIn:
    def test_punch_in_success(self):
        create_employee()
        punched_at = make_epoch_ms(2026, 7, 6, 9, 28)  # 09:28 IST

        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": punched_at,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["emp_code"] == "EMP0001"
        assert data["date"] == "2026-07-06"
        assert data["status"] == "PRESENT"
        assert data["punch_in"] is not None
        assert data["punch_out"] is None
        assert data["work_hours"] is None
        assert data["late_minutes"] == 0  # within grace period
        assert data["overtime_minutes"] == 0
        assert data["half_day"] is False
        assert data["history"] == []

    def test_punch_in_late(self):
        """09:40:01 is late (> 10 min grace). late_minutes = floor since shift_start = 10."""
        create_employee()
        # 09:40:01 IST
        punched_at = make_epoch_ms(2026, 7, 6, 9, 40, 1)

        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": punched_at,
        })
        assert resp.status_code == 201
        data = resp.json()
        assert data["late_minutes"] == 10

    def test_punch_in_not_late_at_boundary(self):
        """09:40:00 is exactly 10 min → NOT late (must be strictly more than 10 min)."""
        create_employee()
        punched_at = make_epoch_ms(2026, 7, 6, 9, 40, 0)

        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": punched_at,
        })
        assert resp.status_code == 201
        assert resp.json()["late_minutes"] == 0

    def test_punch_in_very_late(self):
        """10:15:59 IST → 45 minutes late (floor of 45.983...)."""
        create_employee()
        punched_at = make_epoch_ms(2026, 7, 6, 10, 15, 59)

        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": punched_at,
        })
        assert resp.status_code == 201
        assert resp.json()["late_minutes"] == 45

    def test_punch_in_unknown_employee_404(self):
        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP9999",
            "punched_at": make_epoch_ms(2026, 7, 6, 9, 30),
        })
        assert resp.status_code == 404

    def test_punch_in_duplicate_409(self):
        create_employee()
        punched_at = make_epoch_ms(2026, 7, 6, 9, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punched_at})

        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": make_epoch_ms(2026, 7, 6, 10, 0),
        })
        assert resp.status_code == 409

    def test_punch_in_invalid_status_422(self):
        create_employee()
        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": make_epoch_ms(2026, 7, 6, 9, 30),
            "status": "ABSENT",
        })
        assert resp.status_code == 422

    def test_punch_in_wfh_status(self):
        create_employee()
        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": make_epoch_ms(2026, 7, 6, 9, 30),
            "status": "WFH",
        })
        assert resp.status_code == 201
        assert resp.json()["status"] == "WFH"

    def test_punch_in_epoch_ms_validation(self):
        """Values below 100000000000 should be rejected."""
        create_employee()
        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": 1000,  # looks like seconds
        })
        assert resp.status_code == 422

    def test_punch_in_rejects_float_epoch_ms(self):
        create_employee()
        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": 1783312500000.5,
        })
        assert resp.status_code == 422

    def test_punch_in_rejects_string_epoch_ms(self):
        create_employee()
        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001",
            "punched_at": "1783312500000",
        })
        assert resp.status_code == 422

    def test_punch_in_overnight_shift_before_shift_end(self):
        """Overnight shift (22:00-06:00): punch at 03:00 IST should belong to previous day."""
        create_employee("EMP0005", "Meera", "meera@t.com", "Support", "22:00", "06:00", "2026-03-10")
        # Punch at 03:00 IST on July 7 → should be date 2026-07-06 (previous day's shift)
        punched_at = make_epoch_ms(2026, 7, 7, 3, 0)

        resp = client.post("/attendance/punch-in", json={
            "emp_code": "EMP0005",
            "punched_at": punched_at,
        })
        assert resp.status_code == 201
        assert resp.json()["date"] == "2026-07-06"


# ---------------------------------------------------------------------------
# Punch-Out
# ---------------------------------------------------------------------------
class TestPunchOut:
    def test_punch_out_success(self):
        create_employee()
        punch_in_time = make_epoch_ms(2026, 7, 6, 9, 28)
        punch_out_time = make_epoch_ms(2026, 7, 6, 18, 35)

        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in_time})
        resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punch_out_time})

        assert resp.status_code == 200
        data = resp.json()
        assert data["punch_out"] is not None
        assert data["work_hours"] == 9.12  # (18:35 - 09:28) / 3600 rounded half-up
        assert data["overtime_minutes"] == 0  # 5 min < 30 min threshold
        assert data["half_day"] is False

    def test_punch_out_with_overtime(self):
        """Punch out 40 min after shift end → overtime = 40 (>= 30 threshold)."""
        create_employee()
        punch_in_time = make_epoch_ms(2026, 7, 6, 10, 5)
        punch_out_time = make_epoch_ms(2026, 7, 6, 19, 10)

        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in_time})
        resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punch_out_time})

        assert resp.status_code == 200
        data = resp.json()
        assert data["overtime_minutes"] == 40

    def test_punch_out_half_day(self):
        """Work < 4.50 hours → half_day = True."""
        create_employee()
        punch_in_time = make_epoch_ms(2026, 7, 7, 9, 30)
        punch_out_time = make_epoch_ms(2026, 7, 7, 13, 0)  # 3.5 hours

        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": punch_in_time})
        resp = client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": punch_out_time})

        assert resp.status_code == 200
        data = resp.json()
        assert data["work_hours"] == 3.5
        assert data["half_day"] is True

    def test_punch_out_unknown_employee_404(self):
        resp = client.post("/attendance/punch-out", json={
            "emp_code": "EMP9999",
            "punched_at": make_epoch_ms(2026, 7, 6, 18, 30),
        })
        assert resp.status_code == 404

    def test_punch_out_no_open_record_404(self):
        create_employee()
        resp = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": make_epoch_ms(2026, 7, 6, 18, 30),
        })
        assert resp.status_code == 404

    def test_punch_out_already_punched_out_409(self):
        create_employee()
        pi = make_epoch_ms(2026, 7, 6, 9, 30)
        po = make_epoch_ms(2026, 7, 6, 18, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po})

        resp = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": make_epoch_ms(2026, 7, 6, 19, 0),
        })
        assert resp.status_code == 409

    def test_punch_out_before_punch_in_422(self):
        create_employee()
        pi = make_epoch_ms(2026, 7, 6, 9, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi})

        resp = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": make_epoch_ms(2026, 7, 6, 8, 0),  # Before punch_in
        })
        assert resp.status_code == 422  # 422 if punched_at <= punch_in

    def test_punch_out_more_than_24h_422(self):
        create_employee()
        pi = make_epoch_ms(2026, 7, 6, 9, 30)
        po = make_epoch_ms(2026, 7, 7, 9, 31)  # > 24 hours
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi})

        resp = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": po,
        })
        assert resp.status_code == 422

    def test_punch_out_with_older_closed_record_and_invalid_timestamp_returns_422(self):
        """When an older record is closed and a newer open record exists, punching out before
        the open record's punch_in should return 422 (not 409 from matching the older record)."""
        create_employee()
        # Day 1: punch in and punch out
        pi1 = make_epoch_ms(2026, 7, 6, 9, 30)
        po1 = make_epoch_ms(2026, 7, 6, 18, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi1})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po1})

        # Day 2: punch in at 09:30
        pi2 = make_epoch_ms(2026, 7, 7, 9, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi2})

        # Try to punch out on Day 2 at 08:30 (before Day 2 punch-in)
        po2_invalid = make_epoch_ms(2026, 7, 7, 8, 30)
        resp = client.post("/attendance/punch-out", json={
            "emp_code": "EMP0001",
            "punched_at": po2_invalid,
        })
        assert resp.status_code == 422


# ---------------------------------------------------------------------------
# Attendance List
# ---------------------------------------------------------------------------
class TestListAttendance:
    def test_list_attendance_empty(self):
        resp = client.get("/attendance")
        assert resp.status_code == 200
        data = resp.json()
        assert data["items"] == []
        assert data["total"] == 0

    def test_list_attendance_sorted(self):
        """Sorted by date desc, then emp_code asc."""
        create_employee("EMP0001", "A", "a@t.com")
        create_employee("EMP0002", "B", "b@t.com")

        client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": make_epoch_ms(2026, 7, 6, 9, 30)
        })
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP0002", "punched_at": make_epoch_ms(2026, 7, 6, 9, 35)
        })
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": make_epoch_ms(2026, 7, 7, 9, 30)
        })

        resp = client.get("/attendance")
        data = resp.json()
        assert data["total"] == 3
        # Date desc, then emp_code asc
        assert data["items"][0]["date"] == "2026-07-07"
        assert data["items"][1]["date"] == "2026-07-06"
        assert data["items"][1]["emp_code"] == "EMP0001"
        assert data["items"][2]["date"] == "2026-07-06"
        assert data["items"][2]["emp_code"] == "EMP0002"

    def test_list_attendance_filter_by_date_range(self):
        create_employee()
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": make_epoch_ms(2026, 7, 6, 9, 30)
        })
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": make_epoch_ms(2026, 7, 7, 9, 30)
        })
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": make_epoch_ms(2026, 7, 8, 9, 30)
        })

        resp = client.get("/attendance", params={"date_from": "2026-07-07", "date_to": "2026-07-07"})
        data = resp.json()
        assert data["total"] == 1
        assert data["items"][0]["date"] == "2026-07-07"

    def test_list_date_from_after_date_to_422(self):
        resp = client.get("/attendance", params={"date_from": "2026-07-08", "date_to": "2026-07-06"})
        assert resp.status_code == 422

    def test_list_attendance_no_id_field(self):
        """Response must NOT contain 'id' or '_id'."""
        create_employee()
        client.post("/attendance/punch-in", json={
            "emp_code": "EMP0001", "punched_at": make_epoch_ms(2026, 7, 6, 9, 30)
        })

        resp = client.get("/attendance")
        data = resp.json()
        assert "_id" not in data["items"][0]
        assert "id" not in data["items"][0]


# ---------------------------------------------------------------------------
# Regularize (Manual Correction)
# ---------------------------------------------------------------------------
class TestRegularize:
    def test_regularize_punch_in_correction(self):
        """Correct punch_in and verify history is appended."""
        create_employee()
        pi = make_epoch_ms(2026, 7, 6, 10, 5)  # 35 min late
        po = make_epoch_ms(2026, 7, 6, 18, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po})

        # Correct punch_in to 09:28
        new_pi = make_epoch_ms(2026, 7, 6, 9, 28)
        resp = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": new_pi,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        })

        assert resp.status_code == 200
        data = resp.json()
        assert data["late_minutes"] == 0  # Now within grace
        assert len(data["history"]) == 1
        assert data["history"][0]["by"] == "hr.admin"
        assert data["history"][0]["reason"] == "biometric glitch"
        assert "punch_in" in data["history"][0]["changes"]
        assert "late_minutes" in data["history"][0]["changes"]

    def test_regularize_employee_not_found_404(self):
        resp = client.patch("/attendance/EMP9999/2026-07-06", json={
            "reason": "test reason",
            "regularized_by": "admin",
        })
        assert resp.status_code == 404

    def test_regularize_no_record_404(self):
        create_employee()
        resp = client.patch("/attendance/EMP0001/2026-07-06", json={
            "reason": "test reason",
            "regularized_by": "admin",
        })
        assert resp.status_code == 404

    def test_regularize_no_change_422(self):
        """A request that changes nothing should return 422."""
        create_employee()
        pi = make_epoch_ms(2026, 7, 6, 9, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi})

        resp = client.patch("/attendance/EMP0001/2026-07-06", json={
            "reason": "testing no change",
            "regularized_by": "admin",
        })
        assert resp.status_code == 422

    def test_regularize_absent_with_punch_in_422(self):
        """ABSENT/LEAVE + punch_in → 422."""
        create_employee()
        pi = make_epoch_ms(2026, 7, 6, 9, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi})

        resp = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "punch_in": make_epoch_ms(2026, 7, 6, 10, 0),
            "reason": "invalid correction",
            "regularized_by": "admin",
        })
        assert resp.status_code == 422

    def test_regularize_to_absent_clears_punches(self):
        """Changing to ABSENT clears punch times and derived fields."""
        create_employee()
        pi = make_epoch_ms(2026, 7, 6, 9, 30)
        po = make_epoch_ms(2026, 7, 6, 18, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po})

        resp = client.patch("/attendance/EMP0001/2026-07-06", json={
            "status": "ABSENT",
            "reason": "employee was not actually here",
            "regularized_by": "admin",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["status"] == "ABSENT"
        assert data["punch_in"] is None
        assert data["punch_out"] is None
        assert data["work_hours"] is None
        assert data["late_minutes"] == 0
        assert data["overtime_minutes"] == 0
        assert data["half_day"] is False

    def test_regularize_history_preserves_previous_values(self):
        """History entry should contain the old values."""
        create_employee()
        pi = make_epoch_ms(2026, 7, 6, 10, 5)  # 35 min late
        po = make_epoch_ms(2026, 7, 6, 18, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po})

        new_pi = make_epoch_ms(2026, 7, 6, 9, 28)
        resp = client.patch("/attendance/EMP0001/2026-07-06", json={
            "punch_in": new_pi,
            "reason": "biometric glitch",
            "regularized_by": "hr.admin",
        })

        data = resp.json()
        h = data["history"][0]
        # punch_in changed
        assert h["changes"]["punch_in"]["from"] is not None
        assert h["changes"]["punch_in"]["to"] is not None
        # late_minutes changed from 35 to 0
        assert h["changes"]["late_minutes"]["from"] == 35
        assert h["changes"]["late_minutes"]["to"] == 0


# ---------------------------------------------------------------------------
# Analytics: Employee Monthly
# ---------------------------------------------------------------------------
class TestEmployeeMonthly:
    def test_monthly_basic(self):
        create_employee(joined_on="2026-01-05")

        # Create some attendance for July 2026
        # July 6 (Monday) - present, full day
        pi = make_epoch_ms(2026, 7, 6, 9, 28)
        po = make_epoch_ms(2026, 7, 6, 18, 35)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po})

        # July 7 (Tuesday) - present, full day
        pi2 = make_epoch_ms(2026, 7, 7, 9, 28)
        po2 = make_epoch_ms(2026, 7, 7, 18, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi2})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po2})

        resp = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["emp_code"] == "EMP0001"
        assert data["month"] == "2026-07"
        assert data["working_days"] == 23  # July 2026 has 23 working days
        assert data["present_days"] == 2
        assert data["leave_days"] == 0
        assert data["attendance_pct"] is not None

    def test_monthly_unknown_employee_404(self):
        resp = client.get("/analytics/employees/EMP9999/monthly", params={"month": "2026-07"})
        assert resp.status_code == 404

    def test_monthly_no_logs(self):
        create_employee(joined_on="2026-01-05")
        resp = client.get("/analytics/employees/EMP0001/monthly", params={"month": "2026-07"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["present_days"] == 0
        assert data["leave_days"] == 0
        assert data["late_count"] == 0


# ---------------------------------------------------------------------------
# Analytics: Department Summary
# ---------------------------------------------------------------------------
class TestDepartmentSummary:
    def test_department_summary_basic(self):
        create_employee("EMP0001", "A", "a@t.com", "Engineering", joined_on="2026-01-05")
        create_employee("EMP0002", "B", "b@t.com", "Engineering", joined_on="2026-01-05")

        pi = make_epoch_ms(2026, 7, 6, 9, 28)
        po = make_epoch_ms(2026, 7, 6, 18, 35)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po})

        resp = client.get("/analytics/departments/summary", params={"month": "2026-07"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["month"] == "2026-07"
        assert len(data["items"]) >= 1

        eng = next((i for i in data["items"] if i["department"] == "Engineering"), None)
        assert eng is not None
        assert eng["headcount"] == 2
        assert eng["present_days"] >= 1

    def test_department_summary_includes_zero_log_employees(self):
        """R9: Employees with no logs should still be counted in headcount."""
        create_employee("EMP0001", "A", "a@t.com", "Engineering", joined_on="2026-01-05")
        create_employee("EMP0002", "B", "b@t.com", "Engineering", joined_on="2026-01-05")
        # No attendance for either

        resp = client.get("/analytics/departments/summary", params={"month": "2026-07"})
        data = resp.json()
        eng = next((i for i in data["items"] if i["department"] == "Engineering"), None)
        assert eng is not None
        assert eng["headcount"] == 2
        assert eng["present_days"] == 0

    def test_department_summary_excludes_weekend_presence(self):
        """R5-R7: Only Mon-Fri present days are counted in department summary."""
        create_employee("EMP0001", "A", "a@t.com", "Engineering", joined_on="2026-01-05")

        # Saturday punch (2026-07-04)
        pi_sat = make_epoch_ms(2026, 7, 4, 9, 30)
        po_sat = make_epoch_ms(2026, 7, 4, 18, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi_sat})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po_sat})

        # Monday punch (2026-07-06)
        pi_mon = make_epoch_ms(2026, 7, 6, 9, 30)
        po_mon = make_epoch_ms(2026, 7, 6, 18, 30)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi_mon})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po_mon})

        resp = client.get("/analytics/departments/summary", params={"month": "2026-07"})
        assert resp.status_code == 200
        data = resp.json()
        eng = next((i for i in data["items"] if i["department"] == "Engineering"), None)
        assert eng is not None
        # Should be 1.0 (Monday only), NOT 2.0
        assert eng["present_days"] == 1.0


# ---------------------------------------------------------------------------
# Analytics: Late Leaderboard
# ---------------------------------------------------------------------------
class TestLateLeaderboard:
    def test_leaderboard_basic(self):
        create_employee("EMP0001", "A", "a@t.com", "Eng", joined_on="2026-01-05")
        create_employee("EMP0002", "B", "b@t.com", "Eng", joined_on="2026-01-05")

        # EMP0001: 35 min late
        pi1 = make_epoch_ms(2026, 7, 6, 10, 5)  # 35 min late
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi1})

        # EMP0002: 45 min late
        pi2 = make_epoch_ms(2026, 7, 6, 10, 15)  # 45 min late
        client.post("/attendance/punch-in", json={"emp_code": "EMP0002", "punched_at": pi2})

        resp = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
        assert resp.status_code == 200
        data = resp.json()
        assert data["month"] == "2026-07"
        assert len(data["items"]) == 2
        # EMP0002 should be first (more late minutes)
        assert data["items"][0]["emp_code"] == "EMP0002"
        assert data["items"][0]["rank"] == 1
        assert data["items"][1]["emp_code"] == "EMP0001"
        assert data["items"][1]["rank"] == 2

    def test_leaderboard_ties_share_rank(self):
        """Standard competition ranking: ties share a rank, next is skipped."""
        create_employee("EMP0001", "A", "a@t.com", "Eng", joined_on="2026-01-05")
        create_employee("EMP0002", "B", "b@t.com", "Eng", joined_on="2026-01-05")
        create_employee("EMP0003", "C", "c@t.com", "Eng", joined_on="2026-01-05")

        # All three: 35 min late on the same day
        for emp in ["EMP0001", "EMP0002", "EMP0003"]:
            pi = make_epoch_ms(2026, 7, 6, 10, 5)
            client.post("/attendance/punch-in", json={"emp_code": emp, "punched_at": pi})

        resp = client.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2})
        data = resp.json()
        # All three should be rank 1 (tied), and limit=2 includes rank <= 2,
        # so all 3 are included (they all have rank 1)
        assert len(data["items"]) == 3
        for item in data["items"]:
            assert item["rank"] == 1

    def test_leaderboard_empty(self):
        resp = client.get("/analytics/leaderboard/late", params={"month": "2026-07"})
        assert resp.status_code == 200
        assert resp.json()["items"] == []


# ---------------------------------------------------------------------------
# Analytics: Department Trend
# ---------------------------------------------------------------------------
class TestDepartmentTrend:
    def test_trend_basic(self):
        create_employee("EMP0001", "A", "a@t.com", "Engineering", joined_on="2026-01-05")

        pi = make_epoch_ms(2026, 7, 6, 9, 28)
        po = make_epoch_ms(2026, 7, 6, 18, 35)
        client.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": pi})
        client.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": po})

        resp = client.get("/analytics/departments/Engineering/trend", params={
            "from": "2026-07-06",
            "to": "2026-07-08",
        })
        assert resp.status_code == 200
        data = resp.json()
        assert data["department"] == "Engineering"
        assert len(data["items"]) == 3  # 3 days: Jul 6, 7, 8

        # Jul 6 (Monday): should have 1 present
        day0 = data["items"][0]
        assert day0["date"] == "2026-07-06"
        assert day0["is_working_day"] is True
        assert day0["headcount"] == 1
        assert day0["present_count"] == 1

    def test_trend_unknown_department_404(self):
        resp = client.get("/analytics/departments/NonExistent/trend", params={
            "from": "2026-07-06",
            "to": "2026-07-08",
        })
        assert resp.status_code == 404

    def test_trend_invalid_range_422(self):
        create_employee("EMP0001", "A", "a@t.com", "Engineering")
        resp = client.get("/analytics/departments/Engineering/trend", params={
            "from": "2026-07-10",
            "to": "2026-07-06",
        })
        assert resp.status_code == 422

    def test_trend_range_too_large_422(self):
        create_employee("EMP0001", "A", "a@t.com", "Engineering")
        resp = client.get("/analytics/departments/Engineering/trend", params={
            "from": "2026-01-01",
            "to": "2026-12-31",
        })
        assert resp.status_code == 422

    def test_trend_includes_weekends(self):
        """Trend should include weekend days too."""
        create_employee("EMP0001", "A", "a@t.com", "Engineering", joined_on="2026-01-05")

        resp = client.get("/analytics/departments/Engineering/trend", params={
            "from": "2026-07-04",  # Saturday
            "to": "2026-07-05",    # Sunday
        })
        data = resp.json()
        assert len(data["items"]) == 2
        assert data["items"][0]["is_working_day"] is False
        assert data["items"][1]["is_working_day"] is False
        # Weekend: attendance_rate should be null
        assert data["items"][0]["attendance_rate"] is None
        assert data["items"][1]["attendance_rate"] is None


# ---------------------------------------------------------------------------
# Admin: Explain
# ---------------------------------------------------------------------------
class TestExplain:
    def test_explain_attendance_list(self):
        resp = client.get("/admin/explain/attendance_list")
        assert resp.status_code == 200
        data = resp.json()
        assert data["endpoint"] == "attendance_list"
        assert data["collection"] == "attendance_logs"
        assert "explain" in data

    def test_explain_invalid_endpoint_422(self):
        resp = client.get("/admin/explain/invalid_endpoint")
        assert resp.status_code == 422

    def test_explain_employee_monthly_requires_params(self):
        resp = client.get("/admin/explain/employee_monthly")
        assert resp.status_code == 422
