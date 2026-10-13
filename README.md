# Employee Attendance & Analytics API

A Python API built with FastAPI and MongoDB for employee attendance tracking and analytics.

## Features

- Employee creation and listing with validation and pagination.
- Attendance punch-in and punch-out with business-rule validation.
- Manual attendance corrections with an audit trail.
- Monthly employee attendance analytics.
- Department attendance summaries.
- Late-arrival leaderboard with ranking and tie handling.
- Department attendance trends over a date range.
- Query execution statistics for supported analytics and listing endpoints.

## Technology Stack

- Python 3.11+
- FastAPI
- MongoDB 6.0+
- PyMongo
- Pydantic

## Setup

1. Install Python 3.11 or newer.
2. Install MongoDB 6.0 or newer and ensure it is running.
3. Install dependencies:

   ```bash
   pip install -r requirements.txt
   ```

4. Configure the `MONGO_URI` and `MONGO_DB` environment variables for your MongoDB instance. Do not commit secrets or `.env` files.
5. Start the API:

   ```bash
   uvicorn app.main:app --port 8000
   ```

6. Open the interactive API documentation at:

   `http://127.0.0.1:8000/docs`

## Project Files

- `app/main.py` — API endpoints and business logic.
- `requirements.txt` — Python dependencies.
- `openapi.yaml` — API contract.
- `DATA_MODEL.md` — MongoDB data model.
- `REVIEW.md` — Starter-code review.
- `DECISIONS.md` — Design decisions.
- `sample_seed.py` and `sample_data/` — Sample-data loading resources, if included.

## Notes

This project was developed as part of the HROne Software Engineer Trainee technical assignment. Refer to the API documentation and assignment contract for endpoint details and business rules.
