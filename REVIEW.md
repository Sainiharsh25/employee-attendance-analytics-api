# REVIEW.md

## Starter Code Defect Review

| # | Where (function / area) | What is wrong | How you'd notice it (test, input, or symptom) | How it was fixed |
|---|---|---|---|---|
| 1 | `compute_late_minutes` | The shift start is constructed using the punch-in date. For an overnight shift, an early-morning punch-in is compared against the wrong day's shift start. | Test a 22:00-06:00 overnight shift with a punch-in at 01:00. | Added IST-aware attendance-day and shift-boundary helpers, with timestamps normalized to UTC and truncated to whole seconds. |
| 2 | `compute_overtime` | The scheduled shift end is always constructed on the supplied attendance date, so overnight shifts use the wrong end time. | Test a 22:00-06:00 shift and a punch-out after 06:30 the next day. | Calculate the scheduled end on the correct day, including the next day for overnight shifts. |
| 3 | `compute_work_hours` | Built-in `round()` does not guarantee the required half-up rounding behavior. | Test values whose rounding to two decimal places is a tie. | Use `Decimal` with `ROUND_HALF_UP` and round work hours to two decimal places. |
| 4 | `list_employees` | The skip calculation uses `page * page_size`, which skips the first page when `page=1`. The total count also ignores the department filter. | Create more than one page of employees; request page 1 and filter by department. | Use `(page - 1) * page_size` and count documents using the same filter as the query. |
| 5 | `list_attendance` | The starter loads all matching records into Python, sorts them there, and only then paginates. This wastes memory and work as the collection grows. | Query a large attendance collection and inspect how many records are loaded for a small page. | Apply sorting, `skip`, and `limit` in MongoDB, and count matching records separately. |
| 6 | `create_employee` and `punch_in` | Checking for an existing record before insertion is not sufficient to prevent simultaneous duplicate requests. Punch-in also does not explicitly reject an unknown employee. | Send concurrent punch-in requests for the same employee and date; try punching in an unknown employee. | Added unique indexes, duplicate-key handling, and an employee existence check. |
| 7 | `health` | The starter always returns `{"status": "ok"}` without checking whether MongoDB is reachable. | Stop MongoDB and call `GET /health`. | Ping MongoDB and return HTTP 503 when the database is unavailable. |
| 8 | API endpoints / contract coverage | The starter leaves punch-out, regularization, monthly analytics, department summary, late leaderboard, department trend, and explain endpoints unimplemented. | Call the required endpoints from the API contract. | Implemented the missing endpoints and their request validation, persistence, and aggregation logic. |
| 9 | Request validation and error handling | The starter has limited input validation and does not consistently handle missing employees, invalid date ranges, duplicate records, or invalid punch-out operations. | Try malformed dates, invalid pagination values, an unknown employee, and duplicate or invalid punch operations. | Added Pydantic constraints, date and range checks, and appropriate HTTP 404, 409, and 422 responses. |

## Behaviors Inspected and Not Considered Defects

- **Environment-based MongoDB configuration:** Loading `.env` for local convenience while reading `MONGO_URI` and `MONGO_DB` from environment variables is appropriate; actual environment variables can override defaults.
- **Optional punch timestamps:** Allowing the server to supply the current time when `punched_at` is omitted is intentional, provided supplied timestamps are validated and normalized.
- **Default shift times:** Defaulting to a 09:30-18:30 shift is valid; the implementation also validates custom shift values.
- **Separate employee and attendance collections:** Keeping employee details separate from daily attendance records is reasonable and supports employee lookups and attendance history.
