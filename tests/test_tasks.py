from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.main import app


test_engine = create_engine(
    "sqlite://",
    connect_args={"check_same_thread": False},
    poolclass=StaticPool,
)
TestingSessionLocal = sessionmaker(bind=test_engine, autoflush=False, autocommit=False)


def override_get_db():
    db = TestingSessionLocal()
    try:
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


def test_list_tasks_can_filter_by_status():
    pending = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]
    completed = client.post("/tasks", json={"type": "report", "payload": {}}).json()["id"]
    client.post(f"/tasks/{completed}/processes")

    response = client.get("/tasks", params={"status": "pending"})

    assert response.status_code == 200
    assert [task["id"] for task in response.json()] == [pending]


def test_process_task_marks_pending_task_completed():
    task_id = client.post("/tasks", json={"type": "email", "payload": {}}).json()["id"]

    response = client.post(f"/tasks/{task_id}/processes")

    assert response.status_code == 200
    assert response.json()["status"] == "completed"
