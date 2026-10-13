"""
Employee Attendance & Analytics API - STARTER

Run:  uvicorn app.main:app --port 8000
Env:  MONGO_URI, MONGO_DB (a local .env is loaded for convenience)

This file was written quickly by a colleague who has left the company. The happy path
works, but nobody has reviewed it. Read PROBLEM_STATEMENT.docx for what is expected of you,
openapi.yaml for the contract and DATA_MODEL.md for what is stored in MongoDB.
"""
import os
import calendar
from contextlib import asynccontextmanager
from datetime import date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Any, Literal, Optional

from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Path, Query
from pydantic import BaseModel, ConfigDict, Field, StrictInt, field_validator, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv()

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"))
db = client[os.getenv("MONGO_DB", "attendance_db")]

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc
EpochMillisValue = Annotated[
    int, Field(ge=100_000_000_000, le=4_102_444_800_000)
]


@asynccontextmanager
async def lifespan(_app: FastAPI):
    db.employees.create_index(
        [("emp_code", ASCENDING)], unique=True, name="uq_employees_emp_code"
    )
    db.employees.create_index(
        [("department", ASCENDING), ("emp_code", ASCENDING)],
        name="ix_employees_department_emp_code",
    )
    db.employees.create_index(
        [("joined_on", ASCENDING)], name="ix_employees_joined_on"
    )
    db.attendance_logs.create_index(
        [("emp_code", ASCENDING), ("date", ASCENDING)],
        unique=True,
        name="uq_attendance_emp_code_date",
    )
    db.attendance_logs.create_index(
        [("date", DESCENDING), ("emp_code", ASCENDING)],
        name="ix_attendance_date_emp_code",
    )
    yield


app = FastAPI(
    title="Employee Attendance & Analytics API", version="2.0.0", lifespan=lifespan
)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #
def as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def truncate_to_seconds(value: datetime) -> datetime:
    return as_utc(value).replace(microsecond=0)


def epoch_millis_to_datetime(value: int) -> datetime:
    return truncate_to_seconds(
        datetime(1970, 1, 1, tzinfo=UTC) + timedelta(milliseconds=value)
    )


def datetime_to_epoch_millis(value: Optional[datetime]) -> Optional[int]:
    if value is None:
        return None
    delta = as_utc(value) - datetime(1970, 1, 1, tzinfo=UTC)
    return delta.days * 86_400_000 + delta.seconds * 1_000 + delta.microseconds // 1_000


def shift_clock(value: str) -> time:
    hour, minute = map(int, value.split(":"))
    return time(hour, minute)


def attendance_date_for(punch_in: datetime, shift_start: str, shift_end: str) -> date:
    local = truncate_to_seconds(punch_in).astimezone(IST)
    start_clock = shift_clock(shift_start)
    end_clock = shift_clock(shift_end)
    attendance_day = local.date()
    if end_clock <= start_clock and local.timetz().replace(tzinfo=None) < end_clock:
        attendance_day -= timedelta(days=1)
    return attendance_day


def compute_late_minutes(
    punch_in: datetime,
    shift_start: str,
    shift_end: Optional[str] = None,
    attendance_day: Optional[date] = None,
) -> int:
    local = truncate_to_seconds(punch_in).astimezone(IST)
    start_clock = shift_clock(shift_start)
    if attendance_day is not None:
        shift_day = attendance_day
    else:
        shift_day = local.date()
    if (
        attendance_day is None
        and shift_end is not None
        and shift_clock(shift_end) <= start_clock
    ):
        if local.timetz().replace(tzinfo=None) < shift_clock(shift_end):
            shift_day -= timedelta(days=1)
    shift_start_at = datetime.combine(shift_day, start_clock, tzinfo=IST)
    elapsed_seconds = int((local - shift_start_at).total_seconds())
    if elapsed_seconds <= 600:
        return 0
    return elapsed_seconds // 60


def compute_work_hours(punch_in: datetime, punch_out: datetime) -> float:
    seconds = int(
        (truncate_to_seconds(punch_out) - truncate_to_seconds(punch_in)).total_seconds()
    )
    rounded = (Decimal(seconds) / Decimal(3600)).quantize(
        Decimal("0.01"), rounding=ROUND_HALF_UP
    )
    return float(rounded)


def compute_overtime(
    punch_out: datetime,
    shift_end: str,
    date_str: str,
    shift_start: Optional[str] = None,
) -> int:
    shift_day = date.fromisoformat(date_str)
    if shift_start is not None and shift_clock(shift_end) <= shift_clock(shift_start):
        shift_day += timedelta(days=1)
    shift_end_at = datetime.combine(shift_day, shift_clock(shift_end), tzinfo=IST)
    local_punch_out = truncate_to_seconds(punch_out).astimezone(IST)
    elapsed_seconds = int((local_punch_out - shift_end_at).total_seconds())
    if elapsed_seconds < 1_800:
        return 0
    return elapsed_seconds // 60


def serialize_employee(doc: dict) -> dict:
    return {
        "emp_code": doc["emp_code"],
        "name": doc["name"],
        "email": doc["email"],
        "department": doc["department"],
        "shift_start": doc["shift_start"],
        "shift_end": doc["shift_end"],
        "joined_on": doc["joined_on"],
        "created_at": datetime_to_epoch_millis(doc["created_at"]),
    }


def serialize_attendance(doc: dict) -> dict:
    serialized_history = []
    for entry in doc.get("history", []):
        changes = {}
        for field, values in entry.get("changes", {}).items():
            change = dict(values)
            if field in {"punch_in", "punch_out"}:
                change["from"] = datetime_to_epoch_millis(change.get("from"))
                change["to"] = datetime_to_epoch_millis(change.get("to"))
            changes[field] = change
        serialized_history.append(
            {
                "at": datetime_to_epoch_millis(entry.get("at")),
                "by": entry["by"],
                "reason": entry["reason"],
                "changes": changes,
            }
        )
    return {
        "emp_code": doc["emp_code"],
        "date": doc["date"],
        "status": doc["status"],
        "punch_in": datetime_to_epoch_millis(doc.get("punch_in")),
        "punch_out": datetime_to_epoch_millis(doc.get("punch_out")),
        "work_hours": doc.get("work_hours"),
        "late_minutes": doc.get("late_minutes", 0),
        "overtime_minutes": doc.get("overtime_minutes", 0),
        "half_day": doc.get("half_day", False),
        "history": serialized_history,
    }


# --------------------------------------------------------------------------- #
# Models
# --------------------------------------------------------------------------- #
class EmployeeIn(BaseModel):
    model_config = ConfigDict(extra="ignore")

    emp_code: str = Field(pattern=r"^EMP\d{4,6}$")
    name: str = Field(min_length=1, max_length=100)
    email: str = Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
    department: str = Field(min_length=1, max_length=50)
    shift_start: str = Field(default="09:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    shift_end: str = Field(default="18:30", pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    joined_on: str

    @field_validator("joined_on")
    @classmethod
    def validate_joined_on(cls, value: str) -> str:
        try:
            parsed = date.fromisoformat(value)
        except ValueError as exc:
            raise ValueError("joined_on must be a valid YYYY-MM-DD date") from exc
        if parsed.isoformat() != value:
            raise ValueError("joined_on must use YYYY-MM-DD format")
        return value

    @model_validator(mode="after")
    def validate_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None, ge=100_000_000_000, le=4_102_444_800_000
    )
    status: Literal["PRESENT", "WFH", "ON_DUTY"] = "PRESENT"


class PunchOutIn(BaseModel):
    emp_code: str
    punched_at: Optional[StrictInt] = Field(
        default=None, ge=100_000_000_000, le=4_102_444_800_000
    )


class RegularizeIn(BaseModel):
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"] = Field(
        default=None
    )
    punch_in: StrictInt = Field(
        default=None, ge=100_000_000_000, le=4_102_444_800_000
    )
    punch_out: StrictInt = Field(
        default=None, ge=100_000_000_000, le=4_102_444_800_000
    )
    reason: str = Field(min_length=5, max_length=200)
    regularized_by: str = Field(min_length=1, max_length=50)


class ErrorResponse(BaseModel):
    detail: str


class ValidationErrorResponse(BaseModel):
    detail: list[dict[str, Any]]


class HistoryChangeResponse(BaseModel):
    from_value: Any = Field(alias="from")
    to_value: Any = Field(alias="to")


class HistoryEntryResponse(BaseModel):
    at: EpochMillisValue
    by: str
    reason: str
    changes: dict[str, HistoryChangeResponse]


class AttendanceRecordResponse(BaseModel):
    emp_code: str
    date: date
    status: Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
    punch_in: Optional[EpochMillisValue]
    punch_out: Optional[EpochMillisValue]
    work_hours: Optional[float]
    late_minutes: int
    overtime_minutes: int
    half_day: bool
    history: list[HistoryEntryResponse]


class EmployeeMonthlyResponse(BaseModel):
    emp_code: str
    month: str
    working_days: int
    present_days: float
    leave_days: int
    late_count: int
    total_late_minutes: int
    total_overtime_minutes: int
    attendance_pct: Optional[float]


class DepartmentSummaryItem(BaseModel):
    department: str
    headcount: int
    present_days: float
    avg_work_hours: Optional[float]
    late_count: int
    total_late_minutes: int
    leave_count: int
    on_duty_count: int


class DepartmentSummaryResponse(BaseModel):
    month: str
    items: list[DepartmentSummaryItem]


class LateLeaderboardItem(BaseModel):
    rank: int
    emp_code: str
    name: str
    department: str
    total_late_minutes: int
    late_count: int


class LateLeaderboardResponse(BaseModel):
    month: str
    items: list[LateLeaderboardItem]


class DepartmentTrendItem(BaseModel):
    date: date
    is_working_day: bool
    headcount: int
    present_count: float
    late_count: int
    attendance_rate: Optional[float]
    moving_avg_7d: Optional[float]


class DepartmentTrendResponse(BaseModel):
    department: str
    items: list[DepartmentTrendItem]


class ExplainResponse(BaseModel):
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ]
    collection: str
    explain: dict[str, Any]


# --------------------------------------------------------------------------- #
# Endpoints provided
# --------------------------------------------------------------------------- #
@app.get("/health")
def health():
    try:
        db.command("ping")
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB unavailable") from exc
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = datetime.now(UTC)
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(status_code=409, detail="emp_code already exists") from exc
    return serialize_employee(doc)


@app.get("/employees")
def list_employees(
    department: Optional[str] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    query = {"department": department} if department is not None else {}
    skip = (page - 1) * page_size
    total = db.employees.count_documents(query)
    docs = (
        db.employees.find(query, {"_id": 0})
        .sort("emp_code", ASCENDING)
        .skip(skip)
        .limit(page_size)
    )
    return {
        "items": [serialize_employee(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = db.employees.find_one({"emp_code": body.emp_code})
    if emp is None:
        raise HTTPException(status_code=404, detail="employee not found")
    ts = (
        epoch_millis_to_datetime(body.punched_at)
        if body.punched_at is not None
        else truncate_to_seconds(datetime.now(UTC))
    )
    attendance_day = attendance_date_for(ts, emp["shift_start"], emp["shift_end"])
    d = attendance_day.isoformat()
    doc = {
        "emp_code": body.emp_code,
        "date": d,
        "status": body.status,
        "punch_in": ts,
        "punch_out": None,
        "work_hours": None,
        "late_minutes": compute_late_minutes(
            ts, emp["shift_start"], emp["shift_end"], attendance_day
        ),
        "overtime_minutes": 0,
        "half_day": False,
        "history": [],
    }
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError as exc:
        raise HTTPException(
            status_code=409, detail="already punched in for this date"
        ) from exc
    return serialize_attendance(doc)


@app.post(
    "/attendance/punch-out",
    response_model=AttendanceRecordResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Not found"},
        409: {"model": ErrorResponse, "description": "Conflict"},
        422: {"model": ValidationErrorResponse, "description": "Validation error"},
    },
)
def punch_out(body: PunchOutIn):
    employee = db.employees.find_one({"emp_code": body.emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")

    punched_at = (
        epoch_millis_to_datetime(body.punched_at)
        if body.punched_at is not None
        else truncate_to_seconds(datetime.now(UTC))
    )
    record = db.attendance_logs.find_one(
        {
            "emp_code": body.emp_code,
            "punch_in": {"$ne": None, "$lte": punched_at},
        },
        sort=[("punch_in", DESCENDING)],
    )
    if record is None:
        raise HTTPException(status_code=404, detail="no eligible punch-in found")
    if record.get("punch_out") is not None:
        raise HTTPException(status_code=409, detail="attendance record already punched out")

    punch_in_at = truncate_to_seconds(record["punch_in"])
    elapsed_seconds = (punched_at - punch_in_at).total_seconds()
    if elapsed_seconds <= 0 or elapsed_seconds > 86_400:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["body", "punched_at"],
                    "msg": "punched_at must be after punch_in and no more than 24 hours later",
                    "type": "value_error",
                }
            ],
        )

    work_hours = compute_work_hours(punch_in_at, punched_at)
    overtime_minutes = compute_overtime(
        punched_at,
        employee["shift_end"],
        record["date"],
        employee["shift_start"],
    )
    updated = db.attendance_logs.find_one_and_update(
        {
            "_id": record["_id"],
            "emp_code": body.emp_code,
            "date": record["date"],
            "punch_out": None,
        },
        {
            "$set": {
                "punch_out": punched_at,
                "work_hours": work_hours,
                "overtime_minutes": overtime_minutes,
                "half_day": work_hours < 4.50,
            }
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record already punched out")
    return serialize_attendance(updated)


@app.patch(
    "/attendance/{emp_code}/{date}",
    response_model=AttendanceRecordResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Not found"},
        409: {"model": ErrorResponse, "description": "Conflict"},
        422: {"model": ValidationErrorResponse, "description": "Validation error"},
    },
)
def regularize_attendance(
    body: RegularizeIn,
    emp_code: str = Path(pattern=r"^EMP\d{4,6}$"),
    attendance_day: date = Path(alias="date"),
):
    employee = db.employees.find_one({"emp_code": emp_code})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    current = db.attendance_logs.find_one(
        {"emp_code": emp_code, "date": attendance_day.isoformat()}
    )
    if current is None:
        raise HTTPException(status_code=404, detail="attendance record not found")

    requested_fields = body.model_fields_set
    status = body.status if "status" in requested_fields else current["status"]
    absent_or_leave = status in {"ABSENT", "LEAVE"}
    if absent_or_leave and ({"punch_in", "punch_out"} & requested_fields):
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["body"],
                    "msg": "punch times cannot be supplied when status is ABSENT or LEAVE",
                    "type": "value_error",
                }
            ],
        )

    if absent_or_leave:
        punch_in_at = None
        punch_out_at = None
    else:
        raw_punch_in = (
            epoch_millis_to_datetime(body.punch_in)
            if "punch_in" in requested_fields
            else current.get("punch_in")
        )
        punch_in_at = (
            truncate_to_seconds(raw_punch_in) if raw_punch_in is not None else None
        )
        raw_punch_out = (
            epoch_millis_to_datetime(body.punch_out)
            if "punch_out" in requested_fields
            else current.get("punch_out")
        )
        punch_out_at = (
            truncate_to_seconds(raw_punch_out) if raw_punch_out is not None else None
        )

        if punch_in_at is None:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "loc": ["body", "punch_in"],
                        "msg": "presence status requires a punch_in",
                        "type": "value_error",
                    }
                ],
            )
        if attendance_date_for(
            punch_in_at, employee["shift_start"], employee["shift_end"]
        ) != attendance_day:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "loc": ["body", "punch_in"],
                        "msg": "punch_in must fall on the record's attendance date",
                        "type": "value_error",
                    }
                ],
            )
        if punch_out_at is not None:
            elapsed_seconds = (punch_out_at - punch_in_at).total_seconds()
            if elapsed_seconds <= 0 or elapsed_seconds > 86_400:
                raise HTTPException(
                    status_code=422,
                    detail=[
                        {
                            "loc": ["body", "punch_out"],
                            "msg": "punch_out must be after punch_in and within 24 hours",
                            "type": "value_error",
                        }
                    ],
                )

    if absent_or_leave:
        work_hours = None
        late_minutes = 0
        overtime_minutes = 0
        half_day = False
    else:
        late_minutes = compute_late_minutes(
            punch_in_at,
            employee["shift_start"],
            employee["shift_end"],
            attendance_day,
        )
        if punch_out_at is None:
            work_hours = None
            overtime_minutes = 0
            half_day = False
        else:
            work_hours = compute_work_hours(punch_in_at, punch_out_at)
            overtime_minutes = compute_overtime(
                punch_out_at,
                employee["shift_end"],
                attendance_day.isoformat(),
                employee["shift_start"],
            )
            half_day = work_hours < 4.50

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
        "status": current["status"],
        "punch_in": current.get("punch_in"),
        "punch_out": current.get("punch_out"),
        "work_hours": current.get("work_hours"),
        "late_minutes": current.get("late_minutes", 0),
        "overtime_minutes": current.get("overtime_minutes", 0),
        "half_day": current.get("half_day", False),
    }

    def same_value(left, right):
        if isinstance(left, datetime) and isinstance(right, datetime):
            return as_utc(left) == as_utc(right)
        return left == right

    changes = {
        field: {"from": old_values[field], "to": new_values[field]}
        for field in new_values
        if not same_value(old_values[field], new_values[field])
    }
    if not changes:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["body"],
                    "msg": "regularization does not change the record",
                    "type": "value_error",
                }
            ],
        )

    history_entry = {
        "at": truncate_to_seconds(datetime.now(UTC)),
        "by": body.regularized_by,
        "reason": body.reason,
        "changes": changes,
    }
    update_filter = {
        "_id": current["_id"],
        "emp_code": emp_code,
        "date": attendance_day.isoformat(),
    }
    for field in (
        "status",
        "punch_in",
        "punch_out",
        "work_hours",
        "late_minutes",
        "overtime_minutes",
        "half_day",
        "history",
    ):
        update_filter[field] = (
            current[field] if field in current else {"$exists": False}
        )

    updated = db.attendance_logs.find_one_and_update(
        update_filter,
        {
            "$set": new_values,
            "$push": {"history": history_entry},
        },
        return_document=ReturnDocument.AFTER,
    )
    if updated is None:
        raise HTTPException(status_code=409, detail="attendance record changed concurrently")
    return serialize_attendance(updated)


@app.get(
    "/analytics/employees/{emp_code}/monthly",
    response_model=EmployeeMonthlyResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Not found"},
        422: {"model": ValidationErrorResponse, "description": "Validation error"},
    },
)
def employee_monthly(
    emp_code: str,
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
):
    employee = db.employees.find_one({"emp_code": emp_code}, {"_id": 0, "emp_code": 1})
    if employee is None:
        raise HTTPException(status_code=404, detail="employee not found")
    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["query", "month"],
                    "msg": "month must be a valid YYYY-MM calendar month",
                    "type": "value_error",
                }
            ],
        ) from exc
    next_month = (month_start.replace(day=28) + timedelta(days=4)).replace(day=1)
    month_start_text = month_start.isoformat()
    next_month_text = next_month.isoformat()
    weekday_condition = {
        "$and": [
            {"$gte": [{"$dayOfWeek": "$$attendance_day"}, 2]},
            {"$lte": [{"$dayOfWeek": "$$attendance_day"}, 6]},
        ]
    }
    present_weekday_condition = {
        "$and": [
            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            {
                "$gte": [
                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    2,
                ]
            },
            {
                "$lte": [
                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    6,
                ]
            },
        ]
    }
    pipeline = [
        {"$match": {"emp_code": emp_code}},
        {
            "$set": {
                "_month_start": {
                    "$dateFromString": {"dateString": month_start_text}
                },
                "_next_month": {
                    "$dateFromString": {"dateString": next_month_text}
                },
                "_joined_date": {
                    "$dateFromString": {"dateString": "$joined_on"}
                },
            }
        },
        {
            "$set": {
                "_working_start": {"$max": ["$_joined_date", "$_month_start"]}
            }
        },
        {
            "$set": {
                "_working_days": {
                    "$size": {
                        "$filter": {
                            "input": {
                                "$map": {
                                    "input": {
                                        "$range": [
                                            0,
                                            {
                                                "$dateDiff": {
                                                    "startDate": "$_working_start",
                                                    "endDate": "$_next_month",
                                                    "unit": "day",
                                                }
                                            },
                                        ]
                                    },
                                    "as": "day_offset",
                                    "in": {
                                        "$dateAdd": {
                                            "startDate": "$_working_start",
                                            "unit": "day",
                                            "amount": "$$day_offset",
                                        }
                                    },
                                }
                            },
                            "as": "attendance_day",
                            "cond": weekday_condition,
                        }
                    }
                }
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_code": "$emp_code",
                    "month_start": month_start_text,
                    "next_month": next_month_text,
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", "$$month_start"]},
                                    {"$lt": ["$date", "$$next_month"]},
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
                                        present_weekday_condition,
                                        {
                                            "$cond": [
                                                {"$ifNull": ["$half_day", False]},
                                                0.5,
                                                1,
                                            ]
                                        },
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
                ],
                "as": "_monthly_stats",
            }
        },
        {
            "$set": {
                "_stats": {
                    "$ifNull": [
                        {"$arrayElemAt": ["$_monthly_stats", 0]},
                        {
                            "present_days": 0,
                            "leave_days": 0,
                            "late_count": 0,
                            "total_late_minutes": 0,
                            "total_overtime_minutes": 0,
                        },
                    ]
                }
            }
        },
        {
            "$project": {
                "_id": 0,
                "emp_code": 1,
                "month": {"$literal": month},
                "working_days": "$_working_days",
                "present_days": "$_stats.present_days",
                "leave_days": "$_stats.leave_days",
                "late_count": "$_stats.late_count",
                "total_late_minutes": "$_stats.total_late_minutes",
                "total_overtime_minutes": "$_stats.total_overtime_minutes",
                "attendance_pct": {
                    "$cond": [
                        {"$eq": ["$_working_days", 0]},
                        None,
                        {
                            "$toDouble": {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        {
                                                            "$divide": [
                                                                {
                                                                    "$toDecimal": "$_stats.present_days"
                                                                },
                                                                {
                                                                    "$toDecimal": "$_working_days"
                                                                },
                                                            ]
                                                        },
                                                        Decimal128("1000000"),
                                                    ]
                                                },
                                                Decimal128("0.5"),
                                            ]
                                        }
                                    },
                                    Decimal128("10000"),
                                ]
                            }
                        },
                    ]
                },
            }
        },
    ]
    summary = next(db.employees.aggregate(pipeline), None)
    if summary is None:
        raise HTTPException(status_code=404, detail="employee not found")
    return summary


@app.get(
    "/analytics/leaderboard/late",
    response_model=LateLeaderboardResponse,
    responses={422: {"model": ValidationErrorResponse, "description": "Validation error"}},
)
def late_leaderboard(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    limit: int = Query(default=10, ge=1, le=50),
    department: Optional[str] = None,
):
    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["query", "month"],
                    "msg": "month must be a valid YYYY-MM calendar month",
                    "type": "value_error",
                }
            ],
        ) from exc

    month_start_text = month_start.isoformat()
    month_end_text = date(
        month_start.year,
        month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1],
    ).isoformat()
    pipeline = [
        {
            "$match": {
                "date": {"$gte": month_start_text, "$lte": month_end_text}
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
                "localField": "_id",
                "foreignField": "emp_code",
                "pipeline": [
                    {"$project": {"_id": 0, "name": 1, "department": 1}}
                ],
                "as": "_employee",
            }
        },
        {"$unwind": "$_employee"},
    ]
    if department is not None:
        pipeline.append({"$match": {"_employee.department": department}})
    pipeline.extend(
        [
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
                    "name": "$_employee.name",
                    "department": "$_employee.department",
                    "total_late_minutes": 1,
                    "late_count": 1,
                }
            },
        ]
    )
    items = list(db.attendance_logs.aggregate(pipeline))
    return {"month": month, "items": items}


@app.get(
    "/analytics/departments/{department}/trend",
    response_model=DepartmentTrendResponse,
    responses={
        404: {"model": ErrorResponse, "description": "Not found"},
        422: {"model": ValidationErrorResponse, "description": "Validation error"},
    },
)
def department_trend(
    department: str,
    from_date: date = Query(alias="from"),
    to_date: date = Query(alias="to"),
):
    day_count = (to_date - from_date).days + 1
    if day_count > 92:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["query", "to"],
                    "msg": "date range must not exceed 92 days",
                    "type": "value_error",
                }
            ],
        )
    if to_date < from_date:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["query", "to"],
                    "msg": "to must be on or after from",
                    "type": "value_error",
                }
            ],
        )

    from_text = from_date.isoformat()
    to_text = to_date.isoformat()
    pipeline = [
        {"$match": {"department": department}},
        {
            "$group": {
                "_id": None,
                "employees": {
                    "$push": {
                        "emp_code": "$emp_code",
                        "joined_on": "$joined_on",
                    }
                },
            }
        },
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {
                    "employee_codes": {
                        "$map": {
                            "input": "$employees",
                            "as": "employee",
                            "in": "$$employee.emp_code",
                        }
                    }
                },
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$in": ["$emp_code", "$$employee_codes"]},
                                    {"$gte": ["$date", from_text]},
                                    {"$lte": ["$date", to_text]},
                                ]
                            }
                        }
                    },
                    {
                        "$group": {
                            "_id": "$date",
                            "present_count": {
                                "$sum": {
                                    "$cond": [
                                        {
                                            "$in": [
                                                "$status",
                                                ["PRESENT", "WFH", "ON_DUTY"],
                                            ]
                                        },
                                        {
                                            "$cond": [
                                                {"$ifNull": ["$half_day", False]},
                                                Decimal128("0.5"),
                                                Decimal128("1"),
                                            ]
                                        },
                                        Decimal128("0"),
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
                ],
                "as": "_daily_stats",
            }
        },
        {
            "$set": {
                "_trend_dates": {
                    "$map": {
                        "input": {
                            "$range": [
                                0,
                                {
                                    "$add": [
                                        {
                                            "$dateDiff": {
                                                "startDate": {
                                                    "$dateFromString": {
                                                        "dateString": from_text
                                                    }
                                                },
                                                "endDate": {
                                                    "$dateFromString": {
                                                        "dateString": to_text
                                                    }
                                                },
                                                "unit": "day",
                                            }
                                        },
                                        1,
                                    ]
                                },
                            ]
                        },
                        "as": "day_offset",
                        "in": {
                            "$dateAdd": {
                                "startDate": {
                                    "$dateFromString": {"dateString": from_text}
                                },
                                "unit": "day",
                                "amount": "$$day_offset",
                            }
                        },
                    }
                }
            }
        },
        {"$unwind": "$_trend_dates"},
        {
            "$set": {
                "date": {
                    "$dateToString": {
                        "format": "%Y-%m-%d",
                        "date": "$_trend_dates",
                        "timezone": "UTC",
                    }
                },
                "is_working_day": {
                    "$and": [
                        {"$gte": [{"$dayOfWeek": "$_trend_dates"}, 2]},
                        {"$lte": [{"$dayOfWeek": "$_trend_dates"}, 6]},
                    ]
                },
                "headcount": {
                    "$size": {
                        "$filter": {
                            "input": "$employees",
                            "as": "employee",
                            "cond": {
                                "$lte": ["$$employee.joined_on", {
                                    "$dateToString": {
                                        "format": "%Y-%m-%d",
                                        "date": "$_trend_dates",
                                        "timezone": "UTC",
                                    }
                                }]
                            },
                        }
                    }
                },
                "_day_stats": {
                    "$arrayElemAt": [
                        {
                            "$filter": {
                                "input": "$_daily_stats",
                                "as": "daily_stat",
                                "cond": {
                                    "$eq": [
                                        "$$daily_stat._id",
                                        {
                                            "$dateToString": {
                                                "format": "%Y-%m-%d",
                                                "date": "$_trend_dates",
                                                "timezone": "UTC",
                                            }
                                        },
                                    ]
                                },
                            }
                        },
                        0,
                    ]
                },
            }
        },
        {
            "$set": {
                "present_count": {
                    "$toDouble": {
                        "$ifNull": ["$_day_stats.present_count", Decimal128("0")]
                    }
                },
                "late_count": {"$ifNull": ["$_day_stats.late_count", 0]},
            }
        },
        {
            "$set": {
                "attendance_rate": {
                    "$cond": [
                        {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
                        {
                            "$divide": [
                                {
                                    "$floor": {
                                        "$add": [
                                            {
                                                "$multiply": [
                                                    {
                                                        "$divide": [
                                                            {
                                                                "$toDecimal": "$present_count"
                                                            },
                                                            {
                                                                "$toDecimal": "$headcount"
                                                            },
                                                        ]
                                                    },
                                                    Decimal128("10000"),
                                                ]
                                            },
                                            Decimal128("0.5"),
                                        ]
                                    }
                                },
                                Decimal128("10000"),
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
                    "_moving_avg_raw": {
                        "$avg": "$attendance_rate",
                        "window": {"documents": [-6, 0]},
                    }
                },
            }
        },
        {
            "$project": {
                "_id": 0,
                "date": 1,
                "is_working_day": 1,
                "headcount": 1,
                "present_count": 1,
                "late_count": 1,
                "attendance_rate": {"$toDouble": "$attendance_rate"},
                "moving_avg_7d": {
                    "$cond": [
                        {"$eq": ["$_moving_avg_raw", None]},
                        None,
                        {
                            "$toDouble": {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        "$_moving_avg_raw",
                                                        Decimal128("10000"),
                                                    ]
                                                },
                                                Decimal128("0.5"),
                                            ]
                                        }
                                    },
                                    Decimal128("10000"),
                                ]
                            }
                        },
                    ]
                },
            }
        },
        {"$sort": {"date": 1}},
        {
            "$group": {
                "_id": None,
                "items": {"$push": "$$ROOT"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "department": {"$literal": department},
                "items": 1,
            }
        },
    ]
    result = next(db.employees.aggregate(pipeline), None)
    if result is None:
        raise HTTPException(status_code=404, detail="department not found")
    return result


@app.get(
    "/analytics/departments/summary",
    response_model=DepartmentSummaryResponse,
    responses={422: {"model": ValidationErrorResponse, "description": "Validation error"}},
)
def department_summary(
    month: str = Query(pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
):
    try:
        month_start = date.fromisoformat(f"{month}-01")
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["query", "month"],
                    "msg": "month must be a valid YYYY-MM calendar month",
                    "type": "value_error",
                }
            ],
        ) from exc

    month_start_text = month_start.isoformat()
    month_end_text = date(
        month_start.year,
        month_start.month,
        calendar.monthrange(month_start.year, month_start.month)[1],
    ).isoformat()
    employee_filter = {"joined_on": {"$lte": month_end_text}}
    if department is not None:
        employee_filter["department"] = department

    present_weekday_condition = {
        "$and": [
            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            {
                "$gte": [
                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    2,
                ]
            },
            {
                "$lte": [
                    {"$dayOfWeek": {"$dateFromString": {"dateString": "$date"}}},
                    6,
                ]
            },
        ]
    }
    present_with_hours_condition = {
        "$and": [
            {"$in": ["$status", ["PRESENT", "WFH", "ON_DUTY"]]},
            {"$ne": [{"$ifNull": ["$work_hours", None]}, None]},
        ]
    }
    pipeline = [
        {"$match": employee_filter},
        {
            "$lookup": {
                "from": "attendance_logs",
                "let": {"employee_code": "$emp_code"},
                "pipeline": [
                    {
                        "$match": {
                            "$expr": {
                                "$and": [
                                    {"$eq": ["$emp_code", "$$employee_code"]},
                                    {"$gte": ["$date", month_start_text]},
                                    {"$lte": ["$date", month_end_text]},
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
                                        present_weekday_condition,
                                        {
                                            "$cond": [
                                                {"$ifNull": ["$half_day", False]},
                                                0.5,
                                                1,
                                            ]
                                        },
                                        0,
                                    ]
                                }
                            },
                            "work_hours_sum": {
                                "$sum": {
                                    "$cond": [
                                        present_with_hours_condition,
                                        {"$toDecimal": "$work_hours"},
                                        Decimal128("0"),
                                    ]
                                }
                            },
                            "work_hours_count": {
                                "$sum": {
                                    "$cond": [present_with_hours_condition, 1, 0]
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
                            "total_late_minutes": {
                                "$sum": {"$ifNull": ["$late_minutes", 0]}
                            },
                            "leave_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
                                }
                            },
                            "on_duty_count": {
                                "$sum": {
                                    "$cond": [{"$eq": ["$status", "ON_DUTY"]}, 1, 0]
                                }
                            },
                        }
                    },
                ],
                "as": "_monthly_stats",
            }
        },
        {
            "$unwind": {
                "path": "$_monthly_stats",
                "preserveNullAndEmptyArrays": True,
            }
        },
        {
            "$group": {
                "_id": "$department",
                "headcount": {"$sum": 1},
                "present_days": {
                    "$sum": {"$ifNull": ["$_monthly_stats.present_days", 0]}
                },
                "work_hours_sum": {
                    "$sum": {
                        "$ifNull": ["$_monthly_stats.work_hours_sum", Decimal128("0")]
                    }
                },
                "work_hours_count": {
                    "$sum": {"$ifNull": ["$_monthly_stats.work_hours_count", 0]}
                },
                "late_count": {
                    "$sum": {"$ifNull": ["$_monthly_stats.late_count", 0]}
                },
                "total_late_minutes": {
                    "$sum": {"$ifNull": ["$_monthly_stats.total_late_minutes", 0]}
                },
                "leave_count": {
                    "$sum": {"$ifNull": ["$_monthly_stats.leave_count", 0]}
                },
                "on_duty_count": {
                    "$sum": {"$ifNull": ["$_monthly_stats.on_duty_count", 0]}
                },
            }
        },
        {"$sort": {"_id": 1}},
        {
            "$project": {
                "_id": 0,
                "department": "$_id",
                "headcount": 1,
                "present_days": 1,
                "avg_work_hours": {
                    "$cond": [
                        {"$eq": ["$work_hours_count", 0]},
                        None,
                        {
                            "$toDouble": {
                                "$divide": [
                                    {
                                        "$floor": {
                                            "$add": [
                                                {
                                                    "$multiply": [
                                                        {
                                                            "$divide": [
                                                                "$work_hours_sum",
                                                                {
                                                                    "$toDecimal": "$work_hours_count"
                                                                },
                                                            ]
                                                        },
                                                        Decimal128("100"),
                                                    ]
                                                },
                                                Decimal128("0.5"),
                                            ]
                                        }
                                    },
                                    Decimal128("100"),
                                ]
                            }
                        },
                    ]
                },
                "late_count": 1,
                "total_late_minutes": 1,
                "leave_count": 1,
                "on_duty_count": 1,
            }
        },
        {
            "$group": {
                "_id": None,
                "items": {"$push": "$$ROOT"},
            }
        },
        {
            "$project": {
                "_id": 0,
                "month": {"$literal": month},
                "items": 1,
            }
        },
    ]
    result = next(db.employees.aggregate(pipeline), None)
    return result if result is not None else {"month": month, "items": []}


@app.get("/attendance")
def list_attendance(
    emp_code: Optional[str] = None,
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    if date_from is not None and date_to is not None and date_from > date_to:
        raise HTTPException(status_code=422, detail="date_from must be on or before date_to")
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

    total = db.attendance_logs.count_documents(query)
    docs = (
        db.attendance_logs.find(query, {"_id": 0})
        .sort([("date", DESCENDING), ("emp_code", ASCENDING)])
        .skip((page - 1) * page_size)
        .limit(page_size)
    )
    return {
        "items": [serialize_attendance(doc) for doc in docs],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@app.get(
    "/admin/explain/{endpoint}",
    response_model=ExplainResponse,
    responses={422: {"model": ValidationErrorResponse, "description": "Validation error"}},
)
def admin_explain(
    endpoint: Literal[
        "attendance_list",
        "employee_monthly",
        "department_summary",
        "late_leaderboard",
        "department_trend",
    ],
    emp_code: Optional[str] = None,
    month: Optional[str] = Query(default=None, pattern=r"^\d{4}-(0[1-9]|1[0-2])$"),
    department: Optional[str] = None,
    limit: int = Query(default=10, ge=1, le=50),
    date_from: Optional[date] = None,
    date_to: Optional[date] = None,
    status: Optional[Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]] = None,
    from_date: Optional[date] = Query(default=None, alias="from"),
    to_date: Optional[date] = Query(default=None, alias="to"),
    page: int = Query(default=1, ge=1),
    page_size: int = Query(default=20, ge=1, le=100),
):
    def missing_parameter(name: str):
        raise HTTPException(
            status_code=422,
            detail=[
                {
                    "loc": ["query", name],
                    "msg": f"{name} is required for endpoint {endpoint}",
                    "type": "value_error",
                }
            ],
        )

    def parsed_month():
        if month is None:
            missing_parameter("month")
        try:
            start = date.fromisoformat(f"{month}-01")
        except ValueError as exc:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "loc": ["query", "month"],
                        "msg": "month must be a valid YYYY-MM calendar month",
                        "type": "value_error",
                    }
                ],
            ) from exc
        end_exclusive = (start.replace(day=28) + timedelta(days=4)).replace(day=1)
        end_inclusive = end_exclusive - timedelta(days=1)
        return start.isoformat(), end_exclusive.isoformat(), end_inclusive.isoformat()

    collection_name: str
    operation: dict[str, Any]

    if endpoint == "attendance_list":
        if date_from is not None and date_to is not None and date_from > date_to:
            raise HTTPException(
                status_code=422, detail="date_from must be on or before date_to"
            )
        query: dict[str, Any] = {}
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
        collection_name = "attendance_logs"
        operation = {
            "find": collection_name,
            "filter": query,
            "sort": {"date": DESCENDING, "emp_code": ASCENDING},
            "skip": (page - 1) * page_size,
            "limit": page_size,
        }
    elif endpoint == "employee_monthly":
        if emp_code is None:
            missing_parameter("emp_code")
        month_start, month_end_exclusive, _ = parsed_month()
        collection_name = "attendance_logs"
        operation = {
            "aggregate": collection_name,
            "pipeline": [
                {
                    "$match": {
                        "emp_code": emp_code,
                        "date": {
                            "$gte": month_start,
                            "$lt": month_end_exclusive,
                        },
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
                                                    ["PRESENT", "WFH", "ON_DUTY"],
                                                ]
                                            },
                                            {
                                                "$gte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$date"
                                                            }
                                                        }
                                                    },
                                                    2,
                                                ]
                                            },
                                            {
                                                "$lte": [
                                                    {
                                                        "$dayOfWeek": {
                                                            "$dateFromString": {
                                                                "dateString": "$date"
                                                            }
                                                        }
                                                    },
                                                    6,
                                                ]
                                            },
                                        ]
                                    },
                                    {
                                        "$cond": [
                                            {"$ifNull": ["$half_day", False]},
                                            0.5,
                                            1,
                                        ]
                                    },
                                    0,
                                ]
                            }
                        },
                        "leave_days": {
                            "$sum": {
                                "$cond": [{"$eq": ["$status", "LEAVE"]}, 1, 0]
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
                        "total_late_minutes": {
                            "$sum": {"$ifNull": ["$late_minutes", 0]}
                        },
                        "total_overtime_minutes": {
                            "$sum": {"$ifNull": ["$overtime_minutes", 0]}
                        },
                    }
                },
            ],
            "cursor": {},
        }
    elif endpoint == "department_summary":
        _, _, month_end = parsed_month()
        employee_query: dict[str, Any] = {"joined_on": {"$lte": month_end}}
        if department is not None:
            employee_query["department"] = department
        collection_name = "employees"
        operation = {
            "aggregate": collection_name,
            "pipeline": [
                {"$match": employee_query},
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {
                            "employee_code": "$emp_code",
                            "month_start": parsed_month()[0],
                            "month_end": month_end,
                        },
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {"$eq": ["$emp_code", "$$employee_code"]},
                                            {"$gte": ["$date", "$$month_start"]},
                                            {"$lte": ["$date", "$$month_end"]},
                                        ]
                                    }
                                }
                            }
                        ],
                        "as": "_monthly_stats",
                    }
                },
            ],
            "cursor": {},
        }
    elif endpoint == "late_leaderboard":
        month_start, _, month_end = parsed_month()
        collection_name = "attendance_logs"
        pipeline = [
            {"$match": {"date": {"$gte": month_start, "$lte": month_end}}},
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
                    "localField": "_id",
                    "foreignField": "emp_code",
                    "pipeline": [
                        {"$project": {"_id": 0, "name": 1, "department": 1}}
                    ],
                    "as": "_employee",
                }
            },
            {"$unwind": "$_employee"},
        ]
        if department is not None:
            pipeline.append({"$match": {"_employee.department": department}})
        pipeline.extend(
            [
                {
                    "$setWindowFields": {
                        "sortBy": {"total_late_minutes": -1},
                        "output": {"rank": {"$rank": {}}},
                    }
                },
                {"$match": {"rank": {"$lte": limit}}},
                {"$sort": {"total_late_minutes": -1, "_id": 1}},
            ]
        )
        operation = {
            "aggregate": collection_name,
            "pipeline": pipeline,
            "cursor": {},
        }
    else:
        if department is None:
            missing_parameter("department")
        if from_date is None:
            missing_parameter("from")
        if to_date is None:
            missing_parameter("to")
        if to_date < from_date:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "loc": ["query", "to"],
                        "msg": "to must be on or after from",
                        "type": "value_error",
                    }
                ],
            )
        if (to_date - from_date).days + 1 > 92:
            raise HTTPException(
                status_code=422,
                detail=[
                    {
                        "loc": ["query", "to"],
                        "msg": "date range must not exceed 92 days",
                        "type": "value_error",
                    }
                ],
            )
        collection_name = "employees"
        operation = {
            "aggregate": collection_name,
            "pipeline": [
                {"$match": {"department": department}},
                {
                    "$group": {
                        "_id": None,
                        "employees": {
                            "$push": {
                                "emp_code": "$emp_code",
                                "joined_on": "$joined_on",
                            }
                        },
                    }
                },
                {
                    "$lookup": {
                        "from": "attendance_logs",
                        "let": {
                            "employee_codes": {
                                "$map": {
                                    "input": "$employees",
                                    "as": "employee",
                                    "in": "$$employee.emp_code",
                                }
                            }
                        },
                        "pipeline": [
                            {
                                "$match": {
                                    "$expr": {
                                        "$and": [
                                            {
                                                "$in": [
                                                    "$emp_code",
                                                    "$$employee_codes",
                                                ]
                                            },
                                            {"$gte": ["$date", from_date.isoformat()]},
                                            {"$lte": ["$date", to_date.isoformat()]},
                                        ]
                                    }
                                }
                            },
                            {
                                "$group": {
                                    "_id": "$date",
                                    "present_count": {
                                        "$sum": {
                                            "$cond": [
                                                {
                                                    "$in": [
                                                        "$status",
                                                        ["PRESENT", "WFH", "ON_DUTY"],
                                                    ]
                                                },
                                                {
                                                    "$cond": [
                                                        {
                                                            "$ifNull": [
                                                                "$half_day",
                                                                False,
                                                            ]
                                                        },
                                                        Decimal128("0.5"),
                                                        Decimal128("1"),
                                                    ]
                                                },
                                                Decimal128("0"),
                                            ]
                                        }
                                    },
                                    "late_count": {
                                        "$sum": {
                                            "$cond": [
                                                {
                                                    "$gt": [
                                                        {
                                                            "$ifNull": [
                                                                "$late_minutes",
                                                                0,
                                                            ]
                                                        },
                                                        0,
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
                        "as": "_daily_stats",
                    }
                },
            ],
            "cursor": {},
        }

    try:
        explain_result = db.command(
            "explain", operation, verbosity="executionStats"
        )
    except PyMongoError as exc:
        raise HTTPException(status_code=503, detail="MongoDB explain unavailable") from exc
    explain_result.pop("serverInfo", None)
    return {
        "endpoint": endpoint,
        "collection": collection_name,
        "explain": explain_result,
    }
