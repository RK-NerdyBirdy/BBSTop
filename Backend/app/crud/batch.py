from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.semester import Semester
from app.models.batch import Batch


async def create_semester(
    db: AsyncSession,
    semester_name: str,
    swap_allowed: bool = False,
    cohort_year: int | None = None,
) -> Semester:
    """Create a new semester."""
    semester = Semester(
        semester_name=semester_name,
        swap_allowed=swap_allowed,
        cohort_year=cohort_year,
    )
    db.add(semester)
    await db.commit()
    await db.refresh(semester)
    return semester


async def list_semesters(db: AsyncSession) -> list[Semester]:
    """Get all semesters."""
    result = await db.execute(select(Semester))
    return result.scalars().all()


async def get_semester(db: AsyncSession, semester_id: int) -> Semester | None:
    """Get semester by ID."""
    result = await db.execute(
        select(Semester).where(Semester.semester_id == semester_id)
    )
    return result.scalar_one_or_none()


async def create_batch(
    db: AsyncSession,
    batch_name: str,
    semester_id: int,
) -> Batch:
    """Create a new batch."""
    batch = Batch(
        batch_name=batch_name,
        semester_id=semester_id,
    )
    db.add(batch)
    await db.commit()
    await db.refresh(batch)
    return batch


async def list_batches(db: AsyncSession, semester_id: int | None = None) -> list[Batch]:
    """Get all batches, optionally filtered by semester."""
    query = select(Batch)
    if semester_id:
        query = query.where(Batch.semester_id == semester_id)
    result = await db.execute(query)
    return result.scalars().all()


async def get_batch(db: AsyncSession, batch_id: int) -> Batch | None:
    """Get batch by ID."""
    result = await db.execute(
        select(Batch).where(Batch.batch_id == batch_id)
    )
    return result.scalar_one_or_none()
