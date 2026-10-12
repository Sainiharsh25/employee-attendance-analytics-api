# DECISIONS.md

1. **Indexes.** I created a unique employee-code index to prevent duplicate employees, a department/employee-code index for department queries, and a department/join-date index for headcount queries. Attendance has a unique employee/date index to prevent duplicate daily records, a date/employee-code index for attendance listing, and an employee/date/status index for employee attendance filters. I considered a separate late-minutes index but prioritized the indexes supporting common date and employee queries.

2. **Punch-in race.** Both requests check the employee, but the unique employee/date index allows only one attendance record for that employee on that date. One insert succeeds; the other raises `DuplicateKeyError`, which the API converts to HTTP 409.

3. **Ties.** The leaderboard uses MongoDB `$rank`, so employees tied at the cutoff are included even if the response contains more than the requested limit. Employee code provides deterministic ordering among tied results.

4. **Headcount.** The department summary starts from employees and uses `$lookup` with `preserveNullAndEmptyArrays`. Employees who joined by the period end are counted even if they have no attendance logs.

5. **Scaling.** For 100 times more data, I would review execution statistics against representative workloads, tune indexes using measured query patterns, and consider pre-aggregated analytics for expensive reporting.