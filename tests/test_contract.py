import calendar
import os
import random
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from threading import Barrier

import pytest

IST = timezone(timedelta(hours=5, minutes=30))
UTC = timezone.utc


def ms(text):
    return int(datetime.fromisoformat(text).replace(tzinfo=IST).timestamp() * 1000)


def emp(code="EMP0001", department="Engineering", joined_on="2026-01-01", **extra):
    return {"emp_code": code, "name": code, "email": code + "@example.com",
            "department": department, "joined_on": joined_on, **extra}


def create(http, **kwargs):
    r = http.post("/employees", json=emp(**kwargs))
    assert r.status_code == 201, r.text
    return r.json()


def punch(http, code, text, status="PRESENT"):
    return http.post("/attendance/punch-in", json={"emp_code": code,
                     "punched_at": ms(text), "status": status})


def log(code, day, status="PRESENT", hours=8.0, late=0, overtime=0, half=False):
    pi = datetime.fromisoformat(day + "T09:30").replace(tzinfo=IST).astimezone(UTC)
    return {"emp_code": code, "date": day, "status": status,
            "punch_in": pi if status in ("PRESENT", "WFH", "ON_DUTY") else None,
            "punch_out": pi + timedelta(hours=hours) if hours is not None and status in ("PRESENT", "WFH", "ON_DUTY") else None,
            "work_hours": hours if status in ("PRESENT", "WFH", "ON_DUTY") else None,
            "late_minutes": late, "overtime_minutes": overtime,
            "half_day": half, "history": []}


def rounded(value, places=2):
    return float(Decimal(str(value)).quantize(Decimal(10) ** -places, rounding=ROUND_HALF_UP))


@pytest.mark.parametrize("value", [1783312500, 1783312500000.0, "1783312500000", True, None, 99999999999, 4102444800001])
def test_timestamp_rejected(clean, value):
    http, _ = clean
    create(http)
    for path in ("punch-in", "punch-out"):
        assert http.post("/attendance/" + path, json={"emp_code": "EMP0001", "punched_at": value}).status_code == 422


@pytest.mark.parametrize("changes", [
    {"emp_code": "001"}, {"name": ""}, {"department": ""}, {"email": "bad@email"},
    {"shift_start": "9:30"}, {"shift_end": "24:00"}, {"shift_end": "09:30"},
    {"joined_on": "2026-02-30"}, {"joined_on": "2026-2-01"}, {"created_at": 1783312500000}
])
def test_employee_validation(clean, changes):
    http, _ = clean
    assert http.post("/employees", json={**emp(), **changes}).status_code == 422


@pytest.mark.parametrize("time_value,expected", [("09:40:00", 0), ("09:40:01", 10), ("10:15:59", 45), ("09:00:00", 0)])
def test_late_boundaries(clean, time_value, expected):
    http, db = clean
    create(http)
    r = punch(http, "EMP0001", "2026-07-06T" + time_value)
    assert r.status_code == 201
    assert r.json()["late_minutes"] == expected
    assert r.json()["history"] == []
    assert "id" not in r.json() and "_id" not in r.json()
    assert isinstance(db.attendance_logs.find_one()["punch_in"], datetime)


def test_truncation_before_calculation(clean):
    http, _ = clean
    create(http)
    value = ms("2026-07-06T09:40:00") + 999
    r = http.post("/attendance/punch-in", json={"emp_code": "EMP0001", "punched_at": value})
    assert r.json()["punch_in"] == value - 999
    assert r.json()["late_minutes"] == 0


@pytest.mark.parametrize("duration,expected_hours,half", [(1, 0.0, True), (16181, 4.49, True), (16182, 4.50, False), (32418, 9.01, False)])
def test_half_up_and_half_day(clean, duration, expected_hours, half):
    http, _ = clean
    create(http)
    pi = ms("2026-07-06T09:30:00")
    assert punch(http, "EMP0001", "2026-07-06T09:30:00").status_code == 201
    r = http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": pi + duration * 1000})
    assert r.status_code == 200, r.text
    assert r.json()["work_hours"] == expected_hours
    assert r.json()["half_day"] == half
    assert r.json()["history"] == []


@pytest.mark.parametrize("out,expected", [("18:59:59", 0), ("19:00:00", 30), ("19:10:59", 40)])
def test_overtime_threshold(clean, out, expected):
    http, _ = clean
    create(http)
    punch(http, "EMP0001", "2026-07-06T09:30:00")
    r = http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T" + out)})
    assert r.json()["overtime_minutes"] == expected


def test_overnight_date_and_close(clean):
    http, _ = clean
    create(http, shift_start="22:00", shift_end="06:00")
    r = punch(http, "EMP0001", "2026-07-07T00:15:59")
    assert r.json()["date"] == "2026-07-06"
    assert r.json()["late_minutes"] == 135
    r = http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-07T06:40:00")})
    assert r.status_code == 200
    assert r.json()["date"] == "2026-07-06"
    assert r.json()["overtime_minutes"] == 40
    assert r.json()["work_hours"] == 6.40


def test_punch_out_errors_and_latest_record(clean):
    http, _ = clean
    assert punch(http, "EMP9999", "2026-07-06T09:30").status_code == 404
    create(http)
    assert http.post("/attendance/punch-out", json={"emp_code": "EMP0001"}).status_code == 404
    punch(http, "EMP0001", "2026-07-06T09:30")
    for delta in (0, 86401):
        r = http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T09:30") + delta * 1000})
        assert r.status_code == 422
    r = http.post("/attendance/punch-out", json={"emp_code": "EMP0001", "punched_at": ms("2026-07-06T09:29")})
    assert r.status_code == 404
    punch(http, "EMP0001", "2026-07-07T09:30")
    payload = {"emp_code": "EMP0001", "punched_at": ms("2026-07-07T18:30")}
    assert http.post("/attendance/punch-out", json=payload).status_code == 200
    assert http.post("/attendance/punch-out", json=payload).status_code == 409


def race(n, fn):
    barrier = Barrier(n)
    def ready(i):
        barrier.wait()
        return fn(i)
    with ThreadPoolExecutor(max_workers=n) as pool:
        return list(pool.map(ready, range(n)))


def test_concurrent_creates_and_punches(clean):
    http, db = clean
    results = race(12, lambda _: http.post("/employees", json=emp()).status_code)
    assert results.count(201) == 1 and results.count(409) == 11
    results = race(12, lambda _: punch(http, "EMP0001", "2026-07-06T09:30").status_code)
    assert results.count(201) == 1 and results.count(409) == 11
    payload = {"emp_code": "EMP0001", "punched_at": ms("2026-07-06T18:30")}
    results = race(12, lambda _: http.post("/attendance/punch-out", json=payload).status_code)
    assert results.count(200) == 1 and results.count(409) == 11
    assert db.attendance_logs.find_one()["history"] == []


def test_corrections_and_nested_history(clean):
    http, db = clean
    create(http)
    punch(http, "EMP0001", "2026-07-06T10:15")
    base = {"reason": "Clock repair", "regularized_by": "manager"}
    url = "/attendance/EMP0001/2026-07-06"
    r = http.patch(url, json={**base, "punch_in": ms("2026-07-06T09:30")})
    assert r.status_code == 200
    entry = r.json()["history"][0]
    assert set(entry["changes"]) == {"punch_in", "late_minutes"}
    assert entry["changes"]["punch_in"] == {"from": ms("2026-07-06T10:15"), "to": ms("2026-07-06T09:30")}
    assert isinstance(db.attendance_logs.find_one()["history"][0]["changes"]["punch_in"]["from"], datetime)
    assert http.patch(url, json=base).status_code == 422
    assert http.patch(url, json={**base, "status": "ABSENT", "punch_in": ms("2026-07-06T09:30")}).status_code == 422
    assert http.patch(url, json={**base, "punch_in": ms("2026-07-07T09:30")}).status_code == 422
    assert http.patch(url, json={**base, "work_hours": 8}).status_code == 422
    assert http.patch(url, json={**base, "punch_out": None}).status_code == 422
    r = http.patch(url, json={**base, "status": "LEAVE"})
    assert r.status_code == 200
    assert len(r.json()["history"]) == 2
    assert r.json()["punch_in"] is None and r.json()["late_minutes"] == 0
    assert http.patch(url, json={**base, "status": "WFH"}).status_code == 422
    r = http.patch(url, json={**base, "status": "WFH", "punch_in": ms("2026-07-06T09:30"), "punch_out": ms("2026-07-06T13:00")})
    assert r.status_code == 200
    assert r.json()["half_day"] is True and len(r.json()["history"]) == 3
    assert http.patch("/attendance/EMP9999/2026-07-06", json=base).status_code == 404
    assert http.patch("/attendance/EMP0001/2026-07-05", json=base).status_code == 404


def test_concurrent_corrections_keep_every_success(clean):
    http, db = clean
    create(http)
    punch(http, "EMP0001", "2026-07-06T09:30")
    results = race(16, lambda i: http.patch("/attendance/EMP0001/2026-07-06", json={
        "reason": f"Correction {i}", "regularized_by": f"manager-{i}",
        "punch_in": ms("2026-07-06T09:30") + (i + 1) * 60000}))
    assert all(r.status_code in (200, 409) for r in results)
    history = db.attendance_logs.find_one()["history"]
    successes = [r for r in results if r.status_code == 200]
    assert len(history) == len(successes) >= 1
    for before, after in zip(history, history[1:]):
        assert before["changes"]["punch_in"]["to"] == after["changes"]["punch_in"]["from"]


def test_pagination_filters_sort_and_legacy(clean):
    http, db = clean
    for code, department in [("EMP0003", "Sales"), ("EMP0002", "Engineering"), ("EMP0001", "Engineering")]:
        create(http, code=code, department=department)
    r = http.get("/employees", params={"department": "Engineering", "page_size": 1})
    assert r.json()["total"] == 2 and r.json()["items"][0]["emp_code"] == "EMP0001"
    assert http.get("/employees", params={"department": "Engineering", "page_size": 1, "page": 2}).json()["items"][0]["emp_code"] == "EMP0002"
    docs = [log("EMP0002", "2026-07-06"), log("EMP0001", "2026-07-06"), log("EMP0003", "2026-07-07", status="LEAVE")]
    del docs[1]["half_day"]; del docs[1]["history"]
    db.attendance_logs.insert_many(docs)
    rows = http.get("/attendance").json()["items"]
    assert [(d["date"], d["emp_code"]) for d in rows] == [("2026-07-07", "EMP0003"), ("2026-07-06", "EMP0001"), ("2026-07-06", "EMP0002")]
    assert rows[1]["half_day"] is False and rows[1]["history"] == []
    assert http.get("/attendance", params={"status": "LEAVE"}).json()["total"] == 1
    assert http.get("/attendance", params={"emp_code": "EMP0001", "date_from": "2026-07-06", "date_to": "2026-07-06"}).json()["total"] == 1
    base = {"reason": "Legacy repair", "regularized_by": "manager", "status": "ON_DUTY"}
    assert http.patch("/attendance/EMP0001/2026-07-06", json=base).status_code == 200
    assert len(db.attendance_logs.find_one({"emp_code": "EMP0001"})["history"]) == 1


@pytest.mark.parametrize("path", [
    "/employees?page=0", "/employees?page_size=101", "/attendance?page_size=0",
    "/attendance?status=OTHER", "/attendance?date_from=2026-02-30",
    "/attendance?date_from=2026-07-08&date_to=2026-07-01",
    "/analytics/departments/summary?month=2026-13", "/analytics/departments/summary",
    "/analytics/leaderboard/late?month=2026-07&limit=51",
    "/analytics/departments/Engineering/trend?from=2026-07-08&to=2026-07-01",
    "/analytics/departments/Engineering/trend?from=2026-01-01&to=2026-04-03",
    "/admin/explain/employee_monthly?month=2026-07",
    "/admin/explain/department_trend?department=Engineering",
    "/admin/explain/other", "/admin/explain/department_summary"
])
def test_query_validation(clean, path):
    assert clean[0].get(path).status_code == 422


def test_analytics_ties_headcount_joiners_weekends_and_nulls(clean):
    http, db = clean
    for n in range(1, 7):
        create(http, code=f"EMP{n:04d}", joined_on="2026-08-01" if n == 6 else "2026-07-15" if n == 5 else "2026-01-01")
    create(http, code="EMP0007", department="NoLogs")
    docs = [log("EMP0001", "2026-07-06", hours=8.0, late=40),
            log("EMP0001", "2026-07-07", hours=10.0, late=60),
            log("EMP0002", "2026-07-06", hours=3.0, late=50, half=True),
            log("EMP0003", "2026-07-04", hours=None, late=50, overtime=40),
            log("EMP0004", "2026-07-06", status="LEAVE", late=0),
            log("EMP0004", "2026-07-07", status="ON_DUTY", hours=7.0, late=25),
            log("EMP9999", "2026-07-06", late=1000)]
    db.attendance_logs.insert_many(docs)
    board = http.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 2}).json()["items"]
    assert [(x["emp_code"], x["rank"]) for x in board] == [("EMP0001", 1), ("EMP0002", 2), ("EMP0003", 2)]
    board = http.get("/analytics/leaderboard/late", params={"month": "2026-07", "limit": 4}).json()["items"]
    assert board[-1]["rank"] == 4
    summary = http.get("/analytics/departments/summary", params={"month": "2026-07"}).json()["items"]
    assert summary[0] == {"department": "Engineering", "headcount": 5, "present_days": 3.5,
        "avg_work_hours": 7.0, "late_count": 5, "total_late_minutes": 225,
        "leave_count": 1, "on_duty_count": 1}
    assert summary[1]["headcount"] == 1 and summary[1]["avg_work_hours"] is None
    monthly = http.get("/analytics/employees/EMP0003/monthly?month=2026-07").json()
    assert monthly["present_days"] == 0 and monthly["total_overtime_minutes"] == 40 and monthly["late_count"] == 1
    joiner = http.get("/analytics/employees/EMP0005/monthly?month=2026-07").json()
    assert joiner["working_days"] == 13
    future = http.get("/analytics/employees/EMP0006/monthly?month=2026-07").json()
    assert future["working_days"] == 0 and future["attendance_pct"] is None
    assert http.get("/analytics/employees/EMP9999/monthly?month=2026-07").status_code == 404
    rows = http.get("/analytics/departments/NoLogs/trend?from=2026-07-04&to=2026-07-08").json()["items"]
    assert len(rows) == 5 and rows[0]["moving_avg_7d"] is None
    assert rows[2]["attendance_rate"] == 0 and rows[2]["moving_avg_7d"] == 0
    rows = http.get("/analytics/departments/Engineering/trend?from=2026-07-14&to=2026-07-16").json()["items"]
    assert [x["headcount"] for x in rows] == [4, 5, 5]
    assert http.get("/analytics/departments/Missing/trend?from=2026-07-01&to=2026-07-01").status_code == 404


def test_database_half_up_rounding(clean):
    http, db = clean
    create(http)
    db.attendance_logs.insert_one(log("EMP0001", "2026-07-06", hours=1.005))
    r = http.get("/analytics/departments/summary?month=2026-07").json()
    assert r["items"][0]["avg_work_hours"] == 1.01
    # Headcount 32 gives 1/32 = .03125: four-place half-up must be .0313.
    for n in range(2, 33):
        create(http, code=f"EMP{n:04d}")
    row = http.get("/analytics/departments/Engineering/trend?from=2026-07-06&to=2026-07-06").json()["items"][0]
    assert row["attendance_rate"] == 0.0313 and row["moving_avg_7d"] == 0.0313


def test_random_analytics_against_independent_reference(clean):
    http, db = clean
    rng = random.Random(714)
    employees = []
    docs = []
    for n in range(1, 31):
        e = create(http, code=f"EMP{n:04d}", department="A" if n % 2 else "B",
                   joined_on="2026-07-15" if n % 5 == 0 else "2026-01-01")
        employees.append(e)
        for day in range(1, 32):
            d = date(2026, 7, day)
            if d.isoformat() < e["joined_on"] or rng.random() < .4:
                continue
            status = rng.choice(["PRESENT", "WFH", "ON_DUTY", "ABSENT", "LEAVE"])
            hours = rng.choice([None, 3.5, 8.0, 9.25]) if status in ("PRESENT", "WFH", "ON_DUTY") else None
            docs.append(log(e["emp_code"], d.isoformat(), status, hours, rng.choice([0, 0, 15, 30]), rng.choice([0, 40]), hours == 3.5))
    db.attendance_logs.insert_many(docs)
    for e in employees:
        records = [d for d in docs if d["emp_code"] == e["emp_code"]]
        days = sum(date(2026, 7, n).weekday() < 5 and date(2026, 7, n).isoformat() >= e["joined_on"] for n in range(1, 32))
        present = sum((.5 if d["half_day"] else 1) for d in records if d["status"] in ("PRESENT", "WFH", "ON_DUTY") and date.fromisoformat(d["date"]).weekday() < 5)
        got = http.get(f"/analytics/employees/{e['emp_code']}/monthly?month=2026-07").json()
        assert got["working_days"] == days and got["present_days"] == present
        assert got["attendance_pct"] == rounded(Decimal(str(present)) / days * 100)
        assert got["total_late_minutes"] == sum(d["late_minutes"] for d in records)
    summaries = http.get("/analytics/departments/summary?month=2026-07").json()["items"]
    for dept in ("A", "B"):
        codes = {e["emp_code"] for e in employees if e["department"] == dept}
        records = [d for d in docs if d["emp_code"] in codes]
        hours = [Decimal(str(d["work_hours"])) for d in records if d["status"] in ("PRESENT", "WFH", "ON_DUTY") and d["work_hours"] is not None]
        got = next(x for x in summaries if x["department"] == dept)
        assert got["avg_work_hours"] == rounded(sum(hours) / len(hours))
        assert got["headcount"] == 15
        rows = http.get(f"/analytics/departments/{dept}/trend?from=2026-07-01&to=2026-07-31").json()["items"]
        rates = []
        for row in rows:
            day = row["date"]
            headcount = sum(e["joined_on"] <= day and e["department"] == dept for e in employees)
            present = sum((.5 if d["half_day"] else 1) for d in records if d["date"] == day and d["status"] in ("PRESENT", "WFH", "ON_DUTY"))
            rate = rounded(Decimal(str(present)) / headcount, 4) if date.fromisoformat(day).weekday() < 5 and headcount else None
            rates.append(rate)
            window = [Decimal(str(r)) for r in rates[-7:] if r is not None]
            assert row["attendance_rate"] == rate
            assert row["moving_avg_7d"] == (rounded(sum(window) / len(window), 4) if window else None)


def scan_stages(value):
    stages = []
    if isinstance(value, dict):
        if "stage" in value:
            stages.append(value["stage"])
        if "collectionScans" in value:
            assert value["collectionScans"] == 0
        for v in value.values():
            stages.extend(scan_stages(v))
    elif isinstance(value, list):
        for v in value:
            stages.extend(scan_stages(v))
    return stages


@pytest.mark.scale
@pytest.mark.skipif(os.environ.get("RUN_SCALE") != "1", reason="Set RUN_SCALE=1 for 100k-record verification")
def test_100k_real_mongodb_query_plans(clean):
    http, db = clean
    employees = [{**emp(f"EMP{100000+n:06d}", "Engineering" if n % 2 else "Sales"),
                  "shift_start": "09:30", "shift_end": "18:30", "created_at": datetime(2026, 1, 1, tzinfo=UTC)} for n in range(1000)]
    db.employees.insert_many(employees)
    first = date(2026, 6, 1)
    for offset in range(0, 1000, 100):
        docs = [log(e["emp_code"], (first + timedelta(days=d)).isoformat(), late=d % 40)
                for e in employees[offset:offset+100] for d in range(100)]
        db.attendance_logs.insert_many(docs)
    assert db.attendance_logs.count_documents({}) == 100000
    specs = [
        ("attendance_list", {}),
        ("attendance_list", {"date_from": "2026-07-01", "date_to": "2026-07-31", "page": 2}),
        ("attendance_list", {"emp_code": "EMP100001", "status": "PRESENT", "page_size": 1}),
        ("attendance_list", {"status": "PRESENT", "page_size": 100}),
        ("employee_monthly", {"emp_code": "EMP100001", "month": "2026-07"}),
        ("department_summary", {"month": "2026-07"}),
        ("department_summary", {"month": "2026-07", "department": "Engineering"}),
        ("late_leaderboard", {"month": "2026-07", "limit": 2}),
        ("late_leaderboard", {"month": "2026-07", "department": "Engineering", "limit": 2}),
        ("department_trend", {"department": "Engineering", "from": "2026-07-01", "to": "2026-07-31"})
    ]
    for endpoint, params in specs:
        r = http.get("/admin/explain/" + endpoint, params=params)
        assert r.status_code == 200, r.text
        stages = scan_stages(r.json()["explain"])
        assert "IXSCAN" in stages and "COLLSCAN" not in stages, (endpoint, stages)
