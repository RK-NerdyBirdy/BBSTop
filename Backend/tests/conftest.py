"""Test setup: runs the real app against a throwaway local Postgres database (bbstop_test).

    docker exec bbstop-postgres psql -U postgres -c "CREATE DATABASE bbstop_test"
    .venv/bin/pytest
"""

import os

# Must be set before the app (and its engine) is imported
os.environ["DATABASE_URL"] = os.environ.get(
    "TEST_DATABASE_URL", "postgresql+asyncpg://postgres:postgres@localhost:5434/bbstop_test"
)
os.environ["DEV_AUTH_ENABLED"] = "false"

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import text

from app.core.database import async_session_maker, engine
from app.main import app
from app.models import Admin, Base, Batch, Faculty, Semester, Student, StudentSemesterAssignment
from app.services.auth_service import auth_service

assert "localhost" in os.environ["DATABASE_URL"], "Tests must run against a local database"


@pytest.fixture(scope="session", autouse=True)
async def schema():
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)
    yield
    await engine.dispose()


@pytest.fixture(autouse=True)
async def clean_db(schema):
    tables = ", ".join(f'"{t.name}"' for t in Base.metadata.sorted_tables)
    async with engine.begin() as conn:
        await conn.execute(text(f"TRUNCATE {tables} RESTART IDENTITY CASCADE"))
    yield


@pytest.fixture
async def client():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


class World:
    """Seeded semester with 4 batches, 10 students, 3 guides and a HOD, plus auth headers for each."""

    semester_id: int
    closed_semester_id: int

    def student(self, n: int) -> dict:
        reg = f"S{n:02d}"
        return {"Authorization": f"Bearer {auth_service.create_jwt_token(f's{n}@vitstudent.ac.in', reg, 'student')}"}

    def reg(self, n: int) -> str:
        return f"S{n:02d}"

    def guide(self, faculty_id: int) -> dict:
        token = auth_service.create_jwt_token(f"g{faculty_id}@vit.ac.in", f"G{faculty_id}", "guide", user_id=faculty_id)
        return {"Authorization": f"Bearer {token}"}

    hod = {"Authorization": f"Bearer {auth_service.create_jwt_token('hod@vit.ac.in', 'HOD', 'admin', user_id=1)}"}


@pytest.fixture
async def world() -> World:
    w = World()
    async with async_session_maker() as db:
        db.add(Admin(admin_name="HOD", admin_email="hod@vit.ac.in", password_hash="x", password_initial_change=True))
        open_sem = Semester(semester_name="Open", registration_open=True, swap_allowed=False)
        closed_sem = Semester(semester_name="Closed", registration_open=False, swap_allowed=False)
        db.add_all([open_sem, closed_sem])
        await db.flush()
        batches = [Batch(batch_name=f"B{i}", semester_id=open_sem.semester_id) for i in range(1, 5)]
        closed_batch = Batch(batch_name="C1", semester_id=closed_sem.semester_id)
        db.add_all(batches + [closed_batch])
        await db.flush()
        for n in range(1, 11):
            reg = f"S{n:02d}"
            db.add(Student(register_number=reg, student_name=f"Student {n}", email=f"s{n}@vitstudent.ac.in"))
            await db.flush()
            db.add(StudentSemesterAssignment(register_number=reg, semester_id=open_sem.semester_id, batch_id=batches[n % 4].batch_id, cgpa=8.0))
            db.add(StudentSemesterAssignment(register_number=reg, semester_id=closed_sem.semester_id, batch_id=closed_batch.batch_id, cgpa=8.0))
        # faculty 1: default cap (4), faculty 2: cap 1, faculty 3: cap 2
        db.add_all([
            Faculty(employee_id="E1", faculty_name="Guide One", email="g1@vit.ac.in"),
            Faculty(employee_id="E2", faculty_name="Guide Two", email="g2@vit.ac.in", max_projects=1),
            Faculty(employee_id="E3", faculty_name="Guide Three", email="g3@vit.ac.in", max_projects=2),
        ])
        await db.commit()
        w.semester_id = open_sem.semester_id
        w.closed_semester_id = closed_sem.semester_id
    return w
