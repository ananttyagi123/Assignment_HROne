"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Literal, Optional

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field, StrictInt, field_validator, model_validator
from pymongo import MongoClient
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.environ["MONGO_URI"], tz_aware=True)
db = client[os.environ["MONGO_DB"]]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
    )
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
    lifespan=lifespan,
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def truncate_to_seconds(instant: datetime) -> datetime:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must be timezone-aware")
    return instant.replace(microsecond=0)


def milliseconds_to_datetime(milliseconds: int) -> datetime:
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    return (epoch + timedelta(milliseconds=milliseconds)).replace(microsecond=0)


def datetime_to_milliseconds(instant: datetime) -> int:
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise ValueError("instant must be timezone-aware")
    instant = instant.astimezone(UTC)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    delta = instant - epoch
    return (delta.days * 86400 + delta.seconds) * 1000 + delta.microseconds // 1000


def shift_instant(date_str: str, shift_time: str) -> datetime:
    return datetime.combine(
        date.fromisoformat(date_str),
        time.fromisoformat(shift_time),
        tzinfo=IST,
    )


def attendance_date_for_punch(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> str:
    local_punch = truncate_to_seconds(punch_in).astimezone(IST)
    overnight = shift_end <= shift_start
    if overnight and local_punch.timetz().replace(tzinfo=None) < time.fromisoformat(shift_end):
        return (local_punch.date() - timedelta(days=1)).isoformat()
    return local_punch.date().isoformat()


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: str,
) -> int:
    punch_in = truncate_to_seconds(punch_in).astimezone(IST)
    attendance_date = attendance_date_for_punch(punch_in, shift_start, shift_end)
    start = shift_instant(attendance_date, shift_start)
    elapsed_seconds = int((punch_in - start).total_seconds())
    if elapsed_seconds <= 600:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> Decimal:
    punch_in = truncate_to_seconds(punch_in)
    punch_out = truncate_to_seconds(punch_out)
    elapsed = punch_out - punch_in
    elapsed_seconds = elapsed.days * 86400 + elapsed.seconds
    return (Decimal(elapsed_seconds) / Decimal(3600)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )


def compute_overtime(
    punch_out: datetime,
    shift_start: str,
    shift_end: str,
    date_str: str,
) -> int:
    punch_out = truncate_to_seconds(punch_out).astimezone(IST)
    end_date = date.fromisoformat(date_str)
    if shift_end <= shift_start:
        end_date += timedelta(days=1)
    shift_end_instant = shift_instant(end_date.isoformat(), shift_end)
    elapsed_seconds = int((punch_out - shift_end_instant).total_seconds())
    overtime_minutes = elapsed_seconds // 60
    return overtime_minutes if overtime_minutes >= 30 else 0


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_shift_times(self) -> "EmployeeIn":
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start and shift_end must differ")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


def attendance_response(document: dict) -> dict:
    history = []
    for entry in document.get("history", []):
        response_entry = dict(entry)
        at = response_entry.get("at")
        if isinstance(at, datetime):
            response_entry["at"] = datetime_to_milliseconds(at)
        changes = response_entry.get("changes", {})
        response_changes = {}
        for field, change in changes.items():
            response_change = dict(change)
            if field in {"punch_in", "punch_out"}:
                for direction in ("from", "to"):
                    value = response_change.get(direction)
                    if isinstance(value, datetime):
                        response_change[direction] = datetime_to_milliseconds(
                            truncate_to_seconds(value)
                        )
            response_changes[field] = response_change
        response_entry["changes"] = response_changes
        history.append(response_entry)

    punch_in = document.get("punch_in")
    punch_out = document.get("punch_out")
    return {
        "emp_code": document["emp_code"],
        "date": document["date"],
        "status": document["status"],
        "punch_in": (
            datetime_to_milliseconds(truncate_to_seconds(punch_in))
            if punch_in is not None
            else None
        ),
        "punch_out": (
            datetime_to_milliseconds(truncate_to_seconds(punch_out))
            if punch_out is not None
            else None
        ),
        "work_hours": document.get("work_hours"),
        "late_minutes": document.get("late_minutes", 0),
        "overtime_minutes": document.get("overtime_minutes", 0),
        "half_day": document.get("half_day", False),
        "history": history,
    }


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "emp_code already exists") from exc
    doc.pop("_id", None)
    return {
        **doc,
        "created_at": datetime_to_milliseconds(doc["created_at"]),
    }


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    q = {}
    if department is not None:
        q["department"] = department
    skip = (page - 1) * page_size
    total = db.employees.count_documents(q)
    items = list(
        db.employees.find(q, {"_id": 0})
        .sort("emp_code", 1)
        .skip(skip)
        .limit(page_size)
    )
    for employee in items:
        employee["created_at"] = datetime_to_milliseconds(employee["created_at"])
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(404, "employee not found")
    ts = (
        milliseconds_to_datetime(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )
    d = attendance_date_for_punch(ts, emp["shift_start"], emp["shift_end"])
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts,
            emp["shift_start"],
            emp["shift_end"],
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(409, "already punched in for this date") from exc
    return attendance_response(doc)


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(422, "date_from must be on or before date_to")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if date_from or date_to:
        q["date"] = {}
        if date_from:
            q["date"]["$gte"] = date_from.isoformat()
        if date_to:
            q["date"]["$lte"] = date_to.isoformat()
    if status:
        q["status"] = status
    total = db.attendance_logs.count_documents(q)
    docs = list(
        db.attendance_logs.find(q, {"_id": 0})
        .sort([("date", -1), ("emp_code", 1)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [attendance_response(document) for document in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


# --------------------------------------------------------------------------- #
# TODO - the rest of the contract (see openapi.yaml):
#   POST  /attendance/punch-out
#   PATCH /attendance/{emp_code}/{date}
#   GET   /analytics/employees/{emp_code}/monthly
#   GET   /analytics/departments/summary
#   GET   /analytics/leaderboard/late
#   GET   /analytics/departments/{department}/trend
#   GET   /admin/explain/{endpoint}
# --------------------------------------------------------------------------- #
