# Starter code review

Locations refer to the original starter functions, before replacement.

| # | Where | What is wrong | How to notice it | Fix |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | Shift start is constructed in the punch timestamp's timezone, rather than IST. | 09:40 IST is 04:10 UTC; the old helper compares it with 09:30 UTC. | Construct the start on the attendance date in IST; compare aware instants. |
| 2 | `compute_late_minutes` | Flooring before the grace comparison suppresses lateness between 10:01 and 10:59. | 09:40:01 for a 09:30 shift must report 10 late minutes. | Compare seconds strictly above 600, then floor minutes. |
| 3 | `compute_late_minutes` / punch-in | Overnight shifts use the wrong shift-start day. | A 00:15 punch for 22:00–06:00 belongs to the previous date and is 135 minutes late. | Derive the attendance date before constructing shift bounds. |
| 4 | `compute_work_hours` | Python `round` uses ties-to-even; float arithmetic also loses exact boundary precision. | 9 hours 18 seconds is 9.005 hours and must become 9.01. | Divide integer seconds using Decimal and quantize half-up. |
| 5 | `compute_overtime` | No minimum of 30 whole overtime minutes. | 18:59:59 for an 18:30 end must give zero. | Floor the duration, then apply the 30-minute threshold. |
| 6 | `compute_overtime` | Overnight shift end is placed on the attendance date instead of the following day. | A 06:40 next-day exit for 22:00–06:00 must give 40, not 1480. | Advance the end one calendar day for overnight shifts. |
| 7 | All punch helpers | Subsecond instants are neither truncated before calculation nor stored consistently. | 09:40:00.999 must be stored as 09:40:00 and remain inside grace. | Truncate epoch milliseconds with integer division before conversion. |
| 8 | MongoDB client / timestamp handling | Naive local timestamps and naive UTC BSON values can be mixed. | Change the host timezone or read a seeded record; results vary or subtraction fails. | Use UTC-aware PyMongo decoding; explicitly convert to IST for calendar rules. |
| 9 | `health` | Returns ready without contacting MongoDB. | Stop MongoDB and `/health` still returns 200. | Ping MongoDB; return 503 on failure and require startup indexes. |
| 10 | `EmployeeIn` | No code, name, email, department, date or time validation. | Invalid email, EMP format, 24:00 or February 30 is accepted. | Add contract limits, patterns and real calendar validation. |
| 11 | `EmployeeIn` | Equal shift start/end is accepted. | Submit 09:30–09:30. | Reject equal values with model validation. |
| 12 | `create_employee` | Check-then-insert is vulnerable to a race; no unique index. | Send 12 parallel identical creates; more than one can succeed. | Create a unique employee-code index and map DuplicateKeyError to 409. |
| 13 | `create_employee` | Server creation time is a naive datetime and returned as ISO text. | Response `created_at` is not integer epoch milliseconds. | Store an aware UTC datetime and serialize to integer milliseconds. |
| 14 | `list_employees` | `skip = page * page_size` skips the first page. | Page 1 with one employee returns nothing. | Use `(page - 1) * page_size`. |
| 15 | `list_employees` | Total ignores the department filter. | Two Engineering employees among three overall must yield total 2. | Count with the same filter and selected index. |
| 16 | `list_employees` | Employee ordering is unspecified. | Insert EMP0003 before EMP0001; listing must still start with EMP0001. | Sort by emp_code ascending in MongoDB. |
| 17 | List endpoints | Page and page-size inputs are unbounded. | Page 0 or page_size 101 is accepted. | Enforce page >= 1 and page_size 1–100. |
| 18 | `PunchInIn` | Coerces floats/strings into integers and lacks timestamp range checks. | Epoch seconds, a numeric string, a float or bool must produce 422. | Require strict bounded integer milliseconds; explicit null is invalid. |
| 19 | `PunchInIn` | Any status is accepted at punch-in. | ABSENT or an unknown status creates a punched record. | Use the three-value presence enum. |
| 20 | `punch_in` | An unknown employee is dereferenced. | Unknown code raises a server error at `emp["shift_start"]`. | Return 404 before constructing the record. |
| 21 | `punch_in` | Attendance date follows the host clock, without IST or overnight attribution. | 20:00 UTC is already the next day in IST. | Apply R1 in one shared attendance-date helper. |
| 22 | `punch_in` | Check-then-insert does not protect the daily natural key. | Parallel punches produce duplicate employee/date records. | Unique `(emp_code, date)` index; one insert succeeds and others get 409. |
| 23 | Punch-in / attendance list | Exposes MongoDB identifiers and returns datetime values instead of millisecond instants. | Responses contain `id`; punches and nested history times are ISO text. | Select only contract fields; recursively serialize datetime values. |
| 24 | `list_attendance` | Fetches and sorts every matching record in Python. | A 100k-record query loads all records just to return 20. | Indexed MongoDB sort, skip, limit and count. |
| 25 | `list_attendance` | Same-date rows lack the emp_code tie-breaker. | Same day EMP0002 can precede EMP0001. | Sort date descending, then emp_code ascending. |
| 26 | `list_attendance` | No real date validation, reversed-range validation or status validation. | Invalid dates, reversed dates or unknown statuses reach MongoDB. | Validate at the API boundary; reject reversed bounds with 422. |
| 27 | `list_attendance` | Legacy records lack required defaults in responses. | A seeded record missing history/half_day returns an incomplete shape. | Default history to [], half_day to false, missing minute totals to zero. |
| 28 | Startup / lists | Secondary and unique indexes are never created. | Explain contains a collection scan and duplicate writes are possible. | Idempotently build natural-key, filter, sort, join and punch-selection indexes at startup. |

Examined and retained: `load_dotenv()` with the normal non-overriding behavior lets real environment variables take priority. Synchronous FastAPI handlers are suitable for synchronous PyMongo because FastAPI dispatches them to its worker thread pool. Calendar dates and shift times remain strings as the storage contract requires. A zero overtime value and an empty correction history at punch-in are correct. The starter's history=[] is a freshly constructed list, not a shared mutable function default.
