import uuid
from contextlib import asynccontextmanager
from typing import Any, Optional, Union

from fastapi import Depends, FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field, StrictInt
from sqlalchemy import select, update
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
    priority: StrictInt = Field(default=3, ge=1, le=5)


class TaskResponse(BaseModel):
    id: uuid.UUID
    type: str
    payload: dict[str, Any]
    status: str
    priority: int

    model_config = {"from_attributes": True}


@app.post("/tasks", status_code=201)
def submit_task(task: TaskCreate, db: Session = Depends(get_db)) -> dict[str, uuid.UUID]:
    new_task = Task(type=task.type, payload=task.payload, priority=task.priority)
    db.add(new_task)
    db.commit()
    db.refresh(new_task)
    return {"id": new_task.id}


@app.post("/tasks/process-next", response_model=TaskResponse)
def process_next_task(db: Session = Depends(get_db)) -> Union[Task, Response]:
    task = db.scalars(
        select(Task)
        .where(Task.status == "pending")
        .order_by(Task.priority.desc(), Task.created_at.asc(), Task.id.asc())
        .with_for_update(skip_locked=True)
        .limit(1)
    ).first()
    if task is None:
        return Response(status_code=204)

    task.status = "completed"
    db.commit()
    db.refresh(task)
    return task


@app.get("/tasks/{task_id}", response_model=TaskResponse)
def retrieve_task(task_id: uuid.UUID, db: Session = Depends(get_db)) -> Task:
    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@app.get("/tasks", response_model=list[TaskResponse])
def list_tasks(status: Optional[str] = None, db: Session = Depends(get_db)) -> list[Task]:
    if status is not None and status not in {"pending", "completed", "cancelled"}:
        raise HTTPException(status_code=400, detail="Invalid status")
    query = select(Task)
    if status is not None:
        query = query.where(Task.status == status)
    return list(db.scalars(query))


@app.post("/tasks/{task_id}/processes", response_model=TaskResponse)
def process_task(task_id: uuid.UUID, db: Session = Depends(get_db)) -> Task:
    task = db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "pending")
        .values(status="completed")
        .returning(Task)
    ).scalar_one_or_none()
    if task is not None:
        db.commit()
        db.refresh(task)
        return task

    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    raise HTTPException(status_code=409, detail="Task cannot be processed")


@app.post("/tasks/{task_id}/cancel", response_model=TaskResponse)
def cancel_task(task_id: uuid.UUID, db: Session = Depends(get_db)) -> Task:
    task = db.execute(
        update(Task)
        .where(Task.id == task_id, Task.status == "pending")
        .values(status="cancelled")
        .returning(Task)
    ).scalar_one_or_none()
    if task is not None:
        db.commit()
        db.refresh(task)
        return task

    task = db.get(Task, task_id)
    if task is None:
        raise HTTPException(status_code=404, detail="Task not found")
    raise HTTPException(status_code=409, detail="Task cannot be cancelled")
