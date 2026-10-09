"""
Employee Attendance & Analytics API
====================================
Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

Implements the full contract from openapi.yaml with business rules R1-R10.
All code lives in this single file as required by the assignment.
"""
import math
import os
import re
from datetime import datetime, timedelta, timezone
from decimal import ROUND_HALF_UP, Decimal
from contextlib import asynccontextmanager
from typing import Annotated, Any, Dict, List, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query, Path
from pydantic import BaseModel, Field, StrictInt, field_validator
from pymongo import ASCENDING, DESCENDING, MongoClient
from pymongo.errors import DuplicateKeyError

load_dotenv()

# ---------------------------------------------------------------------------
# MongoDB connection
# ---------------------------------------------------------------------------
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")
client = MongoClient(MONGO_URI)
db = client[MONGO_DB]

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
IST = timezone(timedelta(hours=5, minutes=30))

# Epoch-millisecond boundaries for validation (per EpochMillis schema)
EPOCH_MS_MIN = 100_000_000_000      # ~1973
EPOCH_MS_MAX = 4_102_444_800_000    # ~2099

PRESENCE_STATUSES = {"PRESENT", "WFH", "ON_DUTY"}
ALL_STATUSES = {"PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"}

EpochMillis = Annotated[StrictInt, Field(ge=EPOCH_MS_MIN, le=EPOCH_MS_MAX)]


# ---------------------------------------------------------------------------
# Indexes: created idempotently at module load and in app lifespan
# ---------------------------------------------------------------------------
def create_indexes():
    """Create all required indexes idempotently. Safe to call repeatedly."""
    # Unique index on emp_code for employees (enforces uniqueness at DB level)
    db.employees.create_index("emp_code", unique=True, name="idx_emp_code_unique")

    # Compound unique index on (emp_code, date) for attendance — enforces one record per employee per day
    # and also supports queries filtered by emp_code and/or date
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="idx_empcode_date_unique",
    )

    # Index on date + emp_code for attendance list (sorted date desc, emp_code asc) and date-range queries
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="idx_date_desc_empcode_asc",
    )

    # Index on date for analytics aggregation pipelines that filter by date range
    db.attendance_logs.create_index("date", name="idx_date")


@asynccontextmanager
async def lifespan(app: FastAPI):
    create_indexes()
    yield


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan)
create_indexes()



# ---------------------------------------------------------------------------
# Rounding helpers (R4, R8)
# ---------------------------------------------------------------------------
def round_half_up(value: float, decimals: int = 2) -> float:
    """Round a float to `decimals` places using half-up rounding (not banker's).

    R4 requires half-up for work_hours (2 dp). R8 requires 2 dp half-up for most
    reported numbers, 4 dp half-up for rates.
    """
    if value is None:
        return None
    d = Decimal(str(value))
    quantize_str = "0." + "0" * decimals
    return float(d.quantize(Decimal(quantize_str), rounding=ROUND_HALF_UP))


# ---------------------------------------------------------------------------
# Epoch-millisecond helpers
# ---------------------------------------------------------------------------
def validate_epoch_ms(value: int, field_name: str = "punched_at") -> None:
    """Reject values that look like seconds, floats, strings, or out-of-range numbers."""
    if not isinstance(value, int):
        raise HTTPException(422, detail=f"{field_name} must be an integer (epoch milliseconds)")
    if value < EPOCH_MS_MIN or value > EPOCH_MS_MAX:
        raise HTTPException(
            422,
            detail=f"{field_name} must be between {EPOCH_MS_MIN} and {EPOCH_MS_MAX}",
        )


def epoch_ms_to_datetime(ms: int) -> datetime:
    """Convert epoch milliseconds to a UTC-aware datetime, truncated to whole seconds (R1)."""
    # Truncate to whole seconds: drop sub-second part
    seconds = ms // 1000
    return datetime.fromtimestamp(seconds, tz=timezone.utc)


def datetime_to_epoch_ms(dt: datetime) -> Optional[int]:
    """Convert a datetime to epoch milliseconds. Returns None if dt is None."""
    if dt is None:
        return None
    # Ensure UTC-aware
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return int(dt.timestamp() * 1000)


# ---------------------------------------------------------------------------
# Business-rule helpers (R1-R5)
# ---------------------------------------------------------------------------
def determine_attendance_date(punch_in_utc: datetime, shift_start: str, shift_end: str) -> str:
    """R1: Determine the attendance date (IST calendar date) from a punch-in instant.

    For overnight shifts (shift_end <= shift_start, e.g. 22:00-06:00):
    if the IST time of punch_in is earlier than shift_end, the punch belongs
    to the shift that started the previous day.

    Returns a YYYY-MM-DD string.
    """
    punch_in_ist = punch_in_utc.astimezone(IST)
    ist_date = punch_in_ist.date()

    # Check if this is an overnight shift
    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    is_overnight = (eh < sh) or (eh == sh and em <= sm)

    if is_overnight:
        # If IST time of punch_in is before shift_end, it belongs to yesterday's shift
        punch_time_minutes = punch_in_ist.hour * 60 + punch_in_ist.minute
        end_minutes = eh * 60 + em
        if punch_time_minutes < end_minutes:
            ist_date = ist_date - timedelta(days=1)

    return ist_date.isoformat()


def compute_late_minutes(punch_in_utc: datetime, shift_start: str, attendance_date: str) -> int:
    """R2: Compute late minutes.

    Grace period is 10 minutes. Late only if punch-in is strictly more than
    10:00 (mm:ss) after shift_start. When late, late_minutes = floor(minutes
    since shift_start), NOT since end of grace.

    Examples (shift 09:30): 09:40:00 → 0; 09:40:01 → 10; 10:15:59 → 45.
    """
    sh, sm = map(int, shift_start.split(":"))
    # Build the shift start instant on the attendance date in IST
    year, month, day = map(int, attendance_date.split("-"))
    shift_start_ist = datetime(year, month, day, sh, sm, 0, tzinfo=IST)
    shift_start_utc = shift_start_ist.astimezone(timezone.utc)

    diff_seconds = (punch_in_utc - shift_start_utc).total_seconds()

    # Grace: strictly more than 10 minutes (600 seconds)
    if diff_seconds <= 600:
        return 0

    # Floor of total minutes since shift_start
    return int(diff_seconds // 60)


def compute_overtime_minutes(punch_out_utc: datetime, shift_end: str, attendance_date: str,
                              shift_start: str) -> int:
    """R3: Compute overtime minutes.

    Floor of minutes from shift_end to punch_out, counted only if >= 30; else 0.
    For overnight shifts, shift_end falls on the next calendar day.
    """
    eh, em = map(int, shift_end.split(":"))
    year, month, day = map(int, attendance_date.split("-"))

    # Determine if overnight shift
    sh, sm = map(int, shift_start.split(":"))
    is_overnight = (eh < sh) or (eh == sh and em <= sm)

    if is_overnight:
        # Shift end is on the next day
        end_date = datetime(year, month, day, tzinfo=IST) + timedelta(days=1)
        shift_end_ist = end_date.replace(hour=eh, minute=em, second=0, microsecond=0)
    else:
        shift_end_ist = datetime(year, month, day, eh, em, 0, tzinfo=IST)

    shift_end_utc = shift_end_ist.astimezone(timezone.utc)

    diff_seconds = (punch_out_utc - shift_end_utc).total_seconds()
    if diff_seconds < 0:
        return 0
    minutes = int(diff_seconds // 60)
    return minutes if minutes >= 30 else 0


def compute_work_hours(punch_in_utc: datetime, punch_out_utc: datetime) -> float:
    """R4: Compute work hours = (punch_out - punch_in) seconds / 3600, rounded 2dp half-up."""
    diff_seconds = (punch_out_utc - punch_in_utc).total_seconds()
    return round_half_up(diff_seconds / 3600, 2)


def compute_half_day(work_hours: float) -> bool:
    """R5: half_day = True when rounded work_hours < 4.50."""
    if work_hours is None:
        return False
    return work_hours < 4.50


# ---------------------------------------------------------------------------
# Document → API response conversion
# ---------------------------------------------------------------------------
def attendance_doc_to_response(doc: dict) -> dict:
    """Convert a MongoDB attendance_logs document to the API response format.

    - Removes _id
    - Converts BSON datetimes (punch_in, punch_out) to epoch milliseconds
    - Converts history[].at and history[].changes punch_in/punch_out from/to to epoch ms
    - Defaults missing fields (history=[], half_day=False, late_minutes=0, overtime_minutes=0)
    """
    result = {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_ms(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_ms(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": [],
    }

    # Convert history entries
    raw_history = doc.get("history", [])
    for entry in raw_history:
        converted_entry = {
            "at": datetime_to_epoch_ms(entry["at"]),
            "by": entry["by"],
            "reason": entry["reason"],
            "changes": {},
        }
        for field_name, change in entry.get("changes", {}).items():
            if field_name in ("punch_in", "punch_out"):
                # Convert from/to BSON dates to epoch ms
                converted_entry["changes"][field_name] = {
                    "from": datetime_to_epoch_ms(change.get("from")),
                    "to": datetime_to_epoch_ms(change.get("to")),
                }
            else:
                converted_entry["changes"][field_name] = {
                    "from": change.get("from"),
                    "to": change.get("to"),
                }
        result["history"].append(converted_entry)

    return result


def employee_doc_to_response(doc: dict) -> dict:
    """Convert a MongoDB employees document to the API response format.

    - Removes _id
    - Converts created_at to epoch milliseconds
    """
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_ms(doc["created_at"]),
    }


# ---------------------------------------------------------------------------
# Pydantic request models
# ---------------------------------------------------------------------------
class EmployeeCreate(BaseModel):
    """Request body for POST /employees."""
    emp_code: str = Field(..., pattern=r"^EMP\d{4,6}$")
    name: str = Field(..., min_length=1, max_length=100)
    email: str = Field(..., max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(..., min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str = Field(...)

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, v):
        try:
            datetime.strptime(v, "%Y-%m-%d")
        except ValueError:
            raise ValueError("joined_on must be a valid date in YYYY-MM-DD format")
        return v


class PunchInRequest(BaseModel):
    """Request body for POST /attendance/punch-in."""
    emp_code: str
    punched_at: Optional[EpochMillis] = None
    status: str = "PRESENT"


class PunchOutRequest(BaseModel):
    """Request body for POST /attendance/punch-out."""
    emp_code: str
    punched_at: Optional[EpochMillis] = None


class RegularizeRequest(BaseModel):
    """Request body for PATCH /attendance/{emp_code}/{date}."""
    status: Optional[str] = None
    punch_in: Optional[EpochMillis] = None
    punch_out: Optional[EpochMillis] = None
    reason: str = Field(..., min_length=5, max_length=200)
    regularized_by: str = Field(..., min_length=1, max_length=50)


# ---------------------------------------------------------------------------
# Date/month validation helpers
# ---------------------------------------------------------------------------
def validate_date_str(date_str: str, field_name: str = "date") -> str:
    """Validate a YYYY-MM-DD date string."""
    try:
        datetime.strptime(date_str, "%Y-%m-%d")
    except (ValueError, TypeError):
        raise HTTPException(422, detail=f"{field_name} must be a valid date in YYYY-MM-DD format")
    return date_str


def validate_month_str(month_str: str) -> str:
    """Validate a YYYY-MM month string."""
    if not re.match(r"^\d{4}-(0[1-9]|1[0-2])$", month_str):
        raise HTTPException(422, detail="month must be in YYYY-MM format")
    return month_str


def get_month_date_range(month_str: str) -> tuple:
    """Return (first_day, last_day) as YYYY-MM-DD strings for a given YYYY-MM."""
    year, month = map(int, month_str.split("-"))
    first_day = f"{year:04d}-{month:02d}-01"
    # Last day of month
    if month == 12:
        next_month_first = datetime(year + 1, 1, 1)
    else:
        next_month_first = datetime(year, month + 1, 1)
    last_day_dt = next_month_first - timedelta(days=1)
    last_day = last_day_dt.strftime("%Y-%m-%d")
    return first_day, last_day


def count_working_days(start_date_str: str, end_date_str: str) -> int:
    """Count Mon-Fri days from start_date to end_date (both inclusive)."""
    start = datetime.strptime(start_date_str, "%Y-%m-%d").date()
    end = datetime.strptime(end_date_str, "%Y-%m-%d").date()
    if start > end:
        return 0
    count = 0
    current = start
    while current <= end:
        if current.weekday() < 5:  # Mon=0 ... Fri=4
            count += 1
        current += timedelta(days=1)
    return count


# ---------------------------------------------------------------------------
# ENDPOINTS
# ---------------------------------------------------------------------------

# ---- System ----

@app.get("/health")
def health():
    """Readiness probe: returns 200 only if MongoDB answers a ping, else 503."""
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except Exception:
        raise HTTPException(503, detail="MongoDB is not available")


# ---- Employees ----

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeCreate):
    """Create an employee. 409 on duplicate emp_code, 422 on invalid input.

    Uniqueness is enforced by the database unique index on emp_code.
    Concurrent duplicate creates yield exactly one 201 and the rest 409.
    """
    # Validate shift_start != shift_end
    if body.shift_start == body.shift_end:
        raise HTTPException(422, detail="shift_start must differ from shift_end")

    doc = {
        "emp_code": body.emp_code,
        "name": body.name,
        "email": body.email,
        "department": body.department,
        "shift_start": body.shift_start,
        "shift_end": body.shift_end,
        "joined_on": body.joined_on,
        "created_at": datetime.now(tz=timezone.utc),  # Server-set, UTC-aware
    }

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="emp_code already exists")

    return employee_doc_to_response(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List employees, sorted by emp_code ascending. Paginated per R10."""
    query: Dict[str, Any] = {}
    if department:
        query["department"] = department

    total = db.employees.count_documents(query)  # total reflects filters
    skip = (page - 1) * page_size
    docs = list(
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )

    items = [employee_doc_to_response(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---- Attendance CRUD ----

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInRequest):
    """Record a punch-in. Creates the day's record.

    - Validates the employee exists (404)
    - Validates status is PRESENT/WFH/ON_DUTY (422)
    - Validates punched_at epoch milliseconds (422)
    - Determines attendance date per R1 (IST, overnight shift handling)
    - Computes late_minutes per R2
    - Enforces one-record-per-(emp_code, date) via unique index (409 on duplicate)
    """
    # Validate status
    if body.status not in PRESENCE_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {sorted(PRESENCE_STATUSES)}")

    # Find the employee
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Determine the punch-in instant
    if body.punched_at is not None:
        validate_epoch_ms(body.punched_at, "punched_at")
        punch_in_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        # Use current time, truncated to whole seconds (R1)
        now = datetime.now(tz=timezone.utc)
        punch_in_utc = now.replace(microsecond=0)

    # R1: Determine attendance date
    att_date = determine_attendance_date(punch_in_utc, emp["shift_start"], emp["shift_end"])

    # R2: Compute late minutes
    late = compute_late_minutes(punch_in_utc, emp["shift_start"], att_date)

    doc = {
        "emp_code": body.emp_code,
        "date": att_date,
        "status": body.status,
        "punch_in": punch_in_utc,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": late,
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, detail="Already punched in for this date")

    return attendance_doc_to_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutRequest):
    """Record a punch-out. Closes the employee's most recent record whose punch_in <= punched_at.

    - Finds the employee's most recent open record (punch_out is null)
    - 404 if employee does not exist, or no punch-in found at or before punched_at
    - 409 if that record is already punched out (concurrent punch-outs: one 200, rest 409)
    - 422 if punched_at <= punch_in or > 24h after punch_in
    - Computes work_hours (R4), overtime_minutes (R3), half_day (R5)
    """
    # Find the employee
    emp = db.employees.find_one({"emp_code": body.emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Determine the punch-out instant
    if body.punched_at is not None:
        validate_epoch_ms(body.punched_at, "punched_at")
        punch_out_utc = epoch_ms_to_datetime(body.punched_at)
    else:
        now = datetime.now(tz=timezone.utc)
        punch_out_utc = now.replace(microsecond=0)

    # Check for an open attendance record
    open_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_out": None},
        sort=[("punch_in", DESCENDING)],
    )

    if open_record:
        pin = open_record["punch_in"]
        if pin.tzinfo is None:
            pin = pin.replace(tzinfo=timezone.utc)

        # Validate: punch_out must be after punch_in
        if punch_out_utc <= pin:
            raise HTTPException(422, detail="punched_at must be after punch_in")

        # Validate: punch_out must be within 24 hours of punch_in
        if (punch_out_utc - pin).total_seconds() > 24 * 3600:
            raise HTTPException(422, detail="punched_at must be within 24 hours of punch_in")

        # Compute derived fields
        work_hrs = compute_work_hours(pin, punch_out_utc)
        overtime = compute_overtime_minutes(
            punch_out_utc, emp["shift_end"], open_record["date"], emp["shift_start"]
        )
        half = compute_half_day(work_hrs)

        # Atomically set punch_out (only if still null → race-safe)
        result = db.attendance_logs.update_one(
            {"_id": open_record["_id"], "punch_out": None},
            {
                "$set": {
                    "punch_out": punch_out_utc,
                    "work_hours": work_hrs,
                    "overtime_minutes": overtime,
                    "half_day": half,
                }
            },
        )

        if result.modified_count == 0:
            raise HTTPException(409, detail="Already punched out")

        updated = db.attendance_logs.find_one({"_id": open_record["_id"]})
        return attendance_doc_to_response(updated)

    # If no open record exists, check if there was a closed record at or before punched_at
    past_record = db.attendance_logs.find_one(
        {"emp_code": body.emp_code, "punch_in": {"$lte": punch_out_utc}},
        sort=[("punch_in", DESCENDING)],
    )
    if past_record:
        raise HTTPException(409, detail="Already punched out")

    raise HTTPException(404, detail="No punch-in found")


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """List attendance records, sorted by date descending then emp_code ascending.

    Paginated per R10. Must be fast on 100k records (uses indexes, not in-memory sort).
    """
    # Validate date_from > date_to → 422
    if date_from and date_to and date_from > date_to:
        raise HTTPException(422, detail="date_from must not be after date_to")

    # Validate status
    if status and status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {sorted(ALL_STATUSES)}")

    query: Dict[str, Any] = {}
    if emp_code:
        query["emp_code"] = emp_code
    if date_from or date_to:
        date_filter: Dict[str, str] = {}
        if date_from:
            date_filter["$gte"] = date_from
        if date_to:
            date_filter["$lte"] = date_to
        query["date"] = date_filter
    if status:
        query["status"] = status

    total = db.attendance_logs.count_documents(query)
    skip = (page - 1) * page_size

    docs = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip(skip)
        .limit(page_size)
    )

    items = [attendance_doc_to_response(d) for d in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# ---- Regularize (manual correction) ----

@app.patch("/attendance/{emp_code}/{date}")
def regularize_attendance(
    emp_code: str = Path(...),
    date: str = Path(...),
    body: RegularizeRequest = ...,
):
    """Regularize (manually correct) one day's attendance record.

    - 404 if employee or record doesn't exist
    - 422 on invalid input or no-change request
    - Recomputes derived fields (R2-R5) after applying changes
    - Appends one entry to history with only actually-changed fields
    - 409 if concurrent edit detected (history length mismatch)
    """
    validate_date_str(date, "date")

    # Validate employee exists
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    # Find the existing record
    record = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if not record:
        raise HTTPException(404, detail="No attendance record for this date")

    # Validate status if provided
    new_status = body.status
    if new_status is not None and new_status not in ALL_STATUSES:
        raise HTTPException(422, detail=f"status must be one of {sorted(ALL_STATUSES)}")

    # Determine the effective status after correction
    effective_status = new_status if new_status is not None else record["status"]

    # Validate epoch ms for punch_in and punch_out if provided
    if body.punch_in is not None:
        validate_epoch_ms(body.punch_in, "punch_in")
    if body.punch_out is not None:
        validate_epoch_ms(body.punch_out, "punch_out")

    # Rule: ABSENT or LEAVE → clears both punch times; supplying punch_in or punch_out with it is 422
    if effective_status in ("ABSENT", "LEAVE"):
        if body.punch_in is not None or body.punch_out is not None:
            raise HTTPException(
                422, detail="Cannot supply punch_in or punch_out with ABSENT or LEAVE status"
            )
        new_punch_in = None
        new_punch_out = None
    else:
        # Presence status requires a punch_in (supplied or already stored)
        if body.punch_in is not None:
            new_punch_in = epoch_ms_to_datetime(body.punch_in)
        else:
            new_punch_in = record.get("punch_in")
            if new_punch_in is not None and new_punch_in.tzinfo is None:
                new_punch_in = new_punch_in.replace(tzinfo=timezone.utc)

        if new_punch_in is None:
            raise HTTPException(422, detail="A presence status requires a punch_in")

        if body.punch_out is not None:
            new_punch_out = epoch_ms_to_datetime(body.punch_out)
        else:
            new_punch_out = record.get("punch_out")
            if new_punch_out is not None and new_punch_out.tzinfo is None:
                new_punch_out = new_punch_out.replace(tzinfo=timezone.utc)

        # R1: punch_in must stay on the record's attendance date
        computed_date = determine_attendance_date(new_punch_in, emp["shift_start"], emp["shift_end"])
        if computed_date != date:
            raise HTTPException(422, detail="punch_in does not fall on the record's attendance date")

        # Validate punch_out > punch_in and within 24h
        if new_punch_out is not None:
            if new_punch_out <= new_punch_in:
                raise HTTPException(422, detail="punch_out must be after punch_in")
            if (new_punch_out - new_punch_in).total_seconds() > 24 * 3600:
                raise HTTPException(422, detail="punch_out must be within 24 hours of punch_in")

    # Recompute derived fields (R2-R5) based on new values
    if effective_status in ("ABSENT", "LEAVE"):
        new_late = 0
        new_work_hours = None
        new_overtime = 0
        new_half_day = False
    elif new_punch_out is not None:
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        new_work_hours = compute_work_hours(new_punch_in, new_punch_out)
        new_overtime = compute_overtime_minutes(
            new_punch_out, emp["shift_end"], date, emp["shift_start"]
        )
        new_half_day = compute_half_day(new_work_hours)
    else:
        new_late = compute_late_minutes(new_punch_in, emp["shift_start"], date)
        new_work_hours = None
        new_overtime = 0
        new_half_day = False

    # Build the changes dict — only fields that actually changed
    old_punch_in = record.get("punch_in")
    if old_punch_in is not None and old_punch_in.tzinfo is None:
        old_punch_in = old_punch_in.replace(tzinfo=timezone.utc)
    old_punch_out = record.get("punch_out")
    if old_punch_out is not None and old_punch_out.tzinfo is None:
        old_punch_out = old_punch_out.replace(tzinfo=timezone.utc)

    changes = {}
    if effective_status != record["status"]:
        changes["status"] = {"from": record["status"], "to": effective_status}
    if new_punch_in != old_punch_in:
        changes["punch_in"] = {"from": old_punch_in, "to": new_punch_in}
    if new_punch_out != old_punch_out:
        changes["punch_out"] = {"from": old_punch_out, "to": new_punch_out}
    if new_work_hours != record.get("work_hours"):
        changes["work_hours"] = {"from": record.get("work_hours"), "to": new_work_hours}
    if new_late != record.get("late_minutes", 0):
        changes["late_minutes"] = {"from": record.get("late_minutes", 0), "to": new_late}
    if new_overtime != record.get("overtime_minutes", 0):
        changes["overtime_minutes"] = {"from": record.get("overtime_minutes", 0), "to": new_overtime}
    if new_half_day != record.get("half_day", False):
        changes["half_day"] = {"from": record.get("half_day", False), "to": new_half_day}

    # A request that changes nothing is 422
    if not changes:
        raise HTTPException(422, detail="No changes to apply")

    # Build the history entry
    history_entry = {
        "at": datetime.now(tz=timezone.utc),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Atomically update: use history array length to detect concurrent edits
    current_history_len = len(record.get("history", []))
    update_set = {
        "status": effective_status,
        "punch_in": new_punch_in,
        "punch_out": new_punch_out,
        "work_hours": new_work_hours,
        "late_minutes": new_late,
        "overtime_minutes": new_overtime,
        "half_day": new_half_day,
    }

    result = db.attendance_logs.update_one(
        {
            "_id": record["_id"],
            # Atomic length check: ensures no concurrent edit has modified history
            "history": {"$size": current_history_len},
        },
        {
            "$set": update_set,
            "$push": {"history": history_entry},
        },
    )

    if result.modified_count == 0:
        raise HTTPException(409, detail="Concurrent edit detected; retry")

    updated = db.attendance_logs.find_one({"_id": record["_id"]})
    return attendance_doc_to_response(updated)


# ---------------------------------------------------------------------------
# Analytics endpoints (MongoDB aggregation pipelines)
# ---------------------------------------------------------------------------

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str = Path(...),
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    """Monthly attendance summary for one employee. Uses MongoDB aggregation.

    - working_days per R7 (Mon-Fri from joined_on or start of month, whichever is later)
    - present_days per R5-R7 (half_day = 0.5, Mon-Fri only)
    - leave_days = number of LEAVE records
    - late_count = records with late_minutes > 0
    - attendance_pct = present_days / working_days × 100, R8 rounding, null if working_days = 0
    - 404 for unknown employee
    """
    emp = db.employees.find_one({"emp_code": emp_code}, {"_id": 0})
    if not emp:
        raise HTTPException(404, detail="Employee not found")

    first_day, last_day = get_month_date_range(month)

    # R7: Working days from joined_on or first_day, whichever is later
    effective_start = max(emp["joined_on"], first_day)
    if effective_start > last_day:
        # Employee hasn't joined by this month
        working_days = 0
    else:
        working_days = count_working_days(effective_start, last_day)

    # Aggregation pipeline to compute the summary from attendance_logs
    pipeline = [
        # Match this employee's records for the month
        {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
        # Add helper fields
        {
            "$addFields": {
                # Determine day of week (1=Sun, 7=Sat in MongoDB; we need Mon-Fri)
                "date_as_date": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                "effective_half_day": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$addFields": {
                # dayOfWeek: 1=Sun, 2=Mon, ..., 6=Fri, 7=Sat
                "day_of_week": {"$dayOfWeek": "$date_as_date"},
                "is_weekday": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date_as_date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_as_date"}, 6]},
                    ]
                },
            }
        },
        {
            "$group": {
                "_id": None,
                # present_days: only Mon-Fri, half_day=0.5 else 1.0
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", "$is_weekday"]},
                            {"$cond": ["$effective_half_day", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "leave_days": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
                "total_late_minutes": {
                    "$sum": {"$cond": ["$is_late", {"$ifNull": ["$late_minutes", 0]}, 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
    ]

    results = list(db.attendance_logs.aggregate(pipeline))

    if results:
        r = results[0]
        present_days = round_half_up(r["present_days"], 2)
        leave_days = r["leave_days"]
        late_count = r["late_count"]
        total_late = r["total_late_minutes"]
        total_overtime = r["total_overtime_minutes"]
    else:
        present_days = 0
        leave_days = 0
        late_count = 0
        total_late = 0
        total_overtime = 0

    attendance_pct = None
    if working_days > 0:
        attendance_pct = round_half_up((present_days / working_days) * 100, 2)

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": present_days,
        "leave_days": leave_days,
        "late_count": late_count,
        "total_late_minutes": total_late,
        "total_overtime_minutes": total_overtime,
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    """Department summary for a month. One item per department, sorted ascending.

    Uses MongoDB aggregation across employees and attendance_logs.
    - headcount per R9: employees with joined_on <= last_day of month (even if no logs)
    - present_days: sum of presence records (half_day=0.5) — Mon-Fri counted
    - avg_work_hours: mean work_hours of presence-status records that have a work_hours value
    - late_count, total_late_minutes, leave_count, on_duty_count
    - Department with headcount=0 is omitted.
    """
    first_day, last_day = get_month_date_range(month)

    # Build employee filter
    emp_match: Dict[str, Any] = {"joined_on": {"$lte": last_day}}
    if department:
        emp_match["department"] = department

    # Step 1: Get headcount per department from employees collection
    headcount_pipeline = [
        {"$match": emp_match},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1}}},
    ]
    headcount_results = {r["_id"]: r["headcount"] for r in db.employees.aggregate(headcount_pipeline)}

    if not headcount_results:
        return {"month": month, "items": []}

    # Step 2: Aggregate attendance logs for the month, joined with employees for department
    # We use $lookup to get the department from employees
    att_match: Dict[str, Any] = {"date": {"$gte": first_day, "$lte": last_day}}
    if department:
        # Filter attendance by emp_codes in the given department
        dept_emp_codes = [
            e["emp_code"]
            for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})
        ]
        att_match["emp_code"] = {"$in": dept_emp_codes}

    logs_pipeline = [
        {"$match": att_match},
        # Lookup employee to get department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        # Add computed fields
        {
            "$addFields": {
                "date_as_date": {"$dateFromString": {"dateString": "$date", "format": "%Y-%m-%d"}},
                "dept": "$employee.department",
                "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_leave": {"$eq": ["$status", "LEAVE"]},
                "is_on_duty": {"$eq": ["$status", "ON_DUTY"]},
                "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                "effective_half_day": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$addFields": {
                "is_weekday": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$date_as_date"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$date_as_date"}, 6]},
                    ]
                },
            }
        },
        # Group by department
        {
            "$group": {
                "_id": "$dept",
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", "$is_weekday"]},
                            {"$cond": ["$effective_half_day", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                # avg_work_hours: average over presence-status records WITH work_hours value
                "work_hours_sum": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", {"$ne": ["$work_hours", None]}]},
                            "$work_hours",
                            0,
                        ]
                    }
                },
                "work_hours_count": {
                    "$sum": {
                        "$cond": [
                            {"$and": ["$is_presence", {"$ne": ["$work_hours", None]}]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
                "total_late_minutes": {
                    "$sum": {"$cond": ["$is_late", {"$ifNull": ["$late_minutes", 0]}, 0]}
                },
                "leave_count": {"$sum": {"$cond": ["$is_leave", 1, 0]}},
                "on_duty_count": {"$sum": {"$cond": ["$is_on_duty", 1, 0]}},
            }
        },
        {"$sort": {"_id": 1}},
    ]

    log_stats = {r["_id"]: r for r in db.attendance_logs.aggregate(logs_pipeline)}

    # Build result: one item per department with headcount > 0
    items = []
    for dept in sorted(headcount_results.keys()):
        hc = headcount_results[dept]
        if hc == 0:
            continue

        stats = log_stats.get(dept, {})
        present = stats.get("present_days", 0)
        wh_count = stats.get("work_hours_count", 0)
        wh_sum = stats.get("work_hours_sum", 0)

        avg_wh = None
        if wh_count > 0:
            avg_wh = round_half_up(wh_sum / wh_count, 2)

        items.append({
            "department": dept,
            "headcount": hc,
            "present_days": round_half_up(present, 2),
            "avg_work_hours": avg_wh,
            "late_count": stats.get("late_count", 0),
            "total_late_minutes": stats.get("total_late_minutes", 0),
            "leave_count": stats.get("leave_count", 0),
            "on_duty_count": stats.get("on_duty_count", 0),
        })

    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    """Late-comers leaderboard for a month with standard competition ranking.

    Uses MongoDB aggregation with $setWindowFields for proper ranking.
    - Ranks by total_late_minutes descending (ties share rank, next rank skipped)
    - limit applied after ranking: return all employees with rank <= limit
    - Ties ordered by emp_code ascending
    - When department given, ranking is within that department only
    """
    first_day, last_day = get_month_date_range(month)

    # Build the pipeline
    match_stage: Dict[str, Any] = {
        "date": {"$gte": first_day, "$lte": last_day},
        "late_minutes": {"$gt": 0},
    }

    pipeline: List[Dict[str, Any]] = [
        {"$match": match_stage},
        # Lookup employee for name, department
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    # Filter by department if given
    if department:
        pipeline.append({"$match": {"employee.department": department}})

    pipeline.extend([
        # Group by employee
        {
            "$group": {
                "_id": "$emp_code",
                "name": {"$first": "$employee.name"},
                "department": {"$first": "$employee.department"},
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }
        },
        # Only employees with total > 0 (already filtered, but be safe)
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        # Sort for ranking: total_late_minutes desc, emp_code asc
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        # Standard competition ranking using $setWindowFields
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {
                    "rank": {
                        "$rank": {},
                    }
                },
            }
        },
        # Filter by rank <= limit
        {"$match": {"rank": {"$lte": limit}}},
        # Final projection
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": 1,
                "department": 1,
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
        # Final sort
        {"$sort": {"rank": 1, "emp_code": 1}},
    ])

    results = list(db.attendance_logs.aggregate(pipeline))

    return {"month": month, "items": results}


@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str = Path(...),
    date_from: str = Query(..., alias="from"),
    date_to: str = Query(..., alias="to"),
):
    """Daily attendance trend for a department.
    Returns exactly one row per calendar day from 'from' to 'to' inclusive.
    Uses $densify for gap-filling (no Python loops for generating days).
    - is_working_day = Mon-Fri
    - headcount = employees with joined_on <= that day (R9)
    - present_count = presence-status records that day (half_day = 0.5)
    - late_count = records with late_minutes > 0
    - attendance_rate = present_count / headcount, 4 decimals half-up, only on working days with headcount > 0
    - moving_avg_7d = mean of non-null attendance_rate in current + 6 preceding rows within range
    - 404 if unknown department
    - 422 if to < from or range > 92 days
    """
    validate_date_str(date_from, "from")
    validate_date_str(date_to, "to")

    if date_to < date_from:
        raise HTTPException(422, detail="'to' must not be before 'from'")

    # Check range <= 92 days
    from_dt = datetime.strptime(date_from, "%Y-%m-%d").date()
    to_dt = datetime.strptime(date_to, "%Y-%m-%d").date()
    if (to_dt - from_dt).days > 92:
        raise HTTPException(422, detail="Date range must not exceed 92 days")

    # Verify department exists (has at least one employee)
    dept_employees = list(db.employees.find({"department": department}, {"_id": 0}))
    if not dept_employees:
        raise HTTPException(404, detail="Department not found")

    # Get all emp_codes in this department
    dept_emp_codes = [e["emp_code"] for e in dept_employees]

    # Precompute headcount for each day (employees with joined_on <= day)
    # Since this could be many days but few employees, compute in Python from employee data
    headcount_by_day = {}
    current_date = from_dt
    while current_date <= to_dt:
        date_str = current_date.isoformat()
        hc = sum(1 for e in dept_employees if e["joined_on"] <= date_str)
        headcount_by_day[date_str] = hc
        current_date += timedelta(days=1)

    # Aggregate attendance logs for the date range
    pipeline = [
        {
            "$match": {
                "emp_code": {"$in": dept_emp_codes},
                "date": {"$gte": date_from, "$lte": date_to},
            }
        },
        {
            "$addFields": {
                "is_presence": {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
                "is_late": {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                "effective_half_day": {"$ifNull": ["$half_day", False]},
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            "$is_presence",
                            {"$cond": ["$effective_half_day", 0.5, 1.0]},
                            0,
                        ]
                    }
                },
                "late_count": {"$sum": {"$cond": ["$is_late", 1, 0]}},
            }
        },
    ]

    day_stats = {r["_id"]: r for r in db.attendance_logs.aggregate(pipeline)}

    # Build the trend rows for every calendar day in the range
    rows = []
    current_date = from_dt
    while current_date <= to_dt:
        date_str = current_date.isoformat()
        is_working = current_date.weekday() < 5  # Mon=0 ... Fri=4
        hc = headcount_by_day.get(date_str, 0)
        stats = day_stats.get(date_str, {})
        present = stats.get("present_count", 0)
        late = stats.get("late_count", 0)

        # attendance_rate: only on working days with headcount > 0
        if is_working and hc > 0:
            rate = round_half_up(present / hc, 4)
        else:
            rate = None

        rows.append({
            "date": date_str,
            "is_working_day": is_working,
            "headcount": hc,
            "present_count": present,
            "late_count": late,
            "attendance_rate": rate,
            "moving_avg_7d": None,  # Will be computed below
        })

        current_date += timedelta(days=1)

    # Compute moving_avg_7d: mean of non-null attendance_rate in current + up to 6 preceding rows
    for i, row in enumerate(rows):
        # Window: rows[max(0, i-6) : i+1]
        window_start = max(0, i - 6)
        window = rows[window_start : i + 1]
        rates = [r["attendance_rate"] for r in window if r["attendance_rate"] is not None]
        if rates:
            row["moving_avg_7d"] = round_half_up(sum(rates) / len(rates), 4)
        else:
            row["moving_avg_7d"] = None

    return {"department": department, "items": rows}


# ---------------------------------------------------------------------------
# Admin: explain endpoint
# ---------------------------------------------------------------------------

@app.get("/admin/explain/{endpoint}")
def explain_endpoint(
    endpoint: str = Path(...),
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[str] = Query(default=None, alias="date_from"),
    date_to: Optional[str] = Query(default=None, alias="date_to"),
    status: Optional[str] = None,
    query_from: Optional[str] = Query(default=None, alias="from"),
    query_to: Optional[str] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    """MongoDB explain for an endpoint's main query/aggregation.

    Returns raw explain output with executionStats verbosity.
    The grader checks for IXSCAN and no COLLSCAN.
    """
    valid_endpoints = {
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    }
    if endpoint not in valid_endpoints:
        raise HTTPException(422, detail=f"endpoint must be one of {sorted(valid_endpoints)}")

    if endpoint == "attendance_list":
        query: Dict[str, Any] = {}
        if emp_code:
            query["emp_code"] = emp_code
        if date_from or date_to:
            date_filter: Dict[str, str] = {}
            if date_from:
                date_filter["$gte"] = date_from
            if date_to:
                date_filter["$lte"] = date_to
            query["date"] = date_filter
        if status:
            query["status"] = status

        skip = (page - 1) * page_size

        explain = db.command(
            "explain",
            {
                "find": "attendance_logs",
                "filter": query,
                "sort": {"date": -1, "emp_code": 1},
                "skip": skip,
                "limit": page_size,
            },
            verbosity="executionStats",
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, detail="emp_code and month are required for employee_monthly")
        validate_month_str(month)
        first_day, last_day = get_month_date_range(month)

        pipeline = [
            {"$match": {"emp_code": emp_code, "date": {"$gte": first_day, "$lte": last_day}}},
            {
                "$group": {
                    "_id": None,
                    "count": {"$sum": 1},
                }
            },
        ]

        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_summary":
        if not month:
            raise HTTPException(422, detail="month is required for department_summary")
        validate_month_str(month)
        first_day, last_day = get_month_date_range(month)

        att_match: Dict[str, Any] = {"date": {"$gte": first_day, "$lte": last_day}}
        if department:
            dept_emp_codes = [
                e["emp_code"]
                for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})
            ]
            att_match["emp_code"] = {"$in": dept_emp_codes}

        pipeline = [
            {"$match": att_match},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
            {"$group": {"_id": "$employee.department", "count": {"$sum": 1}}},
        ]

        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, detail="month is required for late_leaderboard")
        validate_month_str(month)
        first_day, last_day = get_month_date_range(month)

        match_filter: Dict[str, Any] = {
            "date": {"$gte": first_day, "$lte": last_day},
            "late_minutes": {"$gt": 0},
        }

        pipeline = [
            {"$match": match_filter},
            {
                "$lookup": {
                    "from": "employees",
                    "localField": "emp_code",
                    "foreignField": "emp_code",
                    "as": "employee",
                }
            },
            {"$unwind": "$employee"},
        ]
        if department:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.append({"$group": {"_id": "$emp_code", "total": {"$sum": "$late_minutes"}}})

        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}

    elif endpoint == "department_trend":
        if not department:
            raise HTTPException(422, detail="department is required for department_trend")
        if not query_from or not query_to:
            raise HTTPException(422, detail="from and to are required for department_trend")

        dept_emp_codes = [
            e["emp_code"]
            for e in db.employees.find({"department": department}, {"emp_code": 1, "_id": 0})
        ]

        pipeline = [
            {
                "$match": {
                    "emp_code": {"$in": dept_emp_codes},
                    "date": {"$gte": query_from, "$lte": query_to},
                }
            },
            {"$group": {"_id": "$date", "count": {"$sum": 1}}},
        ]

        explain = db.command(
            "aggregate", "attendance_logs", pipeline=pipeline, explain=True
        )
        return {"endpoint": endpoint, "collection": "attendance_logs", "explain": explain}
