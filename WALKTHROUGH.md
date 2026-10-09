# Read this before submitting

This package was prepared with AI assistance. The code, review notes and design answers are completed. Use this walkthrough to prepare for the employer's live conversation, where you are expected to explain the code and make a small change. It does not claim that you independently authored or already reviewed the code.

## Trace a normal request

An employee has code EMP0001, shift 09:30–18:30, and an IST joining date. The employee code is the public identifier. MongoDB `_id` stays internal.

Punch-in at 09:40:00 yields zero late minutes. At 09:40:01 it yields ten: compare the full second duration with the ten-minute grace first, then floor the entire duration in minutes. Do not subtract the grace period. Any fractional milliseconds are discarded before these calculations.

The unique index on `(emp_code, date)` determines whether insertion succeeds. A duplicate raises an exception mapped to 409. A Python check before insertion cannot prevent two concurrent requests from both passing the check.

Punch-out selects the employee's most recent punch-in at or before the supplied instant, including already closed records. It must not skip a closed recent record and accidentally close an older open one. Duration must be positive and at most 24 hours. Work hours use Decimal half-up rounding. Half-day depends on the rounded hours, so 4.495 hours becomes 4.50 and is a full day. Overtime under 30 whole minutes is zero.

## Trace an overnight shift

A 22:00–06:00 employee punches in on 7 July at 00:15 IST. Because the time is earlier than 06:00, the attendance date is 6 July. The shift began at 22:00 on 6 July, so lateness is 135 minutes. An exit at 06:40 on 7 July has 40 overtime minutes.

Study `attendance_date`, `shift_bounds`, and `derived`. Changing timezone on the machine does not change these results: storage is UTC and calendar decisions are explicitly IST.

## Trace a manual correction

`RegularizeIn.model_fields_set` distinguishes omitted fields from supplied fields. An omitted punch time stays unchanged; explicit null is rejected because the request contract does not allow it. Selecting ABSENT/LEAVE clears the stored times through the business rule. A presence status requires a punch-in.

The code merges the permitted changes, recalculates the four derived fields, compares the final values with the original values, and appends one audit entry containing only actual changes. A no-op produces 422. Nested history punch times remain BSON datetimes in MongoDB and become milliseconds in JSON.

`unchanged_snapshot` supplies an optimistic compare-and-set condition. It includes the prior mutable values and history, distinguishing missing legacy fields from explicitly stored fields. MongoDB atomically applies `$set` and `$push` only if that snapshot still matches. A concurrent change either precedes the read or makes the update fail with 409. No successful correction overwrites another correction's history.

## Understand each aggregation

| Builder | Main collection | Main idea |
|---|---|---|
| `employee_monthly_pipeline` | employees | Match one code, aggregate its monthly logs, generate the month's working-day calendar in MongoDB, count weekdays from joined_on. |
| `department_summary_pipeline` | employees | Begin with eligible employees so zero-log people count; join grouped log totals and combine record-level hours sums/counts. |
| `late_leaderboard_pipeline` | attendance_logs | Filter the month, group by code, join existing employees, filter department, rank totals, then filter rank and sort ties. |
| `department_trend_pipeline` | employees | Gather the department's employees, generate a daily calendar in MongoDB, join each day's logs, count headcount as of that day, then calculate a seven-row moving window. |

The trend deliberately generates its calendar from employees using `$range`, `$map`, `$dateAdd` and `$unwind`. There is no Python loop over dates and no dependence on finding an attendance log. This guarantees an entire empty interval still returns its daily rows. Saturday/Sunday rates are null; weekdays with positive headcount and no logs have rate zero. The moving average ignores null rates and only considers rows inside the requested range.

The leaderboard uses standard competition rank. Totals 100, 50, 50, 25 produce ranks 1, 2, 2, 4. A limit of two returns three employees. Add emp_code only to the final sort, not to the ranking sort.

Department work-hour averages use the sum and count of eligible records, not an average of per-employee averages. An open record with null hours is excluded from the hours average. Analytics use stored derived values; they do not reinterpret seeded punch times. Weekends contribute late/overtime totals; monthly present days count only weekdays. The daily trend's present counts include weekends.

MongoDB `$round` uses ties-to-even. `half_up` uses Decimal128 arithmetic and floor(value * scale + 0.5) for the nonnegative reported numbers. Rates use four decimal places; other rounded quantities use two.

## Explain and indexes

`explain` reuses the same pipeline builders, attendance filter builder, sort, hint, skip and limit as the real endpoints. It passes executionStats verbosity to MongoDB and returns the actual result. It does not fabricate an index plan. Test `scan_stages` also inspects lookup collectionScans.

Try these paths in `/docs` or a browser:

```text
/admin/explain/attendance_list?date_from=2026-07-01&date_to=2026-07-31&page=2
/admin/explain/employee_monthly?emp_code=EMP0001&month=2026-07
/admin/explain/department_summary?month=2026-07
/admin/explain/late_leaderboard?month=2026-07&limit=2
/admin/explain/department_trend?department=Engineering&from=2026-07-01&to=2026-07-31
```

## Practice changes for the live session

1. Change grace from ten to five minutes; explain why the boundary comparison must still precede flooring. Update a boundary test and run it.
2. Add a presence status; identify all schema and aggregation places affected by the shared status list.
3. Change the moving-average window to fourteen days; explain why thirteen preceding rows plus the current row are required.
4. Explain the tradeoff between offset pagination and keyset pagination at much greater scale.
5. Explain why local time must not be used to decode UTC BSON datetimes.

Official reference used to check aggregation behavior: https://www.mongodb.com/docs/manual/reference/operator/aggregation/round/ and https://www.mongodb.com/docs/manual/reference/operator/aggregation/rank/.
