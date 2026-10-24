#!/usr/bin/env python3
"""Seed a LOCAL database with test data for every role.

    cd Backend && .venv/bin/python -m scripts.seed_dev

Refuses to run unless DATABASE_URL points at localhost. Safe to re-run (skips existing rows).

Test accounts (local only):
  HOD/admin : hod@vit.ac.in / devpassword123   (admin login form)
  Guides    : guide1..guide4@vit.ac.in          (dev login; guide4 has a cap of 1)
  Students  : student01..student24@vitstudent.ac.in (dev login), register numbers 23BBS0001..23BBS0024
"""

import asyncio
import sys

from sqlalchemy import select

from app.core.config import get_settings
from app.core.database import async_session_maker
from app.core.security import get_password_hash
from app.models import Admin, Batch, Faculty, Semester, Student, StudentSemesterAssignment

SEMESTER_NAME = "23 Winter Sem 2026-27"
BATCHES = ["B1", "B2", "B3", "B4"]
NAMES = [
    "Aarav Shah", "Diya Iyer", "Kabir Mehta", "Ananya Rao", "Vivaan Gupta", "Isha Nair",
    "Arjun Reddy", "Meera Pillai", "Rohan Das", "Sara Khan", "Aditya Menon", "Tara Joshi",
    "Nikhil Verma", "Riya Kapoor", "Kunal Bose", "Neha Sinha", "Varun Patel", "Pooja Kulkarni",
    "Siddharth Jain", "Kavya Hegde", "Yash Malhotra", "Anika Sethi", "Dev Chawla", "Lavanya Murthy",
]
GUIDES = [
    ("10001", "Dr. Priya Raman", "guide1@vit.ac.in", None),
    ("10002", "Dr. Suresh Kumar", "guide2@vit.ac.in", None),
    ("10003", "Dr. Lakshmi Narayanan", "guide3@vit.ac.in", 2),
    ("10004", "Dr. Arvind Bhat", "guide4@vit.ac.in", 1),
]


async def main() -> None:
    url = get_settings().DATABASE_URL
    if "localhost" not in url and "127.0.0.1" not in url:
        print("Refusing to seed: DATABASE_URL is not a local database.")
        sys.exit(1)

    async with async_session_maker() as db:
        if not (await db.execute(select(Admin).where(Admin.admin_email == "hod@vit.ac.in"))).scalar_one_or_none():
            db.add(Admin(
                admin_name="HOD (dev)",
                admin_email="hod@vit.ac.in",
                password_hash=get_password_hash("devpassword123"),
                password_initial_change=True,
            ))

        semester = (await db.execute(select(Semester).where(Semester.semester_name == SEMESTER_NAME))).scalar_one_or_none()
        if semester is None:
            semester = Semester(semester_name=SEMESTER_NAME, cohort_year=2023, swap_allowed=True, registration_open=True)
            db.add(semester)
            await db.flush()

        batches = {}
        for name in BATCHES:
            batch = (await db.execute(
                select(Batch).where(Batch.semester_id == semester.semester_id, Batch.batch_name == name)
            )).scalar_one_or_none()
            if batch is None:
                batch = Batch(batch_name=name, semester_id=semester.semester_id)
                db.add(batch)
                await db.flush()
            batches[name] = batch

        for i, name in enumerate(NAMES, start=1):
            reg = f"23BBS{i:04d}"
            if await db.get(Student, reg) is None:
                db.add(Student(
                    register_number=reg,
                    student_name=name,
                    email=f"student{i:02d}@vitstudent.ac.in",
                    phone_number=f"98{i:08d}",
                ))
                await db.flush()
                db.add(StudentSemesterAssignment(
                    register_number=reg,
                    semester_id=semester.semester_id,
                    batch_id=batches[BATCHES[(i - 1) % len(BATCHES)]].batch_id,
                    cgpa=round(7.0 + (i * 0.37) % 3, 2),
                ))

        for employee_id, name, email, cap in GUIDES:
            if not (await db.execute(select(Faculty).where(Faculty.employee_id == employee_id))).scalar_one_or_none():
                db.add(Faculty(employee_id=employee_id, faculty_name=name, email=email, max_projects=cap))

        await db.commit()
    print("Seeded local database. See scripts/seed_dev.py for test accounts.")


if __name__ == "__main__":
    asyncio.run(main())
