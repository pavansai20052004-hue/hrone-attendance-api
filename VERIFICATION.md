# Observed verification results

Verified on 9 October 2026 using Python 3.12 and the dependency versions pinned in the requirements files. The employer's hidden dataset and grading code are not available.

| Check | Observed result |
|---|---|
| Real MongoDB 7.0.26, matching the grader's stated major version | 54 tests passed in 4.63 seconds |
| Real MongoDB 8.0.15 compatibility check | 54 tests passed in 4.31 seconds |
| Fresh application against 100,000 preseeded records with only default indexes | Healthy in 1.382 seconds, below the 20-second contract |
| MongoDB unavailable during startup and health probe | Health endpoint returned 503; it never reported ready |
| Query-plan checks against 100,000 generated attendance records | All five explain endpoint types used IXSCAN, no COLLSCAN; lookup collectionScans were zero |
| Timestamp inputs | Seconds-looking values, floats, strings, booleans, null, out-of-range values rejected |
| Time calculations | Grace boundary, second truncation, half-up rounding, half-day rounding boundary, overtime threshold and overnight attribution passed |
| Parallel writes | 12 duplicate employee creates, 12 duplicate punch-ins and 12 duplicate punch-outs each produced exactly one success |
| Parallel corrections | Every successful correction retained one chained audit entry; competing writes returned 409 when their snapshots became stale |
| Analytics | Mid-month/future joiners, zero-log employees, half days, weekends, open records, orphans, legacy fields and cutoff ties passed |
| Independent reference comparison | Deterministic randomized monthly summaries, department averages, daily rates and moving averages matched a separate Python calculation |

The test runs emitted one Starlette deprecation warning concerning its test HTTP transport; there were no failed tests. This warning does not affect the running API.

Python 3.11 and MongoDB 6.0 are supported by the intended stack and operators, but were not independently exercised in these runs. Measured timings describe this verification environment, not a production performance guarantee.

## Reproduce

From the repository root, install `requirements-dev.txt` and set `TEST_MONGO_URI` to a reachable MongoDB instance. Each run creates a random `hrone_test_...` database and deletes only that database afterward.

PowerShell:

```powershell
$env:TEST_MONGO_URI = "mongodb://localhost:27017"
$env:RUN_SCALE = "1"
python -m pytest -q
```

Linux/macOS:

```bash
TEST_MONGO_URI="mongodb://localhost:27017" RUN_SCALE=1 python -m pytest -q
```

The scale test generates its dataset in memory and inserts it into the disposable database. No dumps or employer data are shipped in this repository.
