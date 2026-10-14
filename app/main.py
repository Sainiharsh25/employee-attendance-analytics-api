import os
import re
import calendar
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Optional, Literal

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from pymongo import (
    MongoClient,
    ASCENDING,
    DESCENDING,
    ReturnDocument,
)
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
MONGO_DB = os.getenv("MONGO_DB", "attendance_db")

client = MongoClient(
    MONGO_URI,
    serverSelectionTimeoutMS=2500,
    tz_aware=False,
)

db = client[MONGO_DB]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc

PRESENCE = {"PRESENT", "WFH", "ON_DUTY"}
STATUSES = PRESENCE | {"ABSENT", "LEAVE"}

app = FastAPI(
    title="Employee Attendance & Analytics API",
    version="2.0.0",
)


# ============================== HELPERS ==============================

def now_utc():
    return datetime.now(UTC).replace(microsecond=0)


def truncate_utc(dt):
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)

    return dt.astimezone(UTC).replace(microsecond=0)


def epoch_ms(dt):
    if dt is None:
        return None

    return int(truncate_utc(dt).timestamp() * 1000)


def from_epoch_ms(value):
    if value is None:
        return None

    return datetime.fromtimestamp(
        value / 1000,
        tz=UTC,
    ).replace(microsecond=0)


def as_ist(dt):
    return truncate_utc(dt).astimezone(IST)


def attendance_day(punch_in, shift_start, shift_end):
    local = as_ist(punch_in)

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    if (eh, em) <= (sh, sm):
        if (local.hour, local.minute) < (eh, em):
            return local.date() - timedelta(days=1)

    return local.date()


def shift_boundary(day, shift_start, shift_end, which):
    if isinstance(day, str):
        day = date.fromisoformat(day)

    sh, sm = map(int, shift_start.split(":"))
    eh, em = map(int, shift_end.split(":"))

    overnight = (eh, em) <= (sh, sm)

    if which == "start":
        dt = datetime(
            day.year, day.month, day.day,
            sh, sm, tzinfo=IST,
        )
    else:
        if overnight:
            day += timedelta(days=1)

        dt = datetime(
            day.year, day.month, day.day,
            eh, em, tzinfo=IST,
        )

    return dt.astimezone(UTC).replace(microsecond=0)


def compute_late_minutes(
    punch_in,
    shift_start,
    shift_end="18:30",
):
    day = attendance_day(
        punch_in, shift_start, shift_end
    )

    start = shift_boundary(
        day, shift_start, shift_end, "start"
    )

    seconds = (
        truncate_utc(punch_in) - start
    ).total_seconds()

    if seconds <= 600:
        return 0

    return int(seconds // 60)


def compute_work_hours(punch_in, punch_out):
    seconds = Decimal(
        str(
            (
                truncate_utc(punch_out)
                - truncate_utc(punch_in)
            ).total_seconds()
        )
    )

    hours = seconds / Decimal("3600")

    return float(
        hours.quantize(
            Decimal("0.01"),
            rounding=ROUND_HALF_UP,
        )
    )


def compute_overtime(
    punch_out,
    shift_end,
    day,
    shift_start="09:30",
):
    scheduled_end = shift_boundary(
        day, shift_start, shift_end, "end"
    )

    seconds = (
        truncate_utc(punch_out) - scheduled_end
    ).total_seconds()

    if seconds < 1800:
        return 0

    return int(seconds // 60)


def round_half_up(value, places=2):
    quantum = Decimal("1").scaleb(-places)

    return float(
        Decimal(str(value)).quantize(
            quantum,
            rounding=ROUND_HALF_UP,
        )
    )


def parse_date(value):
    try:
        return date.fromisoformat(value)
    except (TypeError, ValueError):
        raise HTTPException(
            422, "date must be YYYY-MM-DD"
        )


def month_bounds(month):
    if not re.fullmatch(
        r"\d{4}-(0[1-9]|1[0-2])", month
    ):
        raise HTTPException(
            422, "month must be YYYY-MM"
        )

    year, mon = map(int, month.split("-"))

    first = date(year, mon, 1)
    last = date(
        year,
        mon,
        calendar.monthrange(year, mon)[1],
    )

    return (
        first.isoformat(),
        last.isoformat(),
        first,
        last,
    )


def working_days(joined_on, first, last):
    start = max(parse_date(joined_on), first)

    if start > last:
        return 0

    count = 0
    current = start

    while current <= last:
        if current.weekday() < 5:
            count += 1

        current += timedelta(days=1)

    return count


def output_employee(doc):
    doc.pop("_id", None)

    if isinstance(doc.get("created_at"), datetime):
        doc["created_at"] = epoch_ms(doc["created_at"])

    return doc


def output_attendance(doc):
    doc.pop("_id", None)

    for key in ("punch_in", "punch_out"):
        if isinstance(doc.get(key), datetime):
            doc[key] = epoch_ms(doc[key])

    doc.setdefault("history", [])
    doc.setdefault("half_day", False)

    for entry in doc["history"]:
        if isinstance(entry.get("at"), datetime):
            entry["at"] = epoch_ms(entry["at"])

        for field, change in entry.get(
            "changes", {}
        ).items():
            if field in {"punch_in", "punch_out"}:
                for side in ("from", "to"):
                    if isinstance(
                        change.get(side), datetime
                    ):
                        change[side] = epoch_ms(
                            change[side]
                        )

    return doc


def make_indexes():
    db.employees.create_index(
        [("emp_code", ASCENDING)],
        unique=True,
        name="uq_employee_code",
    )

    db.employees.create_index(
        [
            ("department", ASCENDING),
            ("emp_code", ASCENDING),
        ],
        name="ix_employee_department_code",
    )

    db.employees.create_index(
        [
            ("department", ASCENDING),
            ("joined_on", ASCENDING),
        ],
        name="ix_employee_department_joined",
    )

    db.employees.create_index(
        [("joined_on", ASCENDING)],
        name="ix_employee_joined_on",
    )

    db.attendance_logs.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING),
        ],
        unique=True,
        name="uq_attendance_employee_date",
    )

    db.attendance_logs.create_index(
        [
            ("date", DESCENDING),
            ("emp_code", ASCENDING),
        ],
        name="ix_attendance_date_code",
    )

    db.attendance_logs.create_index(
        [
            ("emp_code", ASCENDING),
            ("date", ASCENDING),
            ("status", ASCENDING),
        ],
        name="ix_attendance_employee_date_status",
    )


@app.on_event("startup")
def startup():
    make_indexes()


# ============================== MODELS ==============================

class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str = Field(
        pattern=r"^EMP\d{4,6}$"
    )
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(
        max_length=120,
        pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$",
    )
    department: str = Field(
        min_length=1, max_length=50
    )
    shift_start: str = Field(
        default="09:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    shift_end: str = Field(
        default="18:30",
        pattern=r"^([01]\d|2[0-3]):[0-5]\d$",
    )
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_join_date(cls, value):
        date.fromisoformat(value)
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError(
                "shift_start must differ from shift_end"
            )

        return self


class PunchInIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )
    status: Literal[
        "PRESENT", "WFH", "ON_DUTY"
    ] = "PRESENT"


class PunchOutIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    emp_code: str
    punched_at: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )


class RegularizeIn(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: Optional[
        Literal[
            "PRESENT", "ABSENT", "LEAVE",
            "WFH", "ON_DUTY",
        ]
    ] = None

    punch_in: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )

    punch_out: Optional[int] = Field(
        default=None,
        strict=True,
        ge=100_000_000_000,
        le=4_102_444_800_000,
    )

    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(
        min_length=1, max_length=50
    )


# ============================== HEALTH ==============================

@app.get("/health")
def health():
    try:
        client.admin.command("ping")
        return {"status": "ok"}
    except PyMongoError:
        raise HTTPException(
            503, "MongoDB unavailable"
        )


# ============================== EMPLOYEES ==============================

@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = now_utc()

    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409, "emp_code already exists"
        )

    return output_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    query = {}

    if department is not None:
        query["department"] = department

    total = db.employees.count_documents(query)

    cursor = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [output_employee(d) for d in cursor],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


# ============================== PUNCH IN ==============================

@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one(
        {"emp_code": body.emp_code}
    )

    if not emp:
        raise HTTPException(
            404, "employee not found"
        )

    ts = truncate_utc(
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    day = attendance_day(
        ts, emp["shift_start"], emp["shift_end"]
    ).isoformat()

    doc = {
        "emp_code": body.emp_code,
        "date": day,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts, emp["shift_start"], emp["shift_end"]
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }

    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(
            409, "already punched in for this date"
        )

    return output_attendance(doc)


# ============================== PUNCH OUT ==============================

@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = db.employees.find_one(
        {"emp_code": body.emp_code}
    )

    if not emp:
        raise HTTPException(
            404, "employee not found"
        )

    ts = truncate_utc(
        from_epoch_ms(body.punched_at)
        if body.punched_at is not None
        else now_utc()
    )

    rec = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": ts},
        },
        sort=[("punch_in", DESCENDING)],
    )

    if not rec:
        raise HTTPException(
            404, "no punch-in found"
        )

    if rec.get("punch_out") is not None:
        raise HTTPException(
            409, "record already punched out"
        )

    seconds = (
        ts - truncate_utc(rec["punch_in"])
    ).total_seconds()

    if seconds <= 0 or seconds > 24 * 3600:
        raise HTTPException(
            422,
            "punched_at must be after punch_in and within 24 hours",
        )

    hours = compute_work_hours(rec["punch_in"], ts)

    overtime = compute_overtime(
        ts,
        emp["shift_end"],
        rec["date"],
        emp["shift_start"],
    )

    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": ts,
                "work_hours": hours,
                "overtime_minutes": overtime,
                "half_day": hours < 4.50,
            }
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            409, "record already punched out"
        )

    return output_attendance(updated)


# ============================== ATTENDANCE LIST ==============================

@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal[
            "PRESENT", "ABSENT", "LEAVE",
            "WFH", "ON_DUTY",
        ]
    ] = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if (
        date_from
        and date_to
        and parse_date(date_from) > parse_date(date_to)
    ):
        raise HTTPException(
            422, "date_from must be <= date_to"
        )

    query = {}

    if emp_code:
        query["emp_code"] = emp_code

    if status:
        query["status"] = status

    if date_from or date_to:
        query["date"] = {}

        if date_from:
            query["date"]["$gte"] = parse_date(
                date_from
            ).isoformat()

        if date_to:
            query["date"]["$lte"] = parse_date(
                date_to
            ).isoformat()

    total = db.attendance_logs.count_documents(query)

    cursor = (
        db.attendance_logs.find(query)
        .sort(
            [
                ("date", DESCENDING),
                ("emp_code", ASCENDING),
            ]
        )
        .skip((page - 1) * page_size)
        .limit(page_size)
    )

    return {
        "items": [
            output_attendance(d) for d in cursor
        ],
        "total": total,
        "page": page,
        "page_size": page_size,
    }

# ============================== REGULARIZATION ==============================

@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: str, body: RegularizeIn):
    target_day = parse_date(date).isoformat()

    emp = db.employees.find_one(
        {"emp_code": emp_code}
    )

    rec = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": target_day}
    )

    if not emp or not rec:
        raise HTTPException(
            404, "employee or attendance record not found"
        )

    old = dict(rec)

    new_status = (
        body.status
        if body.status is not None
        else rec["status"]
    )

    new_in = (
        from_epoch_ms(body.punch_in)
        if "punch_in" in body.model_fields_set
        else rec.get("punch_in")
    )

    new_out = (
        from_epoch_ms(body.punch_out)
        if "punch_out" in body.model_fields_set
        else rec.get("punch_out")
    )

    if new_status in {"ABSENT", "LEAVE"}:
        if (
            body.punch_in is not None
            or body.punch_out is not None
        ):
            raise HTTPException(
                422,
                "ABSENT/LEAVE cannot include punch times",
            )

        new_in = None
        new_out = None

    elif new_in is None:
        raise HTTPException(
            422, "presence status requires punch_in"
        )

    if new_in is not None:
        expected_day = attendance_day(
            new_in,
            emp["shift_start"],
            emp["shift_end"],
        ).isoformat()

        if expected_day != target_day:
            raise HTTPException(
                422,
                "punch_in must belong to the attendance date",
            )

    if new_out is not None:
        if (
            new_in is None
            or truncate_utc(new_out) <= truncate_utc(new_in)
        ):
            raise HTTPException(
                422, "punch_out must be after punch_in"
            )

        if (
            truncate_utc(new_out)
            - truncate_utc(new_in)
        ).total_seconds() > 24 * 3600:
            raise HTTPException(
                422, "punch_out must be within 24 hours"
            )

    new_in = truncate_utc(new_in) if new_in else None
    new_out = truncate_utc(new_out) if new_out else None

    if new_out is None:
        hours = None
        overtime = 0
        half_day = False
    else:
        hours = compute_work_hours(new_in, new_out)

        overtime = compute_overtime(
            new_out,
            emp["shift_end"],
            target_day,
            emp["shift_start"],
        )

        half_day = hours < 4.50

    late = (
        compute_late_minutes(
            new_in,
            emp["shift_start"],
            emp["shift_end"],
        )
        if new_in
        else 0
    )

    final = {
        "status": new_status,
        "punch_in": new_in,
        "punch_out": new_out,
        "work_hours": hours,
        "late_minutes": late,
        "overtime_minutes": overtime,
        "half_day": half_day,
    }

    changes = {}

    for field, value in final.items():
        before = old.get(
            field,
            False if field == "half_day" else None,
        )

        if field in {"punch_in", "punch_out"}:
            before = (
                truncate_utc(before) if before else None
            )

        if before != value:
            if field in {"punch_in", "punch_out"}:
                old_value = epoch_ms(before)
                new_value = epoch_ms(value)
            else:
                old_value = before
                new_value = value

            changes[field] = {
                "from": old_value,
                "to": new_value,
            }

    if not changes:
        raise HTTPException(
            422, "regularization does not change the record"
        )

    history_entry = {
        "at": now_utc(),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }

    # Optimistic concurrency: only update if the record's
    # previous values are unchanged.
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": rec["_id"],
            "status": old["status"],
            "punch_in": old.get("punch_in"),
            "punch_out": old.get("punch_out"),
            "history": old.get("history", []),
        },
        {
            "$set": final,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )

    if not updated:
        raise HTTPException(
            409,
            "record changed concurrently; retry regularization",
        )

    return output_attendance(updated)


# ============================== ANALYTICS HELPERS ==============================

def present_expr():
    return {
        "$cond": [
            {
                "$in": [
                    "$status",
                    ["PRESENT", "WFH", "ON_DUTY"],
                ]
            },
            {
                "$cond": [
                    {"$eq": ["$half_day", True]},
                    0.5,
                    1,
                ]
            },
            0,
        ]
    }


def weekday_present_expr():
    weekday = {
        "$dayOfWeek": {
            "$dateFromString": {"dateString": "$date"}
        }
    }
    return {
        "$cond": [
            {
                "$and": [
                    {"$gte": [weekday, 2]},
                    {"$lte": [weekday, 6]},
                ]
            },
            present_expr(),
            0,
        ]
    }


def attendance_month_pipeline(emp_code, first, last):
    return [
        {
            "$match": {
                "emp_code": emp_code,
                "date": {
                    "$gte": first,
                    "$lte": last,
                },
            }
        },
        {
            "$group": {
                "_id": None,
                "present_days": {
                    "$sum": weekday_present_expr()
                },
                "leave_days": {
                    "$sum": {
                        "$cond": [
                            {"$eq": ["$status", "LEAVE"]},
                            1,
                            0,
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
                            1,
                            0,
                        ]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": ["$late_minutes", 0]
                    }
                },
                "total_overtime_minutes": {
                    "$sum": {
                        "$ifNull": ["$overtime_minutes", 0]
                    }
                },
            }
        },
    ]


# ============================== MONTHLY EMPLOYEE ==============================

@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(
    emp_code: str,
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
):
    emp = db.employees.find_one(
        {"emp_code": emp_code}
    )

    if not emp:
        raise HTTPException(
            404, "employee not found"
        )

    first_s, last_s, first, last = month_bounds(month)

    agg = list(
        db.attendance_logs.aggregate(
            attendance_month_pipeline(
                emp_code, first_s, last_s
            )
        )
    )

    vals = agg[0] if agg else {}

    wd = working_days(
        emp["joined_on"], first, last
    )

    present = round_half_up(
        vals.get("present_days", 0), 2
    )

    pct = (
        round_half_up(present / wd * 100, 4)
        if wd
        else None
    )

    return {
        "emp_code": emp_code,
        "month": month,
        "working_days": wd,
        "present_days": present,
        "leave_days": vals.get("leave_days", 0),
        "late_count": vals.get("late_count", 0),
        "total_late_minutes": vals.get(
            "total_late_minutes", 0
        ),
        "total_overtime_minutes": vals.get(
            "total_overtime_minutes", 0
        ),
        "attendance_pct": pct,
    }


# ============================== DEPARTMENT SUMMARY ==============================

@app.get("/analytics/departments/summary")
def department_summary(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    department: Optional[str] = None,
):
    first_s, last_s, first, last = month_bounds(month)

    employee_match = {
        "joined_on": {"$lte": last_s}
    }

    if department is not None:
        employee_match["department"] = department

    pipeline = [
        {"$match": employee_match},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$emp_code",
                                            "$$code",
                                        ]
                                    },
                                    {"$gte": ["$date", first_s]},
                                    {"$lte": ["$date", last_s]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": None,
                            "present_days": {
                                "$sum": weekday_present_expr()
                            },
                            "late_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$gt": [
                                                "$late_minutes", 0
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "total_late_minutes": {
                                "$sum": {
                                    "$ifNull": [
                                        "$late_minutes", 0
                                    ]
                                }
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                "$status", "LEAVE"
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$eq": [
                                                "$status",
                                                "ON_DUTY",
                                            ]
                                        },
                                        1,
                                        0,
                                    ]
                                }
                            },
                        }
                    },
                ],
                "as": "stats",
            }
        },
        {
            "$unwind": {
                "path": "$stats",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {
                        "$ifNull": [
                            "$stats.present_days", 0
                        ]
                    }
                },
                "late_count": {
                    "$sum": {
                        "$ifNull": ["$stats.late_count", 0]
                    }
                },
                "total_late_minutes": {
                    "$sum": {
                        "$ifNull": [
                            "$stats.total_late_minutes", 0
                        ]
                    }
                },
                "leave_count": {
                    "$sum": {
                        "$ifNull": ["$stats.leave_count", 0]
                    }
                },
                "on_duty_count": {
                    "$sum": {
                        "$ifNull": [
                            "$stats.on_duty_count", 0
                        ]
                    }
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
            }
        },
        {"$sort": {"department": 1}},
    ]

    rows = list(
        db.employees.aggregate(
            pipeline,
            allowDiskUse=True,
        )
    )

    # Average across attendance records, not averages of employees.
    weighted = list(
        db.attendance_logs.aggregate(
            [
                {
                    "$match": {
                        "date": {
                            "$gte": first_s,
                            "$lte": last_s,
                        },
                        "status": {"$in": list(PRESENCE)},
                        "work_hours": {"$ne": None},
                    }
                },
                {
                    "$lookup": {
                        "from": "employees",
                        "localField": "emp_code",
                        "foreignField": "emp_code",
                        "as": "employee",
                    }
                },
                {"$unwind": "$employee"},
                {
                    "$match": {
                        "employee.joined_on": {
                            "$lte": last_s
                        },
                        **(
                            {"employee.department": department}
                            if department is not None
                            else {}
                        ),
                    }
                },
                {
                    "$group": {
                        "_id": "$employee.department",
                        "avg": {"$avg": "$work_hours"},
                    }
                },
            ],
            allowDiskUse=True,
        )
    )

    average_by_department = {
        row["_id"]: row["avg"]
        for row in weighted
    }

    items = []

    for row in rows:
        avg = average_by_department.get(
            row["department"]
        )

        row["avg_work_hours"] = (
            round_half_up(avg, 2)
            if avg is not None
            else None
        )

        row["present_days"] = round_half_up(
            row["present_days"], 2
        )

        items.append(row)

    return {
        "month": month,
        "items": items,
    }
# ============================== LATE LEADERBOARD ==============================

@app.get("/analytics/leaderboard/late")
def late_leaderboard(
    month: str = Query(
        ...,
        pattern=r"^\d{4}-(0[1-9]|1[0-2])$",
    ),
    limit: int = Query(10, ge=1, le=50),
    department: Optional[str] = None,
):
    first_s, last_s, _, _ = month_bounds(month)

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first_s,
                    "$lte": last_s,
                },
                "late_minutes": {"$gt": 0},
            }
        },
        {
            "$group": {
                "_id": "$emp_code",
                "total_late_minutes": {
                    "$sum": "$late_minutes"
                },
                "late_count": {"$sum": 1},
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
    ]

    if department is not None:
        pipeline.append(
            {
                "$match": {
                    "employee.department": department
                }
            }
        )

    pipeline.extend(
        [
            {
                "$setWindowFields": {
                    "sortBy": {
                        "total_late_minutes": -1
                    },
                    "output": {
                        "rank": {"$rank": {}}
                    },
                }
            },
            {"$match": {"rank": {"$lte": limit}}},
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
            {
                "$sort": {
                    "total_late_minutes": -1,
                    "emp_code": 1,
                }
            },
        ]
    )

    return {
        "month": month,
        "items": list(
            db.attendance_logs.aggregate(
                pipeline,
                allowDiskUse=True,
            )
        ),
    }


# ============================== DEPARTMENT TREND ==============================

@app.get("/analytics/departments/{department}/trend")
def department_trend(
    department: str,
    from_date: str = Query(..., alias="from"),
    to_date: str = Query(..., alias="to"),
):
    first = parse_date(from_date)
    last = parse_date(to_date)

    if (
        last < first
        or (last - first).days + 1 > 92
    ):
        raise HTTPException(
            422,
            "invalid date range; maximum is 92 days",
        )

    if db.employees.count_documents(
        {"department": department}
    ) == 0:
        raise HTTPException(
            404, "department not found"
        )

    pipeline = [
        {
            "$match": {
                "date": {
                    "$gte": first.isoformat(),
                    "$lte": last.isoformat(),
                }
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }
        },
        {"$unwind": "$employee"},
        {
            "$match": {
                "employee.department": department
            }
        },
        {
            "$group": {
                "_id": "$date",
                "present_count": {
                    "$sum": weekday_present_expr()
                },
                "late_count": {
                    "$sum": {
                        "$cond": [
                            {"$gt": ["$late_minutes", 0]},
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
                "date": {
                    "$dateFromString": {
                        "dateString": "$_id"
                    }
                },
                "present_count": 1,
                "late_count": 1,
            }
        },
        {
            "$densify": {
                "field": "date",
                "range": {
                    "step": 1,
                    "unit": "day",
                    "bounds": [
                        datetime.combine(
                            first, datetime.min.time()
                        ),
                        datetime.combine(
                            last + timedelta(days=1),
                            datetime.min.time(),
                        ),
                    ],
                },
            }
        },
        {
            "$fill": {
                "output": {
                    "present_count": {"value": 0},
                    "late_count": {"value": 0},
                }
            }
        },
        {
            "$set": {
                "date_str": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$date",
                    }
                },
                "is_working_day": {
                    "$and": [
                        {
                            "$gte": [
                                {"$dayOfWeek": "$date"},
                                2,
                            ]
                        },
                        {
                            "$lte": [
                                {"$dayOfWeek": "$date"},
                                6,
                            ]
                        },
                    ]
                },
            }
        },
        {
            "$lookup": {
                "from": "employees",
                "let": {"day": "$date_str"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {
                                        "$eq": [
                                            "$department",
                                            department,
                                        ]
                                    },
                                    {
                                        "$lte": [
                                            "$joined_on",
                                            "$$day",
                                        ]
                                    },
                                ]
                            }
                        }
                    },
                    {"$count": "n"},
                ],
                "as": "hc",
            }
        },
        {
            "$set": {
                "headcount": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$hc.n", 0]},
                        0,
                    ]
                }
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {
                            "$and": [
                                "$is_working_day",
                                {"$gt": ["$headcount", 0]},
                            ]
                        },
                        {
                            "$divide": [
                                "$present_count",
                                "$headcount",
                            ]
                        },
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
                        "window": {
                            "documents": [-6, 0]
                        },
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": "$date_str",
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": 1,
                "moving_avg_7d": 1,
            }
        },
        {"$sort": {"date": 1}},
    ]

    rows = list(
        db.attendance_logs.aggregate(
            pipeline,
            allowDiskUse=True,
        )
    )

    for row in rows:
        for key in (
            "attendance_rate",
            "moving_avg_7d",
        ):
            if row.get(key) is not None:
                row[key] = round_half_up(
                    row[key], 4
                )

        row["present_count"] = round_half_up(
            row.get("present_count", 0), 2
        )

    return {
        "department": department,
        "items": rows,
    }


# ============================== EXPLAIN ==============================

def explain_find(
    collection,
    query,
    sort,
    skip=0,
    limit=20,
):
    return db.command(
        {
            "explain": {
                "find": collection.name,
                "filter": query,
                "sort": dict(sort),
                "skip": skip,
                "limit": limit,
            },
            "verbosity": "executionStats",
        }
    )


def explain_aggregate(collection, pipeline):
    return db.command(
        {
            "explain": {
                "aggregate": collection.name,
                "pipeline": pipeline,
                "cursor": {},
            },
            "verbosity": "executionStats",
        }
    )


@app.get("/admin/explain/{endpoint}")
def admin_explain(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = None,
    department: Optional[str] = None,
    limit: int = Query(10, ge=1, le=50),
    date_from: Optional[str] = None,
    date_to: Optional[str] = None,
    status: Optional[
        Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    ] = None,
    from_date: Optional[str] = Query(None, alias="from"),
    to_date: Optional[str] = Query(None, alias="to"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
):
    if endpoint == "attendance_list":
        if date_from and date_to and parse_date(date_from) > parse_date(date_to):
            raise HTTPException(422, "invalid date range")

        query = {}
        if emp_code:
            query["emp_code"] = emp_code
        if status:
            query["status"] = status
        if date_from or date_to:
            query["date"] = {}
            if date_from:
                query["date"]["$gte"] = parse_date(date_from).isoformat()
            if date_to:
                query["date"]["$lte"] = parse_date(date_to).isoformat()

        return explain_find(
            db.attendance_logs, query,
            [("date", DESCENDING), ("emp_code", ASCENDING)],
            (page - 1) * page_size, page_size,
        )

    if endpoint == "employee_monthly":
        if not emp_code or not month:
            raise HTTPException(422, "emp_code and month are required")
        first, last, _, _ = month_bounds(month)
        return explain_aggregate(
            db.attendance_logs,
            attendance_month_pipeline(emp_code, first, last),
        )

    if endpoint == "department_summary":
        if not month:
            raise HTTPException(422, "month is required")
        first, last, _, _ = month_bounds(month)

        employee_match = {"joined_on": {"$lte": last}}
        if department is not None:
            employee_match["department"] = department

        summary_pipeline = [
            {"$match": employee_match},
            {"$lookup": {
                "from": "attendance_logs",
                "let": {"code": "$emp_code"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$emp_code", "$$code"]},
                        {"$gte": ["$date", first]},
                        {"$lte": ["$date", last]},
                    ]}}},
                    {"$group": {
                        "_id": None,
                        "present_days": {"$sum": weekday_present_expr()},
                        "late_count": {"$sum": {"$cond": [
                            {"$gt": ["$late_minutes", 0]}, 1, 0
                        ]}},
                        "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                        "leave_count": {"$sum": {"$cond": [
                            {"$eq": ["$status", "LEAVE"]}, 1, 0
                        ]}},
                        "on_duty_count": {"$sum": {"$cond": [
                            {"$eq": ["$status", "ON_DUTY"]}, 1, 0
                        ]}},
                    }},
                ],
                "as": "stats",
            }},
            {"$unwind": {"path": "$stats", "preserveNullAndEmptyArrays": True}},
            {"$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {"$sum": {"$ifNull": ["$stats.present_days", 0]}},
                "late_count": {"$sum": {"$ifNull": ["$stats.late_count", 0]}},
                "total_late_minutes": {"$sum": {"$ifNull": ["$stats.total_late_minutes", 0]}},
                "leave_count": {"$sum": {"$ifNull": ["$stats.leave_count", 0]}},
                "on_duty_count": {"$sum": {"$ifNull": ["$stats.on_duty_count", 0]}},
            }},
            {"$project": {
                "_id": 0, "department": "$_id", "headcount": 1,
                "present_days": 1, "late_count": 1,
                "total_late_minutes": 1, "leave_count": 1,
                "on_duty_count": 1,
            }},
            {"$sort": {"department": 1}},
        ]

        weighted_pipeline = [
            {"$match": {
                "date": {"$gte": first, "$lte": last},
                "status": {"$in": list(PRESENCE)},
                "work_hours": {"$ne": None},
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "emp_code",
                "foreignField": "emp_code",
                "as": "employee",
            }},
            {"$unwind": "$employee"},
            {"$match": {
                "employee.joined_on": {"$lte": last},
                **({"employee.department": department} if department is not None else {}),
            }},
            {"$group": {"_id": "$employee.department", "avg": {"$avg": "$work_hours"}}},
        ]

        return {
            "summary_plan": explain_aggregate(db.employees, summary_pipeline),
            "weighted_average_plan": explain_aggregate(db.attendance_logs, weighted_pipeline),
        }

    if endpoint == "late_leaderboard":
        if not month:
            raise HTTPException(422, "month is required")
        first, last, _, _ = month_bounds(month)

        pipeline = [
            {"$match": {
                "date": {"$gte": first, "$lte": last},
                "late_minutes": {"$gt": 0},
            }},
            {"$group": {
                "_id": "$emp_code",
                "total_late_minutes": {"$sum": "$late_minutes"},
                "late_count": {"$sum": 1},
            }},
            {"$lookup": {
                "from": "employees",
                "localField": "_id",
                "foreignField": "emp_code",
                "as": "employee",
            }},
            {"$unwind": "$employee"},
        ]
        if department is not None:
            pipeline.append({"$match": {"employee.department": department}})
        pipeline.extend([
            {"$setWindowFields": {
                "sortBy": {"total_late_minutes": -1},
                "output": {"rank": {"$rank": {}}},
            }},
            {"$match": {"rank": {"$lte": limit}}},
            {"$project": {
                "_id": 0, "rank": 1, "emp_code": "$_id",
                "name": "$employee.name", "department": "$employee.department",
                "total_late_minutes": 1, "late_count": 1,
            }},
            {"$sort": {"total_late_minutes": -1, "emp_code": 1}},
        ])
        return explain_aggregate(db.attendance_logs, pipeline)

    if endpoint == "department_trend":
        if not department or not from_date or not to_date:
            raise HTTPException(422, "department, from and to are required")

        first = parse_date(from_date)
        last = parse_date(to_date)
        if last < first or (last - first).days + 1 > 92:
            raise HTTPException(422, "invalid date range")
        if db.employees.count_documents({"department": department}) == 0:
            raise HTTPException(404, "department not found")

        start_dt = datetime.combine(first, datetime.min.time())
        end_dt = datetime.combine(last + timedelta(days=1), datetime.min.time())
        pipeline = [
            {"$match": {"date": {"$gte": first.isoformat(), "$lte": last.isoformat()}}},
            {"$lookup": {
                "from": "employees", "localField": "emp_code",
                "foreignField": "emp_code", "as": "employee",
            }},
            {"$unwind": "$employee"},
            {"$match": {"employee.department": department}},
            {"$group": {
                "_id": "$date",
                "present_count": {"$sum": weekday_present_expr()},
                "late_count": {"$sum": {"$cond": [
                    {"$gt": ["$late_minutes", 0]}, 1, 0
                ]}},
            }},
            {"$project": {
                "_id": 0,
                "date": {"$dateFromString": {"dateString": "$_id"}},
                "present_count": 1, "late_count": 1,
            }},
            {"$densify": {
                "field": "date",
                "range": {"step": 1, "unit": "day", "bounds": [start_dt, end_dt]},
            }},
            {"$fill": {"output": {"present_count": {"value": 0}, "late_count": {"value": 0}}}},
            {"$set": {
                "date_str": {"$dateToString": {"format": "%Y-%m-%d", "date": "$date"}},
                "is_working_day": {"$and": [
                    {"$gte": [{"$dayOfWeek": "$date"}, 2]},
                    {"$lte": [{"$dayOfWeek": "$date"}, 6]},
                ]},
            }},
            {"$lookup": {
                "from": "employees",
                "let": {"day": "$date_str"},
                "pipeline": [
                    {"$match": {"$expr": {"$and": [
                        {"$eq": ["$department", department]},
                        {"$lte": ["$joined_on", "$$day"]},
                    ]}}},
                    {"$count": "n"},
                ],
                "as": "hc",
            }},
            {"$set": {"headcount": {"$ifNull": [{"$arrayElemAt": ["$hc.n", 0]}, 0]}}},
            {"$set": {"attendance_rate": {"$cond": [
                {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                {"$divide": ["$present_count", "$headcount"]},
                None,
            ]}}},
            {"$setWindowFields": {
                "sortBy": {"date": 1},
                "output": {"moving_avg_7d": {
                    "$avg": "$attendance_rate",
                    "window": {"documents": [-6, 0]},
                }},
            }},
            {"$project": {
                "_id": 0, "date": "$date_str", "is_working_day": 1,
                "headcount": 1, "present_count": 1, "late_count": 1,
                "attendance_rate": 1, "moving_avg_7d": 1,
            }},
            {"$sort": {"date": 1}},
        ]
        return explain_aggregate(db.attendance_logs, pipeline)

    raise HTTPException(422, "unsupported endpoint")
