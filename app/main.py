import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from .database import Base, engine, get_db
from .models import Task


@asynccontextmanager
async def lifespan(_: FastAPI):
    Base.metadata.create_all(bind=engine)
    yield


app = FastAPI(lifespan=lifespan)


@app.exception_handler(RequestValidationError)
async def invalid_request(_: Request, __: RequestValidationError) -> JSONResponse:
    return JSONResponse(status_code=400, content={"detail": "Invalid request"})


class TaskCreate(BaseModel):
    type: str
    payload: dict[str, Any]


class TaskResponse(BaseModel):
    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    status: str

    model_config = {"from_attributes": True}


@app.post("/tasks", status_code=201)
def submit_task(task: TaskCreate, db: Session = Depends(get_db)) -> dict[str, uuid.UUID]:
    new_task = Task(type=task.type, payload=task.payload)
    db.add(new_task)
    db.commit()
    db.refresh(new_task)
    return {"id": new_task.id}


@app.get("/tasks/{task_id}", response_model=TaskResponse)
def retrieve_task(task_id: uuid.UUID, db: Session = Depends(get_db)) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/tasks", response_model=list[TaskResponse])
def list_tasks(status: Optional[str] = None, db: Session = Depends(get_db)) -> list[Task]:
    if status is not None and status not in {"pending", "completed"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    query = select(Task)
    if status is not None:
        query = query.where(Task.status == status)
    return list(db.scalars(query))


@app.post("/tasks/{task_id}/processes", response_model=TaskResponse)
def process_task(task_id: uuid.UUID, db: Session = Depends(get_db)) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status == "completed":
        raise HTTPException(status_code=409, detail="Task already completed")
    task.status = "completed"
    db.commit()
    db.refresh(task)
    return task
