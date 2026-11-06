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
        "priority": 3,
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


@pytest.mark.parametrize("priority", [0, 6, "3", 2.5, True])
def test_submit_rejects_invalid_priority(priority):
    response = client.post("/tasks", json={"type": "email", "payload": {}, "priority": priority})

    assert response.status_code == 400
    assert response.json() == {"detail": "Invalid request"}


def test_submit_uses_default_and_requested_priority():
    default_task = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]
    highest_task = client.post("/tasks", json={"type": "report", "payload": {}, "priority": 5}).json()["id"]

    assert client.get(f"/tasks/{default_task}").json()["priority"] == 3
    assert client.get(f"/tasks/{highest_task}").json()["priority"] == 5


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


def test_list_tasks_can_filter_cancelled_tasks():
    task_id = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]
    client.post(f"/tasks/{task_id}/cancel")

    response = client.get("/tasks", params={"status": "cancelled"})

    assert response.status_code == 200
    assert [task["id"] for task in response.json()] == [task_id]


def test_process_task_marks_pending_task_completed():
    task_id = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]

    response = client.post(f"/tasks/{task_id}/processes")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"


def test_process_next_selects_highest_priority_task():
    client.post("/tasks", json={"type": "low", "payload": {}, "priority": 1})
    expected = client.post("/tasks", json={"type": "high", "payload": {}, "priority": 5}).json()["id"]
    client.post("/tasks", json={"type": "middle", "payload": {}, "priority": 3})

    response = client.post("/tasks/process-next")

    assert response.status_code == 200
    assert response.json()["id"] == expected
    assert response.json()["status"] == "completed"


def test_process_next_breaks_equal_priority_by_submission_time():
    expected = client.post("/tasks", json={"type": "first", "payload": {}, "priority": 4}).json()["id"]
    client.post("/tasks", json={"type": "second", "payload": {}, "priority": 4})

    response = client.post("/tasks/process-next")

    assert response.status_code == 200
    assert response.json()["id"] == expected


def test_process_next_returns_no_content_when_no_tasks_are_pending():
    response = client.post("/tasks/process-next")

    assert response.status_code == 204
    assert response.content == b""


def test_process_next_skips_cancelled_tasks():
    cancelled = client.post("/tasks", json={"type": "email", "payload": {}, "priority": 5}).json()["id"]
    expected = client.post("/tasks", json={"type": "report", "payload": {}, "priority": 1}).json()["id"]
    client.post(f"/tasks/{cancelled}/cancel")

    response = client.post("/tasks/process-next")

    assert response.status_code == 200
    assert response.json()["id"] == expected


def test_process_completed_task_returns_conflict():
    task_id = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]
    client.post(f"/tasks/{task_id}/processes")

    response = client.post(f"/tasks/{task_id}/processes")

    assert response.status_code == 409
    assert response.json() == {"detail": "Task cannot be processed"}


def test_cancel_pending_task():
    task_id = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]

    response = client.post(f"/tasks/{task_id}/cancel")

    assert response.status_code == 200
    assert response.json()["status"] == "cancelled"


def test_cancel_missing_task_returns_not_found():
    response = client.post("/tasks/00000000-0000-0000-0000-000000000000/cancel")

    assert response.status_code == 404
    assert response.json() == {"detail": "Task not found"}


@pytest.mark.parametrize("terminal_action", ["processes", "cancel"])
def test_cancel_terminal_task_returns_conflict(terminal_action):
    task_id = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]
    client.post(f"/tasks/{task_id}/{terminal_action}")

    response = client.post(f"/tasks/{task_id}/cancel")

    assert response.status_code == 409
    assert response.json() == {"detail": "Task cannot be cancelled"}


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


def test_concurrent_process_next_claims_distinct_tasks():
    global session_barrier
    task_ids = [
        client.post("/tasks", json={"type": "email", "payload": {}, "priority": 5}).json()["id"]
        for _ in range(2)
    ]
    session_pids.clear()
    session_barrier = Barrier(2)

    def process_next():
        concurrent_client = TestClient(app)
        return concurrent_client.post("/tasks/process-next")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(lambda _: process_next(), range(2)))
    finally:
        session_barrier = None

    assert [response.status_code for response in responses] == [200, 200]
    assert {response.json()["id"] for response in responses} == set(task_ids)
    assert len(set(session_pids)) == 2


def test_concurrent_cancellation_and_processing_allow_one_transition():
    global session_barrier
    task_id = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]
    session_pids.clear()
    session_barrier = Barrier(2)

    def cancel_or_process(path):
        concurrent_client = TestClient(app)
        return concurrent_client.post(f"/tasks/{task_id}/{path}")

    try:
        with ThreadPoolExecutor(max_workers=2) as executor:
            responses = list(executor.map(cancel_or_process, ["cancel", "processes"]))
    finally:
        session_barrier = None

    assert sorted(response.status_code for response in responses) == [200, 409]
    assert len(set(session_pids)) == 2
    assert client.get(f"/tasks/{task_id}").json()["status"] in {"completed", "cancelled"}
