from concurrent.futures import ThreadPoolExecutor
import os
from threading import Barrier, Lock

from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
import pytest

from app.database import Base, get_db
from app.main import app


TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+psycopg://postgres:postgres@localhost:5433/tasks_test",
)
test_engine = create_engine(TEST_DATABASE_URL, pool_size=5, max_overflow=0)
TestingSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)
session_barrier = None
session_pids = []
session_lock = Lock()

if test_engine.dialect.name != "postgresql":
    raise RuntimeError("Tests require a PostgreSQL TEST_DATABASE_URL")


def override_get_db():
    db = TestingSessionLocal()
    try:
        backend_pid = db.connection().connection.driver_connection.info.backend_pid
        with session_lock:
            session_pids.append(backend_pid)
        if session_barrier is not None:
            session_barrier.wait(timeout=5)
        yield db
    finally:
        db.close()


app.dependency_overrides[get_db] = override_get_db
client = TestClient(app)


def setup_function():
    Base.metadata.drop_all(bind=test_engine)
    Base.metadata.create_all(bind=test_engine)


def test_submit_task_returns_new_id():
    response = client.post("/tasks", json={"type": "email", "payload": {"to": "a@example.com"}})

    assert response.status_code == 201
    assert response.json()["id"]


def test_retrieve_task_returns_task():
    created = client.post("/tasks", json={"type": "report", "payload": {"period": "weekly"}})

    response = client.get(f"/tasks/{created.json()['id']}")

    assert response.status_code == 200
    assert response.json() == {
        "id": created.json()["id"],
        "type": "report",
        "payload": {"period": "weekly"},
        "status": "pending",
    }


@pytest.mark.parametrize("path", ["/tasks/00000000-0000-0000-0000-000000000000", "/tasks/00000000-0000-0000-0000-000000000000/processes"])
def test_unknown_task_returns_not_found(path):
    response = client.get(path) if path.endswith("000000") else client.post(path)

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


@pytest.mark.parametrize("body", [{"payload": {}}, {"type": "email"}])
def test_submit_requires_type_and_payload(body):
    response = client.post("/tasks", json=body)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid request"}


def test_submit_rejects_invalid_json():
    response = client.post("/tasks", content='{', headers={"content-type": "application/json"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid request"}


@pytest.mark.parametrize("body", [{"type": 1, "payload": {}}, {"type": "email", "payload": "not-an-object"}])
def test_submit_rejects_wrong_field_types(body):
    response = client.post("/tasks", json=body)

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid request"}


def test_list_tasks_can_filter_by_status():
    pending = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]
    completed = client.post("/tasks", json={"type": "report", "payload": {}}).json()["id"]
    client.post(f"/tasks/{completed}/processes")

    response = client.get("/tasks", params={"status": "pending"})

    assert response.status_code == 200
    assert [task["id"] for task in response.json()] == [pending]


def test_list_tasks_rejects_invalid_status_filter():
    response = client.get("/tasks", params={"status": "unknown"})

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid status"}


def test_process_task_marks_pending_task_completed():
    task_id = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]

    response = client.post(f"/tasks/{task_id}/processes")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_process_completed_task_returns_conflict():
    task_id = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]
    client.post(f"/tasks/{task_id}/processes")

    response = client.post(f"/tasks/{task_id}/processes")

    assert response.status_code == 409
    assert response.json() == {"detail": "Task already completed"}


def test_concurrent_processing_allows_only_one_completion():
    global session_barrier
    task_id = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]
    session_pids.clear()
    session_barrier = Barrier(2)

    def process():
        concurrent_client = TestClient(app)
        return concurrent_client.post(f"/tasks/{task_id}/processes")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: process(), range(2)))
    finally:
        session_barrier = None

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(set(session_pids)) == 2
    assert client.get(f"/tasks/{task_id}").json()["status"] == "completed"
