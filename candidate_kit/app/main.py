"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import re
from calendar import monthrange
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Literal, NoReturn, Optional

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
PRESENCE_STATUSES = ["PRESENT", "WFH", "ON_DUTY"]
ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


@asynccontextmanager
async def lifespan(_: FastAPI):
    db.employees.create_index("emp_code", unique=True)
    db.employees.create_index([("joined_on", 1), ("department", 1)])
    db.employees.create_index([("department", 1), ("joined_on", 1)])
    db.attendance_logs.create_index(
        [("emp_code", 1), ("date", 1)],
        unique=True,
    )
    db.attendance_logs.create_index([("date", -1), ("emp_code", 1)])
    db.attendance_logs.create_index([("emp_code", 1), ("punch_in", -1)])
    db.attendance_logs.create_index([("status", 1), ("date", -1), ("emp_code", 1)])
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


def raise_validation_error(message: str, location: tuple[str, ...]) -> NoReturn:
    raise HTTPException(
        422,
        [{"loc": list(location), "msg": message, "type": "value_error"}],
    )


def parse_month(month: str) -> tuple[date, date]:
    if re.fullmatch(r"\d{4}-(0[1-9]|1[0-2])", month) is None:
        raise_validation_error("month must use YYYY-MM format", ("query", "month"))
    year, month_number = map(int, month.split("-"))
    try:
        first_day = date(year, month_number, 1)
        last_day = date(year, month_number, monthrange(year, month_number)[1])
    except ValueError:
        raise_validation_error(
            "month must be a valid calendar month",
            ("query", "month"),
        )
    return first_day, last_day


def round_half_up(value: int | float | Decimal, places: int = 2) -> float:
    quantum = Decimal(1).scaleb(-places)
    return float(Decimal(str(value)).quantize(quantum, rounding=ROUND_HALF_UP))


def count_working_days(start: date, end: date) -> int:
    if start > end:
        return 0
    days = (end - start).days + 1
    full_weeks, remainder = divmod(days, 7)
    working_days = full_weeks * 5
    for offset in range(remainder):
        if (start.weekday() + offset) % 7 < 5:
            working_days += 1
    return working_days


def build_attendance_list_query(
    emp_code: Optional[str],
    date_from: Optional[date],
    date_to: Optional[date],
    status: Optional[str],
) -> dict:
    if date_from is not None and date_to is not None and date_from > date_to:
        raise_validation_error(
            "date_from must be on or before date_to",
            ("query", "date_from"),
        )
    query = {}
    if emp_code is not None:
        query["emp_code"] = emp_code
    if date_from is not None or date_to is not None:
        query["date"] = {}
        if date_from is not None:
            query["date"]["$gte"] = date_from.isoformat()
        if date_to is not None:
            query["date"]["$lte"] = date_to.isoformat()
    if status is not None:
        query["status"] = status
    return query


def build_employee_monthly_pipeline(
    emp_code: str,
    first_day: date,
    last_day: date,
) -> list[dict]:
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": first_day.isoformat(),
                    "$lte": last_day.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "_is_weekday": {
                    "$in": [
                        {
                            "$dayOfWeek": {
                                "$dateFromString": {"dateString": "$date"}
                            }
                        },
                        [2, 3, 4, 5, 6],
                    ]
                }
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": {
                        "$cond": [
                            {
                                "$and": [
                                    {"$in": ["$status", PRESENCE_STATUSES]},
                                    "$_is_weekday",
                                ]
                            },
                            {"$cond": ["$half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "leave_days": {
                    "$sum": {"$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "total_overtime_minutes": {
                    "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                },
            }
        },
        {"$project": {"_id": 0}},
    ]


def build_department_summary_pipeline(
    first_day: date,
    last_day: date,
    department: Optional[str],
) -> list[dict]:
    employee_match = {"joined_on": {"$lte": last_day.isoformat()}}
    if department is not None:
        employee_match["department"] = department
    lookup_match = {
        "$match": {
            "$expr": {
                "$and": [
                    {"$eq": ["$emp_code", "$$employee_code"]},
                    {"$gte": ["$date", first_day.isoformat()]},
                    {"$lte": ["$date", last_day.isoformat()]},
                ]
            }
        }
    }
    return [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    lookup_match,
                    {
                        "$addFields": {
                            "_is_weekday": {
                                "$in": [
                                    {
                                        "$dayOfWeek": {
                                            "$dateFromString": {
                                                "dateString": "$date"
                                            }
                                        }
                                    },
                                    [2, 3, 4, 5, 6],
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        PRESENCE_STATUSES,
                                                    ]
                                                },
                                                "$_is_weekday",
                                            ]
                                        },
                                        {"$cond": ["$half_day", 0.5, 1]},
                                        0,
                                    ]
                                }
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                {"$ifNull": ["$late_minutes", 0]},
                                                0,
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "LEAVE"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {"$eq": ["$status", "ON_DUTY"]},
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        PRESENCE_STATUSES,
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        "$work_hours",
                                        0,
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$and": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        PRESENCE_STATUSES,
                                                    ]
                                                },
                                                {"$ne": ["$work_hours", None]},
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                    {"$project": {"_id": 0}},
                ],
                "as": "monthly",
            }
        },
        {
            "$addFields": {
                "_monthly": {
                    "$ifNull": [{"$arrayElemAt": ["$monthly", 0]}, {}]
                }
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {"$ifNull": ["$_monthly.present_days", 0]}
                },
                "late_count": {"$sum": {"$ifNull": ["$_monthly.late_count", 0]}},
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$_monthly.total_late_minutes", 0]}
                },
                "leave_count": {"$sum": {"$ifNull": ["$_monthly.leave_count", 0]}},
                "on_duty_count": {
                    "$sum": {"$ifNull": ["$_monthly.on_duty_count", 0]}
                },
                "work_hours_sum": {
                    "$sum": {"$ifNull": ["$_monthly.work_hours_sum", 0]}
                },
                "work_hours_count": {
                    "$sum": {"$ifNull": ["$_monthly.work_hours_count", 0]}
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$gt": ["$work_hours_count", 0]},
                        {"$divide": ["$work_hours_sum", "$work_hours_count"]},
                        None,
                    ]
                },
            }
        },
        {"$sort": {"department": 1}},
    ]


def build_late_leaderboard_pipeline(
    first_day: date,
    last_day: date,
    department: Optional[str],
    limit: int,
) -> list[dict]:
    lookup_pipeline = [
        {
            "$match": {
                "$expr": {"$eq": ["$emp_code", "$$employee_code"]}
            }
        }
    ]
    if department is not None:
        lookup_pipeline.append({"$match": {"department": department}})
    lookup_pipeline.append(
        {"$project": {"_id": 0, "name": 1, "department": 1}}
    )
    return [
        {
            "$match": {
                "date": {
                    "$gte": first_day.isoformat(),
                    "$lte": last_day.isoformat(),
                }
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$late_minutes", 0]}
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {
            "$lookup": {
                "from": "employees",
                "let": {"employee_code": "$_id"},
                "pipeline": lookup_pipeline,
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {
            "$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }
        },
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {
            "$project": {
                "_id": 0,
                "rank": 1,
                "emp_code": "$_id",
                "name": "$employee.name",
                "department": "$employee.department",
                "total_late_minutes": 1,
                "late_count": 1,
            }
        },
    ]


def build_department_trend_pipeline(
    emp_codes: list[str],
    joined_on_dates: list[str],
    first_day: date,
    last_day: date,
) -> list[dict]:
    start_instant = datetime.combine(first_day, time.min, tzinfo=UTC)
    end_instant = datetime.combine(last_day, time.min, tzinfo=UTC)
    number_of_days = (last_day - first_day).days + 1
    return [
        {
            "$match": {
                "emp_code": {"$in": emp_codes},
                "date": {
                    "$gte": first_day.isoformat(),
                    "$lte": last_day.isoformat(),
                },
            }
        },
        {
            "$addFields": {
                "_calendar_day": {
                    "$dateFromString": {"dateString": "$date"}
                }
            }
        },
        {
            "$group": {
                "_id": "$_calendar_day",
                "present_count": {
                    "$sum": {
                        "$cond": [
                            {"$in": ["$status", PRESENCE_STATUSES]},
                            {"$cond": ["$half_day", 0.5, 1]},
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]},
                            1,
                            0,
                        ]
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_id",
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$unionWith": {
                "coll": "employees",
                "pipeline": [
                    {"$match": {"emp_code": emp_codes[0]}},
                    {"$limit": 1},
                    {
                        "$project": {
                            "_id": 0,
                            "_day_offsets": {"$range": [0, number_of_days]},
                        }
                    },
                    {"$unwind": "$_day_offsets"},
                    {
                        "$project": {
                            "_id": 0,
                            "date": {
                                "$dateAdd": {
                                    "startDate": start_instant,
                                    "unit": "day",
                                    "amount": "$_day_offsets",
                                }
                            },
                            "present_count": {"$literal": 0},
                            "late_count": {"$literal": 0},
                        }
                    },
                ]
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {"$sum": "$present_count"},
                "late_count": {"$sum": "$late_count"},
            }
        },
        {"$project": {"_id": 0, "date": "$_id", "present_count": 1, "late_count": 1}},
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [start_instant, end_instant],
                },
            }
        },
        {
            "$fill": {
                "sortBy": {"date": 1},
                "output": {
                    "present_count": {"value": 0},
                    "late_count": {"value": 0},
                },
            }
        },
        {
            "$addFields": {
                "_date_string": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {
                    "$in": [{"$dayOfWeek": "$date"}, [2, 3, 4, 5, 6]]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": {"$literal": [{"joined_on": value} for value in joined_on_dates]},
                            "as": "employee",
                            "cond": {
                                "$lte": ["$$employee.joined_on", "$_date_string"]
                            },
                        }
                    }
                },
            }
        },
        {
            "$addFields": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {"$divide": ["$present_count", "$headcount"]},
                        None,
                    ]
                }
            }
        },
        {
            "$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {
                    "moving_avg_7d": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$_date_string",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
    ]


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


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )


class RegularizeIn(BaseModel):
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None
    punch_in: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )
    punch_out: Optional[StrictInt] = Field(
        default=None,
        ge=100000000000,
        le=4102444800000,
    )
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)

    @model_validator(mode="after")
    def validate_supplied_times(self) -> "RegularizeIn":
        if "status" in self.model_fields_set and self.status is None:
            raise ValueError("status must not be null")
        for field_name in ("punch_in", "punch_out"):
            if field_name in self.model_fields_set and getattr(self, field_name) is None:
                raise ValueError(f"{field_name} must be an epoch-millisecond integer")
        return self


class APIErrorResponse(BaseModel):
    detail: str


class APIValidationErrorResponse(BaseModel):
    detail: list[dict[str, Any]]


def response_docs(*status_codes: int) -> dict:
    return {
        status_code: {
            "model": (
                APIValidationErrorResponse
                if status_code == 422
                else APIErrorResponse
            ),
            "description": (
                "Validation error" if status_code == 422 else "Error"
            ),
        }
        for status_code in status_codes
    }


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
@app.get("/health", responses=response_docs(503))
def health():
    try:
        client.admin.command("ping")
    except PyMongoError as exc:
        raise HTTPException(503, "MongoDB is unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201, responses=response_docs(409, 422))
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


@app.get("/employees", responses=response_docs(422))
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


@app.post(
    "/attendance/punch-in",
    status_code=201,
    responses=response_docs(404, 409, 422),
)
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


@app.get("/attendance", responses=response_docs(422))
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = build_attendance_list_query(emp_code, date_from, date_to, status)
    total = db.attendance_logs.count_documents(query)
    docs = list(
        db.attendance_logs.find(query, {"_id": 0})
        .sort(ATTENDANCE_SORT)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    items = [attendance_response(document) for document in docs]
    return {"items": items, "total": total, "page": page, "page_size": page_size}


@app.post(
    "/attendance/punch-out",
    responses=response_docs(404, 409, 422),
)
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    punched_at = (
        milliseconds_to_datetime(body.punched_at)
        if body.punched_at is not None
        else datetime.now(UTC).replace(microsecond=0)
    )
    record_cursor = (
        db.attendance_logs.find(
            {
                "emp_code": body.emp_code,
                "punch_in": {"$ne": None, "$lte": punched_at},
            }
        )
        .sort("punch_in", -1)
        .limit(1)
    )
    records = list(record_cursor)
    if not records:
        later_records = list(
            db.attendance_logs.find(
                {"emp_code": body.emp_code, "punch_in": {"$ne": None}}
            )
            .sort("punch_in", -1)
            .limit(1)
        )
        if later_records and punched_at <= truncate_to_seconds(
            later_records[0]["punch_in"]
        ):
            raise_validation_error(
                "punched_at must be after punch_in",
                ("body", "punched_at"),
            )
        raise HTTPException(404, "no punch-in found")

    record = records[0]
    if record.get("punch_out") is not None:
        raise HTTPException(409, "record is already punched out")
    punch_in_at = truncate_to_seconds(record["punch_in"])
    elapsed = punched_at - punch_in_at
    if elapsed.total_seconds() <= 0 or elapsed > timedelta(hours=24):
        raise_validation_error(
            "punched_at must be after punch_in and within 24 hours",
            ("body", "punched_at"),
        )

    work_hours = compute_work_hours(punch_in_at, punched_at)
    updates = {
        "punch_out": punched_at,
        "work_hours": float(work_hours),
        "overtime_minutes": compute_overtime(
            punched_at,
            employee["shift_start"],
            employee["shift_end"],
            record["date"],
        ),
        "half_day": work_hours < Decimal("4.50"),
    }
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], "punch_out": None},
        {"$set": updates},
    )
    if result.modified_count == 0:
        raise HTTPException(409, "record is already punched out")

    record.update(updates)
    return attendance_response(record)


@app.patch(
    "/attendance/{emp_code}/{date}",
    responses=response_docs(404, 409, 422),
)
def regularize_attendance(
    emp_code: str,
    date: date,
    body: RegularizeIn,
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")
    record = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": date.isoformat()}
    )
    if record is None:
        raise HTTPException(404, "attendance record not found")

    supplied = body.model_fields_set
    old_punch_in = record.get("punch_in")
    old_punch_out = record.get("punch_out")
    status = body.status if "status" in supplied else record["status"]
    punch_in_at = (
        milliseconds_to_datetime(body.punch_in)
        if "punch_in" in supplied
        else old_punch_in
    )
    punch_out_at = (
        milliseconds_to_datetime(body.punch_out)
        if "punch_out" in supplied
        else old_punch_out
    )

    if status in {"ABSENT", "LEAVE"}:
        if "punch_in" in supplied or "punch_out" in supplied:
            raise_validation_error(
                "ABSENT and LEAVE cannot include punch times",
                ("body",),
            )
        punch_in_at = None
        punch_out_at = None
    elif punch_in_at is None:
        raise_validation_error(
            "presence statuses require punch_in",
            ("body", "punch_in"),
        )

    if punch_in_at is not None:
        punch_in_at = truncate_to_seconds(punch_in_at)
        if attendance_date_for_punch(
            punch_in_at,
            employee["shift_start"],
            employee["shift_end"],
        ) != date.isoformat():
            raise_validation_error(
                "punch_in must belong to the attendance date",
                ("body", "punch_in"),
            )
    if punch_out_at is not None:
        punch_out_at = truncate_to_seconds(punch_out_at)
        if punch_in_at is None:
            raise_validation_error(
                "punch_out requires punch_in",
                ("body", "punch_out"),
            )
        elapsed = punch_out_at - punch_in_at
        if elapsed.total_seconds() <= 0 or elapsed > timedelta(hours=24):
            raise_validation_error(
                "punch_out must be after punch_in and within 24 hours",
                ("body", "punch_out"),
            )

    if status in {"ABSENT", "LEAVE"}:
        late_minutes = 0
        work_hours = None
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = compute_late_minutes(
            punch_in_at,
            employee["shift_start"],
            employee["shift_end"],
        )
        if punch_out_at is None:
            work_hours = None
            overtime_minutes = 0
            half_day = False
        else:
            decimal_work_hours = compute_work_hours(punch_in_at, punch_out_at)
            work_hours = float(decimal_work_hours)
            overtime_minutes = compute_overtime(
                punch_out_at,
                employee["shift_start"],
                employee["shift_end"],
                date.isoformat(),
            )
            half_day = decimal_work_hours < Decimal("4.50")

    new_values = {
        "status": status,
        "punch_in": punch_in_at,
        "punch_out": punch_out_at,
        "work_hours": work_hours,
        "late_minutes": late_minutes,
        "overtime_minutes": overtime_minutes,
        "half_day": half_day,
    }
    old_values = {
        "status": record.get("status"),
        "punch_in": old_punch_in,
        "punch_out": old_punch_out,
        "work_hours": record.get("work_hours"),
        "late_minutes": record.get("late_minutes", 0),
        "overtime_minutes": record.get("overtime_minutes", 0),
        "half_day": record.get("half_day", False),
    }
    changed = {
        field: value
        for field, value in new_values.items()
        if old_values[field] != value
    }
    if not changed:
        raise_validation_error(
            "regularization does not change the record",
            ("body",),
        )

    changes = {}
    for field in changed:
        old_value = old_values[field]
        new_value = new_values[field]
        if field in {"punch_in", "punch_out"}:
            old_value = (
                datetime_to_milliseconds(truncate_to_seconds(old_value))
                if old_value is not None
                else None
            )
            new_value = (
                datetime_to_milliseconds(new_value)
                if new_value is not None
                else None
            )
        changes[field] = {"from": old_value, "to": new_value}

    history = record.get("history", [])
    history_entry = {
        "at": datetime.now(UTC).replace(microsecond=0),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    history_filter = (
        {"history": {"$size": len(history)}}
        if "history" in record
        else {"history": {"$exists": False}}
    )
    result = db.attendance_logs.update_one(
        {"_id": record["_id"], **history_filter},
        {"$set": changed, "$push": {"history": history_entry}},
    )
    if result.modified_count == 0:
        raise HTTPException(409, "attendance record changed concurrently")

    record.update(changed)
    record["history"] = [*history, history_entry]
    return attendance_response(record)


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    responses=response_docs(404, 422),
)
def employee_monthly(
    emp_code: str,
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(404, "employee not found")

    first_day, last_day = parse_month(month)
    pipeline = build_employee_monthly_pipeline(emp_code, first_day, last_day)
    result = next(iter(db.attendance_logs.aggregate(pipeline)), {})
    joined_on = date.fromisoformat(employee["joined_on"])
    working_days = count_working_days(max(joined_on, first_day), last_day)
    present_days = Decimal(str(result.get("present_days", 0)))
    attendance_pct = None
    if working_days:
        attendance_pct = round_half_up(
            present_days * Decimal(100) / Decimal(working_days),
            2,
        )
    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": working_days,
        "present_days": round_half_up(present_days),
        "leave_days": result.get("leave_days", 0),
        "late_count": result.get("late_count", 0),
        "total_late_minutes": result.get("total_late_minutes", 0),
        "total_overtime_minutes": result.get("total_overtime_minutes", 0),
        "attendance_pct": attendance_pct,
    }


@app.get("/analytics/departments/summary", responses=response_docs(422))
def department_summary(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    first_day, last_day = parse_month(month)
    pipeline = build_department_summary_pipeline(first_day, last_day, department)
    items = []
    for result in db.employees.aggregate(pipeline):
        result["avg_work_hours"] = (
            round_half_up(result["avg_work_hours"])
            if result["avg_work_hours"] is not None
            else None
        )
        result["present_days"] = round_half_up(result["present_days"])
        items.append(result)
    return {"month": month, "items": items}


@app.get("/analytics/leaderboard/late", responses=response_docs(422))
def late_leaderboard(
    month: str = Query(..., pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_day, last_day = parse_month(month)
    pipeline = build_late_leaderboard_pipeline(
        first_day,
        last_day,
        department,
        limit,
    )
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    responses=response_docs(404, 422),
)
def department_trend(
    department: str,
    from_date: date = Query(..., alias="from"),
    to_date: date = Query(..., alias="to"),
):
    if to_date < from_date or (to_date - from_date).days + 1 > 92:
        raise_validation_error(
            "date range must be ordered and no longer than 92 days",
            ("query", "to"),
        )
    employees = list(
        db.employees.find(
            {"department": department},
            {"_id": 0, "emp_code": 1, "joined_on": 1},
        )
    )
    if not employees:
        raise HTTPException(404, "department not found")
    pipeline = build_department_trend_pipeline(
        [employee["emp_code"] for employee in employees],
        [employee["joined_on"] for employee in employees],
        from_date,
        to_date,
    )
    items = list(db.attendance_logs.aggregate(pipeline))
    for item in items:
        for field in ("attendance_rate", "moving_avg_7d"):
            if item[field] is not None:
                item[field] = round_half_up(item[field], 4)
    return {"department": department, "items": items}


@app.get("/admin/explain/{endpoint}", responses=response_docs(422))
def explain_endpoint(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(
        None,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    from_date: Optional[date] = Query(None, alias="from"),
    to_date: Optional[date] = Query(None, alias="to"),
):
    if endpoint == "attendance_list":
        query = build_attendance_list_query(emp_code, date_from, date_to, status)
        explain = db.command(
            {
                "explain": {
                    "find": "attendance_logs",
                    "filter": query,
                    "projection": {"_id": 0},
                    "sort": ATTENDANCE_SORT,
                    "skip": (page - 1) * page_size,
                    "limit": page_size,
                },
                "verbosity": "executionStats",
            }
        )
        collection = "attendance_logs"
    elif endpoint == "employee_monthly":
        if emp_code is None or month is None:
            raise_validation_error(
                "emp_code and month are required",
                ("query",),
            )
        if db.employees.find_one({"emp_code": emp_code}) is None:
            raise HTTPException(404, "employee not found")
        first_day, last_day = parse_month(month)
        pipeline = build_employee_monthly_pipeline(emp_code, first_day, last_day)
        explain = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        collection = "attendance_logs"
    elif endpoint == "department_summary":
        if month is None:
            raise_validation_error("month is required", ("query", "month"))
        first_day, last_day = parse_month(month)
        pipeline = build_department_summary_pipeline(first_day, last_day, department)
        explain = db.command(
            {
                "explain": {
                    "aggregate": "employees",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        collection = "employees"
    elif endpoint == "late_leaderboard":
        if month is None:
            raise_validation_error("month is required", ("query", "month"))
        first_day, last_day = parse_month(month)
        pipeline = build_late_leaderboard_pipeline(
            first_day,
            last_day,
            department,
            limit,
        )
        explain = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        collection = "attendance_logs"
    else:
        if department is None or from_date is None or to_date is None:
            raise_validation_error(
                "department, from, and to are required",
                ("query",),
            )
        if to_date < from_date or (to_date - from_date).days + 1 > 92:
            raise_validation_error(
                "date range must be ordered and no longer than 92 days",
                ("query", "to"),
            )
        employees = list(
            db.employees.find(
                {"department": department},
                {"_id": 0, "emp_code": 1, "joined_on": 1},
            )
        )
        if not employees:
            raise HTTPException(404, "department not found")
        pipeline = build_department_trend_pipeline(
            [employee["emp_code"] for employee in employees],
            [employee["joined_on"] for employee in employees],
            from_date,
            to_date,
        )
        explain = db.command(
            {
                "explain": {
                    "aggregate": "attendance_logs",
                    "pipeline": pipeline,
                    "cursor": {},
                },
                "verbosity": "executionStats",
            }
        )
        collection = "attendance_logs"
    return {"endpoint": endpoint, "collection": collection, "explain": explain}


# --------------------------------------------------------------------------- #
# Admin endpoints use the same query and pipeline builders as the API routes.
# --------------------------------------------------------------------------- #
