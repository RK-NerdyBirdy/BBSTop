from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.student_semester_assignment import StudentSemesterAssignment
from app.models.swap_request import SwapRequest
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
        - Neither student may have used up their swaps for the semester
        """

        requester_result = await db.execute(
            select(StudentSemesterAssignment).where(
                StudentSemesterAssignment.assignment_id == requester_assignment_id
            )
        )
        requester = requester_result.scalar_one_or_none()

        if requester is None:
            return []

        limit = await settings_service.get(db, "swap_limit")
        if (await SwapCandidateService.swaps_used(db, [requester_assignment_id])).get(requester_assignment_id, 0) >= limit:
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
        candidates = list(result.scalars().all())
        used = await SwapCandidateService.swaps_used(db, [c.assignment_id for c in candidates])
        return [c for c in candidates if used.get(c.assignment_id, 0) < limit]

    @staticmethod
    async def swaps_used(db: AsyncSession, assignment_ids: list[int]) -> dict[int, int]:
        """assignment_id -> approved swaps. Assignments are per semester, so the count starts afresh each semester."""
        if not assignment_ids:
            return {}
        rows = await db.execute(
            select(SwapRequest.requester_assignment_id, SwapRequest.target_assignment_id).where(
                SwapRequest.status == "APPROVED",
                or_(
                    SwapRequest.requester_assignment_id.in_(assignment_ids),
                    SwapRequest.target_assignment_id.in_(assignment_ids),
                ),
            )
        )
        wanted = set(assignment_ids)
        used: dict[int, int] = {}
        for requester_id, target_id in rows.all():
            for assignment_id in (requester_id, target_id):
                if assignment_id in wanted:
                    used[assignment_id] = used.get(assignment_id, 0) + 1
        return used

    async def limit_reached(self, db: AsyncSession, *assignments: StudentSemesterAssignment) -> list[StudentSemesterAssignment]:
        """The given students who have no swaps left this semester."""
        limit = await settings_service.get(db, "swap_limit")
        used = await self.swaps_used(db, [a.assignment_id for a in assignments])
        return [a for a in assignments if used.get(a.assignment_id, 0) >= limit]

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
