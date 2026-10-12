# Employee Attendance & Analytics API

A REST API built with FastAPI and MongoDB for employee management, attendance tracking, punch-in/punch-out, attendance regularization, and analytics.

## Requirements

- Python 3.10+
- MongoDB 6.0+
- Dependencies listed in `requirements.txt`

## Setup

Create and activate a virtual environment, then install dependencies:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
```

Configure the MongoDB connection using environment variables:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
```

Start the API:

```powershell
python -m uvicorn app.main:app --port 8000
```

The application creates its indexes at startup. MongoDB must be reachable for the API to operate.

## API Documentation

- Health check: `GET /health`
- Employee management: `/employees`
- Attendance and punch operations: `/attendance`
- Monthly employee analytics: `/analytics/employees/{emp_code}/monthly`
- Department summary: `/analytics/departments/summary`
- Late leaderboard: `/analytics/leaderboard/late`
- Department trend: `/analytics/departments/{department}/trend`
- Query execution plans: `/admin/explain/{endpoint}`

Interactive API documentation is available at `/docs`.

## Error Handling

The API uses HTTP 404 for missing resources, HTTP 409 for conflicts, and HTTP 422 for invalid input.

## Project Files

- `app/main.py` — application, models, routes, and MongoDB aggregation pipelines
- `requirements.txt` — Python dependencies
- `REVIEW.md` — starter defect review
- `DECISIONS.md` — implementation decisions

## Configuration and Security

Do not commit `.env` files, credentials, database dumps, or other secrets. Configure database connection details through environment variables.