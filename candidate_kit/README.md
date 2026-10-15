# Employee Attendance & Analytics API

## Run locally

Use Python 3.10 or newer and MongoDB 6.0 or newer. Install the dependencies:

```bash
python -m venv .venv
pip install -r requirements.txt
```

Set `MONGO_URI` and `MONGO_DB` in the environment or in a local `.env` file. Load the sample records if desired:

```bash
python sample_seed.py
uvicorn app.main:app --port 8000
```

The app creates its indexes at startup. `/health` returns ready only when MongoDB responds to a ping.

## Local edge-case data

`local_test_data.py` is intentionally ignored by Git. Run it once to create a separate `<MONGO_DB>_b0_test` database with
the no-log, mid-month join, overnight, tied-lateness, weekend, and missing-weekday cases. To test against those records,
set `MONGO_DB` to that suffixed database before starting the app. The script does not drop or modify the configured base
database.

## Completion

The employee, attendance, monthly analytics, department summary, late leaderboard, department trend, and explain
endpoints are implemented. No assignment endpoints are currently unfinished.
