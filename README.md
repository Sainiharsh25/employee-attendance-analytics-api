# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management, attendance tracking, punch-in/punch-out, attendance regularization, and analytics.

## Author

**Harsh Saini**

GitHub: [Sainiharsh25](https://github.com/Sainiharsh25)

## Requirements

- Python 3.10+
- MongoDB 6.0+
- Dependencies listed in `requirements.txt`

## Setup

Open PowerShell in the project directory.

### 1. Create and activate a virtual environment

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Configure MongoDB

Set the MongoDB connection details using environment variables:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

Make sure MongoDB is running and accessible before starting the application.

### 4. Start the API

```powershell
python -m uvicorn app.main:app --port 8000
```

The API will be available at:

`http://127.0.0.1:8000`

## API Documentation

Interactive API documentation is available at:

`http://127.0.0.1:8000/docs`

### Available Endpoints

| Method | Endpoint | Description |
|---|---|---|
| GET | `/health` | Check application and database health |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees |
| POST | `/attendance/punch-in` | Record employee punch-in |
| POST | `/attendance/punch-out` | Record employee punch-out |
| GET | `/attendance` | List attendance records |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize an attendance record |
| GET | `/analytics/employees/{emp_code}/monthly` | Get monthly employee analytics |
| GET | `/analytics/departments/summary` | Get department-wise monthly summary |
| GET | `/analytics/leaderboard/late` | Get the late-arrival leaderboard |
| GET | `/analytics/departments/{department}/trend` | Get department attendance trends |
| GET | `/admin/explain/{endpoint}` | Inspect MongoDB query execution statistics |

## Features

- Employee creation and listing
- Attendance punch-in and punch-out
- Attendance regularization with audit history
- Late-arrival and overtime calculations
- Monthly employee attendance analytics
- Department-wise attendance summaries
- Late-arrival leaderboard with ranking
- Department attendance trends and moving averages
- MongoDB aggregation pipelines for analytics
- Database indexes for query performance
- Pagination and input validation
- HTTP error handling for missing resources, conflicts, and invalid input
- Query execution statistics for supported analytics and listing endpoints

## Error Handling

The API uses appropriate HTTP status codes:

- `404 Not Found` — requested resource does not exist
- `409 Conflict` — conflicting operation, such as a duplicate employee or punch
- `422 Unprocessable Entity` — invalid request data or validation errors

## Project Structure

```text
employee-attendance-analytics-api/
├── app/
│   └── main.py
├── requirements.txt
├── README.md
├── REVIEW.md
├── DECISIONS.md
└── .gitignore
```

## Configuration and Security

- Configure MongoDB through the `MONGO_URI` and `MONGO_DB` environment variables.
- The application creates its database indexes at startup.
- Do not commit `.env` files, database credentials, secrets, virtual environments, or database dumps.
- Keep configuration and sensitive information outside the source code.

## License

This project was developed as part of a software engineering assignment.

## Author

**Harsh Saini**

GitHub: [Sainiharsh25](https://github.com/Sainiharsh25)