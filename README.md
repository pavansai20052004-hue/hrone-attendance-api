# Employee Attendance & Analytics API

Python 3.11+, FastAPI, PyMongo and MongoDB 6.0+. All application code is in `app/main.py`. Implements the complete v2 attendance, regularization, analytics and explain endpoints. Indexes are created automatically; no seed step is required.

## Run

Install from the repository root:

```bash
python -m venv .venv
pip install -r requirements.txt
```

Activate the virtual environment before installing. On Windows PowerShell use `.venv\Scripts\Activate.ps1`; on Linux/macOS use `source .venv/bin/activate`.

Set `MONGO_URI` and `MONGO_DB` in your shell. PowerShell example:

```powershell
$env:MONGO_URI = "mongodb://localhost:27017"
$env:MONGO_DB = "attendance_db"
uvicorn app.main:app --port 8000
```

Linux/macOS:

```bash
export MONGO_URI="mongodb://localhost:27017"
export MONGO_DB="attendance_db"
uvicorn app.main:app --port 8000
```

Check `http://localhost:8000/health`; explore `http://localhost:8000/docs`. Real environment variables override local dotenv settings. No external services are called by the application.

## Verification

```bash
pip install -r requirements-dev.txt
```

Set `TEST_MONGO_URI` to your MongoDB URI, then run `python -m pytest -q`. Set `RUN_SCALE=1` to include the 100,000-record test. Tests create and remove a uniquely named test database; they never use the application database. See `VERIFICATION.md` for observed results and `WALKTHROUGH.md` for the implementation explanation.

No endpoints are intentionally missing. The employer's hidden tests are unavailable. Authentication and holiday calendars are outside this assignment's scope. Code and notes were prepared with AI assistance; the candidate must review, understand, and take responsibility for the submitted work.
