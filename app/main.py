"""Attendance contract v2: UTC instants, IST days, atomic writes, DB analytics."""
import calendar
import os
import re
from contextlib import asynccontextmanager
from datetime import date as Date, datetime, time, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Annotated, Literal

from bson import ObjectId
from bson.decimal128 import Decimal128
from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Query
from pydantic import AfterValidator, BaseModel, ConfigDict, Field, model_validator
from pymongo import ASCENDING, DESCENDING, MongoClient, ReturnDocument
from pymongo.errors import DuplicateKeyError, PyMongoError

load_dotenv(override=False)
UTC = timezone.utc
IST = timezone(timedelta(hours=5, minutes=30))
PRESENCE = ["PRESENT", "WFH", "ON_DUTY"]
Status = Literal["PRESENT", "ABSENT", "LEAVE", "WFH", "ON_DUTY"]
PresenceStatus = Literal["PRESENT", "WFH", "ON_DUTY"]
EpochMillis = Annotated[int, Field(strict=True, ge=100000000000, le=4102444800000)]
MUTABLE = ("status", "punch_in", "punch_out", "work_hours", "late_minutes",
           "overtime_minutes", "half_day")
ATTENDANCE_FIELDS = ("emp_code", "date") + MUTABLE + ("history",)
EMPLOYEE_FIELDS = ("emp_code", "name", "email", "department", "shift_start",
                   "shift_end", "joined_on", "created_at")

client = MongoClient(os.getenv("MONGO_URI", "mongodb://localhost:27017"),
                     tz_aware=True, tzinfo=UTC, serverSelectionTimeoutMS=3000,
                     connectTimeoutMS=3000)
db = client[os.getenv("MONGO_DB", "attendance_db")]


def indexes():
    """Unique natural keys also enforce concurrency; the others serve reads."""
    db.employees.create_index([("emp_code", ASCENDING)], unique=True, name="emp_unique")
    db.employees.create_index([("department", ASCENDING), ("emp_code", ASCENDING)],
                              name="emp_department_code")
    db.employees.create_index([("joined_on", ASCENDING), ("department", ASCENDING)],
                              name="emp_join_department")
    db.employees.create_index([("department", ASCENDING), ("joined_on", ASCENDING)],
                              name="emp_department_join")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", ASCENDING)],
                                    unique=True, name="log_unique")
    db.attendance_logs.create_index([("date", DESCENDING), ("emp_code", ASCENDING)],
                                    name="log_date_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("date", DESCENDING)],
                                    name="log_employee_date")
    db.attendance_logs.create_index([("status", ASCENDING), ("date", DESCENDING),
                                    ("emp_code", ASCENDING)], name="log_status_date_code")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("status", ASCENDING),
                                    ("date", DESCENDING)], name="log_employee_status_date")
    db.attendance_logs.create_index([("emp_code", ASCENDING), ("punch_in", DESCENDING)],
                                    name="log_employee_punch")


@asynccontextmanager
async def lifespan(_app):
    try:
        indexes()
    except PyMongoError:
        # Keep health available (503) if MongoDB is unavailable at startup.
        # A later successful health probe retries idempotent index creation.
        _app.state.indexes_ready = False
    else:
        _app.state.indexes_ready = True
    yield
    client.close()


app = FastAPI(title="Employee Attendance & Analytics API", version="2.0.0",
              lifespan=lifespan)


def invalid(message, field="body"):
    raise HTTPException(422, detail=[{"type": "value_error", "loc": [field],
                                     "msg": message, "input": None}])


def calendar_date(value):
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", value):
        raise ValueError("Use YYYY-MM-DD")
    Date.fromisoformat(value)
    return value


def calendar_month(value):
    if not re.fullmatch(r"[0-9]{4}-(0[1-9]|1[0-2])", value):
        raise ValueError("Use YYYY-MM")
    Date.fromisoformat(value + "-01")
    return value


CalendarDate = Annotated[str, AfterValidator(calendar_date)]
CalendarMonth = Annotated[str, AfterValidator(calendar_month)]
Page = Annotated[int, Query(ge=1)]
PageSize = Annotated[int, Query(ge=1, le=100)]
LeaderboardLimit = Annotated[int, Query(ge=1, le=50)]


def now_ms():
    return int(datetime.now(UTC).timestamp()) * 1000


def instant(value):
    # Integer division, not floating timestamp arithmetic, truncates before rules.
    return datetime.fromtimestamp(value // 1000, UTC)


def whole_seconds(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).replace(microsecond=0)


def json_value(value):
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return int(value.timestamp() * 1000)
    if isinstance(value, (ObjectId,)):
        return str(value)
    if isinstance(value, Decimal128):
        return float(value.to_decimal())
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, dict):
        return {k: json_value(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(v) for v in value]
    return value


def employee_response(doc):
    return json_value({k: doc[k] for k in EMPLOYEE_FIELDS})


def attendance_response(doc):
    defaults = {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False,
                "history": []}
    return json_value({k: doc.get(k, defaults.get(k)) for k in ATTENDANCE_FIELDS})


def employee(emp_code):
    doc = db.employees.find_one({"emp_code": emp_code})
    if doc is None:
        raise HTTPException(404, "Unknown employee")
    return doc


def attendance_date(punch, emp):
    local = punch.astimezone(IST)
    day = local.date()
    if emp["shift_end"] <= emp["shift_start"] and local.time() < time.fromisoformat(emp["shift_end"]):
        day -= timedelta(days=1)
    return day.isoformat()


def shift_bounds(date_str, emp):
    start = datetime.combine(Date.fromisoformat(date_str),
                             time.fromisoformat(emp["shift_start"]), IST)
    end = datetime.combine(Date.fromisoformat(date_str),
                           time.fromisoformat(emp["shift_end"]), IST)
    if emp["shift_end"] <= emp["shift_start"]:
        end += timedelta(days=1)
    return start, end


def compute_late_minutes(punch_in, shift_start, date_str=None):
    punch_in = whole_seconds(punch_in)
    local = punch_in.astimezone(IST)
    start = datetime.combine(Date.fromisoformat(date_str) if date_str else local.date(),
                             time.fromisoformat(shift_start), IST)
    seconds = int((punch_in - start).total_seconds())
    return seconds // 60 if seconds > 600 else 0


def compute_work_hours(punch_in, punch_out):
    seconds = int((whole_seconds(punch_out) - whole_seconds(punch_in)).total_seconds())
    return float((Decimal(seconds) / Decimal(3600)).quantize(Decimal("0.01"),
                                                           rounding=ROUND_HALF_UP))


def compute_overtime(punch_out, shift_end, date_str, shift_start="09:30"):
    _, end = shift_bounds(date_str, {"shift_start": shift_start, "shift_end": shift_end})
    minutes = int((whole_seconds(punch_out) - end).total_seconds()) // 60
    return minutes if minutes >= 30 else 0


def duration_valid(punch_in, punch_out):
    seconds = (punch_out - punch_in).total_seconds()
    if not 0 < seconds <= 86400:
        invalid("punch_out must be after punch_in and within 24 hours")


def derived(doc, emp):
    if doc["status"] not in PRESENCE:
        return {"punch_in": None, "punch_out": None, "work_hours": None,
                "late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    pi = doc.get("punch_in")
    if pi is None:
        invalid("A presence status requires punch_in")
    pi = whole_seconds(pi)
    if attendance_date(pi, emp) != doc["date"]:
        invalid("punch_in must stay on the attendance date")
    po = doc.get("punch_out")
    po = whole_seconds(po) if po is not None else None
    hours, overtime, half = None, 0, False
    if po is not None:
        duration_valid(pi, po)
        hours = compute_work_hours(pi, po)
        overtime = compute_overtime(po, emp["shift_end"], doc["date"], emp["shift_start"])
        half = hours < 4.50
    return {"punch_in": pi, "punch_out": po, "work_hours": hours,
            "late_minutes": compute_late_minutes(pi, emp["shift_start"], doc["date"]),
            "overtime_minutes": overtime, "half_day": half}


def unchanged_snapshot(doc):
    # Compare against the exact snapshot, including absence of legacy fields.
    return {"_id": doc["_id"], **{k: doc[k] if k in doc else {"$exists": False}
                                 for k in MUTABLE + ("history",)}}


class InputModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class EmployeeIn(InputModel):
    emp_code: Annotated[str, Field(pattern=r"^EMP[0-9]{4,6}$")]
    name: Annotated[str, Field(min_length=1, max_length=100)]
    email: Annotated[str, Field(max_length=120, pattern=r"^[^@\s]+@[^@\s]+\.[^@\s]+$")]
    department: Annotated[str, Field(min_length=1, max_length=50)]
    shift_start: Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")] = "09:30"
    shift_end: Annotated[str, Field(pattern=r"^([01][0-9]|2[0-3]):[0-5][0-9]$")] = "18:30"
    joined_on: CalendarDate

    @model_validator(mode="after")
    def distinct_shift(self):
        if self.shift_start == self.shift_end:
            raise ValueError("shift_start must differ from shift_end")
        return self


class PunchInIn(InputModel):
    emp_code: str
    punched_at: EpochMillis = Field(default_factory=now_ms)
    status: PresenceStatus = "PRESENT"


class PunchOutIn(InputModel):
    emp_code: str
    punched_at: EpochMillis = Field(default_factory=now_ms)


class RegularizeIn(InputModel):
    # Defaults represent omission; explicit null fails the non-nullable types.
    status: Status = Field(default=None)
    punch_in: EpochMillis = Field(default=None)
    punch_out: EpochMillis = Field(default=None)
    reason: Annotated[str, Field(min_length=5, max_length=200)]
    regularized_by: Annotated[str, Field(min_length=1, max_length=50)]


@app.get("/health")
def health():
    try:
        db.command("ping")
        if not getattr(app.state, "indexes_ready", False):
            indexes()
            app.state.indexes_ready = True
    except PyMongoError:
        raise HTTPException(503, "MongoDB unavailable")
    return {"status": "ok"}


@app.post("/employees", status_code=201)
def create_employee(body: EmployeeIn):
    doc = body.model_dump()
    doc["created_at"] = instant(now_ms())
    try:
        db.employees.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "emp_code already exists")
    return employee_response(doc)


@app.get("/employees")
def list_employees(department: str | None = None, page: Page = 1, page_size: PageSize = 20):
    q = {"department": department} if department is not None else {}
    hint = "emp_department_code" if department is not None else "emp_unique"
    items = db.employees.find(q).hint(hint).sort("emp_code", 1).skip((page - 1) * page_size).limit(page_size)
    return {"items": [employee_response(d) for d in items],
            "total": db.employees.count_documents(q, hint=hint),
            "page": page, "page_size": page_size}


@app.post("/attendance/punch-in", status_code=201)
def punch_in(body: PunchInIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    day = attendance_date(ts, emp)
    doc = {"emp_code": body.emp_code, "date": day, "status": body.status,
           "punch_in": ts, "punch_out": None, "work_hours": None,
           "late_minutes": compute_late_minutes(ts, emp["shift_start"], day),
           "overtime_minutes": 0, "half_day": False, "history": []}
    try:
        db.attendance_logs.insert_one(doc)
    except DuplicateKeyError:
        raise HTTPException(409, "Already punched in for this date")
    return attendance_response(doc)


@app.post("/attendance/punch-out")
def punch_out(body: PunchOutIn):
    emp = employee(body.emp_code)
    ts = instant(body.punched_at)
    doc = db.attendance_logs.find_one({"emp_code": body.emp_code,
                                      "punch_in": {"$type": "date", "$lte": ts}},
                                     sort=[("punch_in", -1)], hint="log_employee_punch")
    if doc is None:
        raise HTTPException(404, "No punch-in found")
    if doc.get("punch_out") is not None:
        raise HTTPException(409, "Already punched out")
    duration_valid(whole_seconds(doc["punch_in"]), ts)
    updated = {**doc, "punch_out": ts}
    values = derived(updated, emp)
    result = db.attendance_logs.find_one_and_update(unchanged_snapshot(doc),
                {"$set": values}, return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "Record changed concurrently")
    return attendance_response(result)


def attendance_query(emp_code, date_from, date_to, status):
    if date_from is not None and date_to is not None and date_from > date_to:
        invalid("date_from must be on or before date_to", "query")
    q = {}
    if emp_code is not None:
        q["emp_code"] = emp_code
    if status is not None:
        q["status"] = status
    if date_from is not None or date_to is not None:
        q["date"] = {}
        if date_from is not None:
            q["date"]["$gte"] = date_from
        if date_to is not None:
            q["date"]["$lte"] = date_to
    hint = ("log_employee_status_date" if emp_code is not None and status is not None
            else "log_employee_date" if emp_code is not None
            else "log_status_date_code" if status is not None else "log_date_code")
    return q, hint


ATTENDANCE_SORT = [("date", -1), ("emp_code", 1)]


@app.get("/attendance")
def list_attendance(emp_code: str | None = None, date_from: CalendarDate | None = None,
                    date_to: CalendarDate | None = None, status: Status | None = None,
                    page: Page = 1, page_size: PageSize = 20):
    q, hint = attendance_query(emp_code, date_from, date_to, status)
    items = db.attendance_logs.find(q).hint(hint).sort(ATTENDANCE_SORT).skip((page - 1) * page_size).limit(page_size)
    return {"items": [attendance_response(d) for d in items],
            "total": db.attendance_logs.count_documents(q, hint=hint),
            "page": page, "page_size": page_size}


@app.patch("/attendance/{emp_code}/{date}")
def regularize(emp_code: str, date: CalendarDate, body: RegularizeIn):
    emp = employee(emp_code)
    doc = db.attendance_logs.find_one({"emp_code": emp_code, "date": date})
    if doc is None:
        raise HTTPException(404, "No record for this date")
    supplied = body.model_fields_set
    if body.status in ("ABSENT", "LEAVE") and supplied & {"punch_in", "punch_out"}:
        invalid("ABSENT/LEAVE cannot be supplied with punch times")
    final = dict(doc)
    if "status" in supplied:
        final["status"] = body.status
    for field in ("punch_in", "punch_out"):
        if field in supplied:
            final[field] = instant(getattr(body, field))
    final.update(derived(final, emp))
    defaults = {"late_minutes": 0, "overtime_minutes": 0, "half_day": False}
    changes = {k: {"from": doc.get(k, defaults.get(k)), "to": final[k]}
               for k in MUTABLE if doc.get(k, defaults.get(k)) != final[k]}
    if not changes:
        invalid("The request changes nothing")
    entry = {"at": instant(now_ms()), "by": body.regularized_by,
             "reason": body.reason, "changes": changes}
    result = db.attendance_logs.find_one_and_update(unchanged_snapshot(doc),
                {"$set": {k: final[k] for k in MUTABLE}, "$push": {"history": entry}},
                return_document=ReturnDocument.AFTER)
    if result is None:
        raise HTTPException(409, "Record changed concurrently; retry against the new record")
    return attendance_response(result)


# Aggregation expressions. All counting, joining, averaging and gap filling run
# inside MongoDB. Python only constructs pipelines and serializes their results.
def month_bounds(month):
    year, number = map(int, month.split("-"))
    return month + "-01", f"{month}-{calendar.monthrange(year, number)[1]:02d}"


def mongo_day(expr):
    return {"$dateFromString": {"dateString": expr, "format": "%Y-%m-%d", "timezone": "+05:30"}}


def weekday(expr):
    return {"$in": [{"$dayOfWeek": {"date": mongo_day(expr), "timezone": "+05:30"}}, [2, 3, 4, 5, 6]]}


def presence_weight():
    return {"$cond": [{"$in": ["$status", PRESENCE]},
                      {"$cond": [{"$ifNull": ["$half_day", False]}, 0.5, 1]}, 0]}


def sum_if(condition):
    return {"$sum": {"$cond": [condition, 1, 0]}}


def half_up(expr, places):
    # MongoDB $round uses ties-to-even. Decimal floor(x*scale + .5) implements
    # half-up for the nonnegative values reported by these endpoints.
    scale = Decimal128(str(10 ** places))
    return {"$cond": [{"$eq": [{"$ifNull": [expr, None]}, None]}, None,
                      {"$toDouble": {"$divide": [
                          {"$floor": {"$add": [{"$multiply": [{"$toDecimal": expr}, scale]}, Decimal128("0.5")]}},
                          scale]}}]}


def monthly_log_totals(first, last):
    return [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": None,
            "present_days": {"$sum": {"$cond": [weekday("$date"), presence_weight(), 0]}},
            "leave_days": sum_if({"$eq": ["$status", "LEAVE"]}),
            "late_count": sum_if({"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]}),
            "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
            "total_overtime_minutes": {"$sum": {"$ifNull": ["$overtime_minutes", 0]}},
            "on_duty_count": sum_if({"$eq": ["$status", "ON_DUTY"]}),
            "hours_sum": {"$sum": {"$cond": [{"$in": ["$status", PRESENCE]}, {"$toDecimal": "$work_hours"}, 0]}},
            "hours_count": sum_if({"$and": [{"$in": ["$status", PRESENCE]},
                                             {"$ne": [{"$ifNull": ["$work_hours", None]}, None]}]})}}
    ]


def monthly_lookup(first, last):
    return {"$lookup": {"from": "attendance_logs", "localField": "emp_code",
                        "foreignField": "emp_code", "pipeline": monthly_log_totals(first, last),
                        "as": "totals"}}


def employee_monthly_pipeline(emp_code, month):
    first, last = month_bounds(month)
    days = int(last[-2:])
    fields = ("present_days", "leave_days", "late_count", "total_late_minutes", "total_overtime_minutes")
    return "employees", "emp_unique", [
        {"$match": {"emp_code": emp_code}}, monthly_lookup(first, last),
        {"$set": {"totals": {"$arrayElemAt": ["$totals", 0]}}},
        {"$set": {"working_days": {"$size": {"$filter": {
            "input": {"$map": {"input": {"$range": [0, days]}, "as": "n", "in":
                {"$dateToString": {"date": {"$dateAdd": {"startDate": mongo_day(first), "unit": "day", "amount": "$$n"}},
                                    "format": "%Y-%m-%d", "timezone": "+05:30"}}}},
            "as": "day", "cond": {"$and": [{"$gte": ["$$day", "$joined_on"]}, weekday("$$day")]}}}},
            **{k: {"$ifNull": ["$totals." + k, 0]} for k in fields}}},
        {"$project": {"_id": 0, "emp_code": 1, "month": {"$literal": month}, "working_days": 1,
            **{k: 1 for k in fields}, "attendance_pct": {"$cond": [{"$gt": ["$working_days", 0]},
                half_up({"$multiply": [{"$divide": [{"$toDecimal": "$present_days"}, "$working_days"]}, 100]}, 2), None]}}}
    ]


def department_summary_pipeline(month, department):
    first, last = month_bounds(month)
    q = {"joined_on": {"$lte": last}}
    if department is not None:
        q["department"] = department
    sums = {"present_days": "present_days", "late_count": "late_count",
            "total_late_minutes": "total_late_minutes", "leave_count": "leave_days",
            "on_duty_count": "on_duty_count", "hours_sum": "hours_sum", "hours_count": "hours_count"}
    return "employees", "emp_department_join" if department is not None else "emp_join_department", [
        {"$match": q}, monthly_lookup(first, last),
        {"$set": {"totals": {"$arrayElemAt": ["$totals", 0]}}},
        {"$group": {"_id": "$department", "headcount": {"$sum": 1},
            **{out: {"$sum": {"$ifNull": ["$totals." + source, 0]}} for out, source in sums.items()}}},
        {"$project": {"_id": 0, "department": "$_id", "headcount": 1,
            **{k: 1 for k in sums if not k.startswith("hours_")},
            "avg_work_hours": {"$cond": [{"$gt": ["$hours_count", 0]},
                                          half_up({"$divide": ["$hours_sum", "$hours_count"]}, 2), None]}}},
        {"$sort": {"department": 1}}
    ]


def late_leaderboard_pipeline(month, limit, department):
    first, last = month_bounds(month)
    pipeline = [
        {"$match": {"date": {"$gte": first, "$lte": last}}},
        {"$group": {"_id": "$emp_code", "total_late_minutes": {"$sum": {"$ifNull": ["$late_minutes", 0]}},
                    "late_count": sum_if({"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]})}},
        {"$match": {"total_late_minutes": {"$gt": 0}}},
        {"$lookup": {"from": "employees", "localField": "_id", "foreignField": "emp_code", "as": "employee"}},
        {"$unwind": "$employee"}
    ]
    if department is not None:
        pipeline.append({"$match": {"employee.department": department}})
    pipeline += [
        {"$setWindowFields": {"sortBy": {"total_late_minutes": -1}, "output": {"rank": {"$rank": {}}}}},
        {"$match": {"rank": {"$lte": limit}}},
        {"$sort": {"total_late_minutes": -1, "_id": 1}},
        {"$project": {"_id": 0, "rank": 1, "emp_code": "$_id", "name": "$employee.name",
                      "department": "$employee.department", "total_late_minutes": 1, "late_count": 1}}
    ]
    return "attendance_logs", "log_date_code", pipeline


def trend_range(first, last):
    if first > last:
        invalid("to must be on or after from", "query")
    days = (Date.fromisoformat(last) - Date.fromisoformat(first)).days + 1
    if days > 92:
        invalid("Range must contain at most 92 calendar days", "query")
    return days


def department_trend_pipeline(department, first, last):
    days = trend_range(first, last)
    return "employees", "emp_department_join", [
        {"$match": {"department": department}},
        {"$group": {"_id": None, "join_dates": {"$push": "$joined_on"}, "codes": {"$push": "$emp_code"}}},
        # Database calendar spine works even when no attendance logs exist.
        {"$set": {"day": {"$map": {"input": {"$range": [0, days]}, "as": "n", "in":
            {"$dateAdd": {"startDate": mongo_day(first), "unit": "day", "amount": "$$n"}}}}}},
        {"$unwind": "$day"},
        {"$set": {"date": {"$dateToString": {"date": "$day", "format": "%Y-%m-%d", "timezone": "+05:30"}}}},
        {"$set": {"is_working_day": weekday("$date"), "headcount": {"$size": {"$filter": {
            "input": "$join_dates", "as": "joined", "cond": {"$lte": ["$$joined", "$date"]}}}}}},
        {"$lookup": {"from": "attendance_logs", "localField": "date", "foreignField": "date",
            "let": {"codes": "$codes"}, "pipeline": [
                {"$match": {"$expr": {"$in": ["$emp_code", "$$codes"]}}},
                {"$group": {"_id": None, "present_count": {"$sum": presence_weight()},
                            "late_count": sum_if({"$gt": [{"$ifNull": ["$late_minutes", 0]}, 0]})}}
            ], "as": "totals"}},
        {"$set": {"totals": {"$arrayElemAt": ["$totals", 0]}}},
        {"$set": {"present_count": {"$ifNull": ["$totals.present_count", 0]},
                  "late_count": {"$ifNull": ["$totals.late_count", 0]}}},
        {"$set": {"attendance_rate": {"$cond": [
            {"$and": ["$is_working_day", {"$gt": ["$headcount", 0]}]},
            half_up({"$divide": [{"$toDecimal": "$present_count"}, "$headcount"]}, 4), None]}}},
        {"$setWindowFields": {"sortBy": {"date": 1}, "output": {"moving_avg_7d": {
            "$avg": {"$toDecimal": "$attendance_rate"}, "window": {"documents": [-6, 0]}}}}},
        {"$project": {"_id": 0, "date": 1, "is_working_day": 1, "headcount": 1,
            "present_count": 1, "late_count": 1, "attendance_rate": 1,
            "moving_avg_7d": half_up("$moving_avg_7d", 4)}},
        {"$sort": {"date": 1}}
    ]


def aggregate(spec):
    collection, hint, pipeline = spec
    return json_value(list(db[collection].aggregate(pipeline, hint=hint, allowDiskUse=True)))


@app.get("/analytics/employees/{emp_code}/monthly")
def employee_monthly(emp_code: str, month: CalendarMonth):
    rows = aggregate(employee_monthly_pipeline(emp_code, month))
    if not rows:
        raise HTTPException(404, "Unknown employee")
    return rows[0]


@app.get("/analytics/departments/summary")
def department_summary(month: CalendarMonth, department: str | None = None):
    return {"month": month, "items": aggregate(department_summary_pipeline(month, department))}


@app.get("/analytics/leaderboard/late")
def late_leaderboard(month: CalendarMonth, limit: LeaderboardLimit = 10, department: str | None = None):
    return {"month": month, "items": aggregate(late_leaderboard_pipeline(month, limit, department))}


@app.get("/analytics/departments/{department}/trend")
def department_trend(department: str, from_: Annotated[CalendarDate, Query(alias="from")],
                     to: CalendarDate):
    rows = aggregate(department_trend_pipeline(department, from_, to))
    if not rows:
        raise HTTPException(404, "Unknown department")
    return {"department": department, "items": rows}


ExplainEndpoint = Literal["attendance_list", "employee_monthly", "department_summary", "late_leaderboard", "department_trend"]


@app.get("/admin/explain/{endpoint}")
def explain(endpoint: ExplainEndpoint, emp_code: str | None = None,
            month: CalendarMonth | None = None, department: str | None = None,
            limit: LeaderboardLimit = 10, date_from: CalendarDate | None = None,
            date_to: CalendarDate | None = None, status: Status | None = None,
            from_: Annotated[CalendarDate | None, Query(alias="from")] = None,
            to: CalendarDate | None = None, page: Page = 1, page_size: PageSize = 20):
    if endpoint == "attendance_list":
        q, hint = attendance_query(emp_code, date_from, date_to, status)
        collection = "attendance_logs"
        command = {"find": collection, "filter": q, "sort": dict(ATTENDANCE_SORT),
                   "skip": (page - 1) * page_size, "limit": page_size, "hint": hint}
    else:
        if endpoint == "department_trend":
            if department is None or from_ is None or to is None:
                invalid("department, from and to are required", "query")
            spec = department_trend_pipeline(department, from_, to)
        else:
            if month is None:
                invalid("month is required", "query")
            if endpoint == "employee_monthly":
                if emp_code is None:
                    invalid("emp_code is required", "query")
                spec = employee_monthly_pipeline(emp_code, month)
            elif endpoint == "department_summary":
                spec = department_summary_pipeline(month, department)
            else:
                spec = late_leaderboard_pipeline(month, limit, department)
        collection, hint, pipeline = spec
        command = {"aggregate": collection, "pipeline": pipeline, "cursor": {},
                   "hint": hint, "allowDiskUse": True}
    raw = db.command({"explain": command, "verbosity": "executionStats"})
    return {"endpoint": endpoint, "collection": collection, "explain": json_value(raw)}
