"""Real-MongoDB tests use a unique disposable DB, never the application DB."""
import os
import uuid
import pytest


@pytest.fixture(scope="session")
def api():
    uri = os.environ.get("TEST_MONGO_URI")
    if not uri:
        pytest.skip("Set TEST_MONGO_URI to a MongoDB 6.0+ instance")
    name = "hrone_test_" + uuid.uuid4().hex
    os.environ["MONGO_URI"] = uri
    os.environ["MONGO_DB"] = name
    from fastapi.testclient import TestClient
    from pymongo import MongoClient
    from app.main import app, db
    control = MongoClient(uri)
    try:
        with TestClient(app) as http:
            assert http.get("/health").status_code == 200
            yield http, db
    finally:
        control.drop_database(name)
        control.close()


@pytest.fixture
def clean(api):
    http, db = api
    db.employees.delete_many({})
    db.attendance_logs.delete_many({})
    return http, db
