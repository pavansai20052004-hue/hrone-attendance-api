# Design decisions

1. **Indexes.** Unique indexes on employee code and employee/date prevent duplicates. Department/code supports employee listing. Joined-on/department and department/joined-on support headcount queries. Attendance date/code, employee/date, status/date/code and employee/status/date support filtered lists and analytics. Employee/punch-in supports finding the latest punch. A separate late-minutes index would not help much because the leaderboard ranks monthly totals, rather than individual records.

2. **Punch-in race.** Both requests validate the input, find the employee and calculate the same IST date. Both try to insert. MongoDB's unique employee/date index allows one insert, returning 201. The other raises DuplicateKeyError, which the API returns as 409. A check before inserting would not safely handle this race.

3. **Ties.** The leaderboard uses MongoDB `$rank` on total late minutes. Employees tied at the cutoff are all returned. Their employee codes decide display order, without changing their shared rank.

4. **Headcount.** The summary starts with employees who joined by month-end and then joins attendance totals. Employees with no logs still contribute one to headcount and zero attendance.

5. **100x data.** Daily department summaries would reduce repeated aggregation work. Corrections would update those summaries, with reconciliation against the original logs to keep the numbers reliable.
