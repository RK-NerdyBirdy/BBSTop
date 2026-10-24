from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.batch import Batch
from app.models.semester import Semester
from app.models.student_semester_assignment import StudentSemesterAssignment


class BatchService:
    @staticmethod
    async def get_batch_export(
        db: AsyncSession,
        batch_id: int,
    ) -> dict:
        """Export batch details with all assigned students."""
        batch_result = await db.execute(
            select(Batch).where(Batch.batch_id == batch_id)
        )
        batch = batch_result.scalar_one_or_none()
        if not batch:
            return None

        semester_result = await db.execute(
            select(Semester).where(Semester.semester_id == batch.semester_id)
        )
        semester = semester_result.scalar_one_or_none()

        assignments_result = await db.execute(
            select(StudentSemesterAssignment)
            .options(selectinload(StudentSemesterAssignment.student))
            .where(StudentSemesterAssignment.batch_id == batch_id)
            .order_by(StudentSemesterAssignment.register_number)
        )
        assignments = assignments_result.scalars().all()

        return {
            "batch_id": batch.batch_id,
            "batch_name": batch.batch_name,
            "semester_id": batch.semester_id,
            "semester_name": semester.semester_name if semester else None,
            "swap_allowed": semester.swap_allowed if semester else False,
            "total_students": len(assignments),
            "students": [
                {
                    "register_number": a.register_number,
                    "student_name": a.student.student_name,
                    "email": a.student.email,
                    "phone_number": a.student.phone_number,
                    "batch_name": batch.batch_name,
                    "semester_name": semester.semester_name if semester else None,
                    "cgpa": float(a.cgpa),
                    "active": a.active,
                }
                for a in assignments
            ],
            "stats": {
                "avg_cgpa": sum(float(a.cgpa) for a in assignments) / len(assignments) if assignments else 0.0,
                "min_cgpa": min(float(a.cgpa) for a in assignments) if assignments else 0.0,
                "max_cgpa": max(float(a.cgpa) for a in assignments) if assignments else 0.0,
            },
        }

    @staticmethod
    async def update_swap_allowed(
        db: AsyncSession,
        semester_id: int,
        swap_allowed: bool,
    ) -> dict:
        """Toggle swap_allowed flag for a semester."""
        semester_result = await db.execute(
            select(Semester).where(Semester.semester_id == semester_id)
        )
        semester = semester_result.scalar_one_or_none()
        if not semester:
            return None
        if semester.archived and swap_allowed:
            raise HTTPException(status_code=400, detail="This semester is archived and read-only")

        semester.swap_allowed = swap_allowed
        if swap_allowed:
            # Students can now see and act on batches: no more full reshuffles
            semester.assignment_locked = True
        await db.commit()
        await db.refresh(semester)

        return {
            "semester_id": semester.semester_id,
            "semester_name": semester.semester_name,
            "swap_allowed": semester.swap_allowed,
        }

    @staticmethod
    async def update_assignment_batch(
        db: AsyncSession,
        assignment_id: int,
        new_batch_id: int,
    ) -> dict:
        """Manually reassign student to a different batch."""
        assignment_result = await db.execute(
            select(StudentSemesterAssignment).where(
                StudentSemesterAssignment.assignment_id == assignment_id
            )
        )
        assignment = assignment_result.scalar_one_or_none()
        if not assignment:
            return None

        old_batch_id = assignment.batch_id
        new_batch_result = await db.execute(
            select(Batch).where(Batch.batch_id == new_batch_id)
        )
        new_batch = new_batch_result.scalar_one_or_none()
        if not new_batch:
            return None

        if assignment.semester_id != new_batch.semester_id:
            return None

        assignment.batch_id = new_batch_id
        await db.commit()
        await db.refresh(assignment)

        return {
            "assignment_id": assignment.assignment_id,
            "register_number": assignment.register_number,
            "old_batch_id": old_batch_id,
            "new_batch_id": new_batch_id,
            "semester_id": assignment.semester_id,
        }


batch_service = BatchService()
