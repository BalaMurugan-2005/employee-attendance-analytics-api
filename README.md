# Employee Attendance & Analytics API

A FastAPI backend for employee attendance management with punch-in/out, manual corrections with audit trails, and MongoDB-powered analytics.

## Features

- **Employee management** — Create and list employees with pagination and department filtering
- **Attendance tracking** — Punch-in, punch-out with IST timezone handling and overnight shift support
- **Manual corrections** — Regularize attendance records with full audit trail (history)
- **Analytics** — Monthly employee summary, department summary, late-comers leaderboard, and daily attendance trends
- **Business rules** — R1-R10 fully implemented (late/grace, overtime threshold, half-day detection, working days, headcount)
- **Concurrency safety** — Unique indexes prevent duplicate records; atomic updates prevent data loss
- **MongoDB explain** — Admin endpoint to verify index usage

## Tech Stack

- **Python 3.11+**
- **FastAPI** — async web framework with auto-generated OpenAPI docs
- **MongoDB 6.0+** — document database with aggregation pipelines
- **PyMongo 4.7+** — MongoDB driver
- **Pydantic 2.6+** — request/response validation
- **python-dotenv** — environment variable loading

## Prerequisites

- Python 3.11 or newer
- MongoDB 6.0 or newer (7.0 recommended; Atlas M0 free tier works)
- pip

## Installation

```bash
# Clone the repository
git clone <your-repo-url>
cd candidate_kit

# Create a virtual environment
python -m venv .venv

# Activate (Windows PowerShell)
.venv\Scripts\Activate.ps1

# Activate (Linux/macOS)
source .venv/bin/activate

# Install dependencies
pip install -r requirements.txt
```

## MongoDB Setup

Start MongoDB locally (Docker):
```bash
docker run -d -p 27017:27017 --name mongo mongo:7
```

Or use MongoDB Atlas (free M0 tier) and set the connection string in your `.env`.

## Configuration

Create a `.env` file (do NOT commit it):
```
MONGO_URI=mongodb://localhost:27017
MONGO_DB=attendance_db
```

Real environment variables always override `.env` values.

## Running the API

```bash
uvicorn app.main:app --port 8000 --reload
```

## Swagger Documentation

Open [http://localhost:8000/docs](http://localhost:8000/docs) in your browser for the interactive Swagger UI.

## Seed Sample Data

```bash
python sample_seed.py
```

## Running Tests

```bash
# Install test dependencies
pip install pytest httpx

# Run tests (uses a separate test database: attendance_test_db)
pytest tests/test_main.py -v
```

## API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/health` | Readiness probe (pings MongoDB) |
| POST | `/employees` | Create an employee |
| GET | `/employees` | List employees (paginated, filterable) |
| POST | `/attendance/punch-in` | Record a punch-in |
| POST | `/attendance/punch-out` | Record a punch-out |
| GET | `/attendance` | List attendance records (paginated, filterable) |
| PATCH | `/attendance/{emp_code}/{date}` | Regularize (correct) a record |
| GET | `/analytics/employees/{emp_code}/monthly` | Monthly summary for an employee |
| GET | `/analytics/departments/summary` | Department summary for a month |
| GET | `/analytics/leaderboard/late` | Late-comers leaderboard |
| GET | `/analytics/departments/{department}/trend` | Daily attendance trend |
| GET | `/admin/explain/{endpoint}` | MongoDB explain output |

## Example API Calls

```bash
# Health check
curl http://localhost:8000/health

# Create an employee
curl -X POST http://localhost:8000/employees \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0001","name":"Asha Rao","email":"asha@example.com","department":"Engineering","joined_on":"2026-01-05"}'

# Punch in (epoch ms for 2026-07-06 09:28:00 IST)
curl -X POST http://localhost:8000/attendance/punch-in \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0001","punched_at":1783311480000}'

# Punch out
curl -X POST http://localhost:8000/attendance/punch-out \
  -H "Content-Type: application/json" \
  -d '{"emp_code":"EMP0001","punched_at":1783344300000}'

# Monthly analytics
curl "http://localhost:8000/analytics/employees/EMP0001/monthly?month=2026-07"

# Department summary
curl "http://localhost:8000/analytics/departments/summary?month=2026-07"

# Late leaderboard
curl "http://localhost:8000/analytics/leaderboard/late?month=2026-07&limit=5"
```

## Known Limitations

- **No authentication** — `regularized_by` is free text; there is no login or authorization system. This is by design for this assignment.
- **No holiday calendar** — Working days are Mon-Fri only (R7). No support for public holidays.
- **Department trend gap-filling** — The trend endpoint fills gaps in Python rather than using `$densify` in MongoDB. This is correct and efficient for the <= 92 day range limit.
- **Pagination** — Uses skip-based pagination which can slow down on very deep pages. For 100x scale, cursor-based pagination would be better.
- **Single file** — All code is in `app/main.py` as required by the assignment. In production, this would be split into separate modules.
