from sqlalchemy import and_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.student_semester_assignment import StudentSemesterAssignment
from app.services.settings_service import settings_service


class SwapCandidateService:
    @staticmethod
    async def get_swap_candidates(
        db: AsyncSession,
        requester_assignment_id: int,
    ) -> list[StudentSemesterAssignment]:
        """Fetch eligible swap candidates for a requester.

        Rules:
        - Must be in the same semester as requester
        - Must NOT be in the same batch as requester
        - CGPA must be within requester_cgpa ± CGPA_TOL
        - Must be active in their assignment
        """

        requester_result = await db.execute(
            select(StudentSemesterAssignment).where(
                StudentSemesterAssignment.assignment_id == requester_assignment_id
            )
        )
        requester = requester_result.scalar_one_or_none()

        if requester is None:
            return []

        cgpa_threshold = float(await settings_service.get(db, "swap_cgpa_tolerance"))
        requester_cgpa = float(requester.cgpa)
        min_cgpa = max(0.0, requester_cgpa - cgpa_threshold)
        max_cgpa = min(10.0, requester_cgpa + cgpa_threshold)

        result = await db.execute(
            select(StudentSemesterAssignment).where(
                and_(
                    StudentSemesterAssignment.semester_id == requester.semester_id,
                    StudentSemesterAssignment.batch_id != requester.batch_id,
                    StudentSemesterAssignment.cgpa >= min_cgpa,
                    StudentSemesterAssignment.cgpa <= max_cgpa,
                    StudentSemesterAssignment.active.is_(True),
                    StudentSemesterAssignment.assignment_id != requester_assignment_id,
                )
            )
        )
        return list(result.scalars().all())

    async def is_eligible(
        self,
        db: AsyncSession,
        requester: StudentSemesterAssignment,
        target: StudentSemesterAssignment,
    ) -> bool:
        """Re-check a swap pair against current CGPAs (they can change after a CGPA upload)."""
        tolerance = float(await settings_service.get(db, "swap_cgpa_tolerance"))
        return (
            requester.semester_id == target.semester_id
            and requester.batch_id != target.batch_id
            and requester.active
            and target.active
            and abs(float(requester.cgpa) - float(target.cgpa)) <= tolerance + 1e-9
        )


swap_candidate_service = SwapCandidateService()
