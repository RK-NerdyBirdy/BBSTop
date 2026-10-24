from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.faculty import Faculty
from app.models.project import ApplicationStatus, ProjectApplication


async def get_faculty(db: AsyncSession, faculty_id: int) -> Faculty | None:
    return await db.get(Faculty, faculty_id)


async def get_faculty_by_email(db: AsyncSession, email: str) -> Faculty | None:
    result = await db.execute(select(Faculty).where(func.lower(Faculty.email) == email.lower()))
    return result.scalar_one_or_none()


async def get_faculty_for_update(db: AsyncSession, faculty_id: int) -> Faculty | None:
    result = await db.execute(
        select(Faculty).where(Faculty.faculty_id == faculty_id).with_for_update()
    )
    return result.scalar_one_or_none()


async def list_faculty(db: AsyncSession, active_only: bool = False) -> list[Faculty]:
    stmt = select(Faculty).order_by(Faculty.faculty_name)
    if active_only:
        stmt = stmt.where(Faculty.active.is_(True))
    result = await db.execute(stmt)
    return list(result.scalars().all())


async def approved_counts(db: AsyncSession, semester_id: int) -> dict[int, int]:
    """faculty_id -> number of guide-approved projects in the semester (what the cap counts)."""
    result = await db.execute(
        select(ProjectApplication.faculty_id, func.count())
        .where(
            ProjectApplication.semester_id == semester_id,
            ProjectApplication.status == ApplicationStatus.GUIDE_APPROVED,
            ProjectApplication.faculty_id.is_not(None),
        )
        .group_by(ProjectApplication.faculty_id)
    )
    return {faculty_id: count for faculty_id, count in result.all()}


def effective_cap(faculty: Faculty, default_cap: int) -> int:
    return faculty.max_projects if faculty.max_projects is not None else default_cap
