from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.models.semester import Semester
from app.models.student_semester_assignment import StudentSemesterAssignment
from app.models.swap_request import SwapRequest
from app.schemas.swap_request import SwapRequestCreate


async def create_swap_request(
    db: AsyncSession,
    payload: SwapRequestCreate,
) -> SwapRequest:
    request = SwapRequest(
        requester_assignment_id=payload.requester_assignment_id,
        target_assignment_id=payload.target_assignment_id,
        status="PENDING",
    )
    db.add(request)
    await db.commit()
    await db.refresh(request)
    return request


async def get_swap_request(db: AsyncSession, request_id: int) -> SwapRequest | None:
    result = await db.execute(
        select(SwapRequest).where(SwapRequest.request_id == request_id)
    )
    return result.scalar_one_or_none()


async def get_swap_request_for_update(
    db: AsyncSession,
    request_id: int,
) -> SwapRequest | None:
    result = await db.execute(
        select(SwapRequest)
        .where(SwapRequest.request_id == request_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def get_active_swap_request_between(
    db: AsyncSession,
    requester_assignment_id: int,
    target_assignment_id: int,
) -> SwapRequest | None:
    active_statuses = ["PENDING", "ACCEPTED"]
    result = await db.execute(
        select(SwapRequest)
        .where(
            or_(
                and_(
                    SwapRequest.requester_assignment_id == requester_assignment_id,
                    SwapRequest.target_assignment_id == target_assignment_id,
                ),
                and_(
                    SwapRequest.requester_assignment_id == target_assignment_id,
                    SwapRequest.target_assignment_id == requester_assignment_id,
                ),
            ),
            SwapRequest.status.in_(active_statuses),
        )
        .order_by(SwapRequest.created_at.desc())
        .limit(1)
    )
    return result.scalars().first()


async def update_swap_request_status(
    db: AsyncSession,
    request_id: int,
    status: str,
    admin_id: int | None = None,
) -> SwapRequest | None:
    request = await get_swap_request(db, request_id)
    if request is None:
        return None

    request.status = status
    if admin_id is not None:
        request.approved_by = admin_id
        from datetime import datetime, timezone

        request.approved_at = datetime.now(timezone.utc)

    await db.commit()
    await db.refresh(request)
    return request


async def get_assignment_by_id(
    db: AsyncSession,
    assignment_id: int,
) -> StudentSemesterAssignment | None:
    result = await db.execute(
        select(StudentSemesterAssignment).where(
            StudentSemesterAssignment.assignment_id == assignment_id
        )
    )
    return result.scalar_one_or_none()


async def get_assignment_by_id_for_update(
    db: AsyncSession,
    assignment_id: int,
) -> StudentSemesterAssignment | None:
    result = await db.execute(
        select(StudentSemesterAssignment)
        .where(StudentSemesterAssignment.assignment_id == assignment_id)
        .with_for_update()
    )
    return result.scalar_one_or_none()


async def get_assignments_by_register_number_for_update(
    db: AsyncSession,
    register_number: str,
) -> list[StudentSemesterAssignment]:
    result = await db.execute(
        select(StudentSemesterAssignment)
        .where(StudentSemesterAssignment.register_number == register_number)
        .with_for_update()
    )
    return list(result.scalars().all())


async def get_student_assignments_by_semester(
    db: AsyncSession,
    register_number: str,
    semester_id: int,
) -> list[StudentSemesterAssignment]:
    result = await db.execute(
        select(StudentSemesterAssignment).where(
            and_(
                StudentSemesterAssignment.register_number == register_number,
                StudentSemesterAssignment.semester_id == semester_id,
            )
        )
    )
    return list(result.scalars().all())


async def get_all_student_assignments(
    db: AsyncSession,
    register_number: str,
) -> list[StudentSemesterAssignment]:
    """Get a student's assignments in current (non-archived) semesters; archived ones are hidden from students."""
    result = await db.execute(
        select(StudentSemesterAssignment)
        .join(Semester, Semester.semester_id == StudentSemesterAssignment.semester_id)
        .where(
            StudentSemesterAssignment.register_number == register_number,
            Semester.archived.is_(False),
        )
    )
    return list(result.scalars().all())


async def list_swap_requests_for_assignments(
    db: AsyncSession,
    assignment_ids: list[int],
    incoming: bool,
) -> list[SwapRequest]:
    if not assignment_ids:
        return []

    target_field = (
        SwapRequest.target_assignment_id if incoming else SwapRequest.requester_assignment_id
    )

    result = await db.execute(
        select(SwapRequest)
        .options(
            selectinload(SwapRequest.requester_assignment),
            selectinload(SwapRequest.target_assignment),
        )
        .where(target_field.in_(assignment_ids))
        .order_by(SwapRequest.created_at.desc())
    )
    return list(result.scalars().all())


async def list_accepted_swap_requests(db: AsyncSession) -> list[SwapRequest]:
    result = await db.execute(
        select(SwapRequest)
        .options(
            selectinload(SwapRequest.requester_assignment),
            selectinload(SwapRequest.target_assignment),
        )
        .where(SwapRequest.status == "ACCEPTED")
        .order_by(SwapRequest.created_at.desc())
    )
    return list(result.scalars().all())


async def has_accepted_request_for_assignments(
    db: AsyncSession,
    assignment_ids: list[int],
    exclude_request_id: int | None = None,
) -> bool:
    if not assignment_ids:
        return False

    stmt = select(SwapRequest.request_id).where(
        or_(
            SwapRequest.target_assignment_id.in_(assignment_ids),
            SwapRequest.requester_assignment_id.in_(assignment_ids),
        ),
        SwapRequest.status == "ACCEPTED",
    )

    if exclude_request_id is not None:
        stmt = stmt.where(SwapRequest.request_id != exclude_request_id)

    result = await db.execute(stmt)
    return result.first() is not None




async def execute_batch_swap(
    db: AsyncSession,
    requester_assignment_id: int,
    target_assignment_id: int,
    commit: bool = True,
    lock: bool = True,
) -> tuple[StudentSemesterAssignment, StudentSemesterAssignment] | None:
    """Execute batch swap for two students in a swap request.

    Returns:
        Tuple of (updated_requester_assignment, updated_target_assignment) or None if failed
    """
    if lock:
        # Lock in deterministic order by assignment_id to avoid deadlocks
        first_id, second_id = (
            (requester_assignment_id, target_assignment_id)
            if requester_assignment_id <= target_assignment_id
            else (target_assignment_id, requester_assignment_id)
        )
        requester = await get_assignment_by_id_for_update(db, first_id)
        target = await get_assignment_by_id_for_update(db, second_id)
    else:
        requester = await get_assignment_by_id(db, requester_assignment_id)
        target = await get_assignment_by_id(db, target_assignment_id)

    if not requester or not target:
        return None

    # Swap batch IDs
    requester_batch = requester.batch_id
    requester.batch_id = target.batch_id
    target.batch_id = requester_batch

    db.add(requester)
    db.add(target)

    if commit:
        await db.commit()
        await db.refresh(requester)
        await db.refresh(target)
    else:
        await db.flush()

    return requester, target
