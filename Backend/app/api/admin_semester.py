from fastapi import APIRouter, Depends, File, UploadFile, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.dependencies.admin_auth import get_current_admin
from app.models.admin import Admin
from app.schemas.batch import SemesterResponse
from app.services.cgpa_file import read_cgpa_file
from app.services.project_service import admin_actor
from app.services.semester_service import semester_service

router = APIRouter(dependencies=[Depends(get_current_admin)])

MAX_SHEET_BYTES = 5 * 1024 * 1024


class RolloverRequest(BaseModel):
    source_semester_id: int
    semester_name: str = Field(..., min_length=1, max_length=100)
    cohort_year: int | None = Field(default=None, ge=2000, le=2100)
    # shuffle=False (default): same batches as the source semester, everyone stays in theirs.
    # shuffle=True: deal students into batch_names with balanced average CGPA.
    shuffle: bool = False
    batch_names: list[str] = Field(default_factory=list, max_length=20)
    exclude: list[str] = Field(default_factory=list)


class ArchiveUpdate(BaseModel):
    archived: bool


class CohortUpdate(BaseModel):
    cohort_year: int | None = Field(default=None, ge=2000, le=2100)


class AssignmentRequest(BaseModel):
    mode: str = "fresh"   # "fresh" | "min_moves"
    seed: int | None = None


class AssignmentApply(BaseModel):
    mode: str
    seed: int
    fingerprint: str


async def _read_sheet(file: UploadFile) -> list[tuple[int, str, str]]:
    content = await file.read(MAX_SHEET_BYTES + 1)
    if len(content) > MAX_SHEET_BYTES:
        from fastapi import HTTPException
        raise HTTPException(status_code=413, detail="File must be at most 5 MB")
    return read_cgpa_file(file.filename or "", content)


@router.get("/semesters/{semester_id}/students")
async def semester_students(semester_id: int, db: AsyncSession = Depends(get_db)) -> dict:
    """Everyone in a semester with their batch and CGPA (works for archived semesters too)."""
    batches = await semester_service.batches(db, semester_id)
    students = await semester_service.students(db, semester_id)
    return {
        "batches": [{"batch_id": b.batch_id, "batch_name": b.batch_name} for b in batches],
        "students": [s.__dict__ for s in students],
        "active_swap_requests": await semester_service.active_swap_count(db, semester_id),
    }


@router.post("/semesters/rollover", status_code=status.HTTP_201_CREATED)
async def rollover(
    payload: RolloverRequest,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    """New semester from an existing one: copy students (in the same batches, or reshuffled), archive the old one."""
    semester, summary = await semester_service.rollover(
        db,
        admin_actor(current_admin.admin_id),
        payload.source_semester_id,
        payload.semester_name,
        payload.cohort_year,
        payload.batch_names,
        payload.exclude,
        payload.shuffle,
    )
    return {"semester": SemesterResponse.model_validate(semester), **summary}


@router.patch("/semesters/{semester_id}/archive", response_model=SemesterResponse)
async def set_archived(
    semester_id: int,
    payload: ArchiveUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> SemesterResponse:
    semester = await semester_service.set_archived(db, admin_actor(current_admin.admin_id), semester_id, payload.archived)
    return SemesterResponse.model_validate(semester)


@router.patch("/semesters/{semester_id}/cohort", response_model=SemesterResponse)
async def set_cohort(
    semester_id: int,
    payload: CohortUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> SemesterResponse:
    from app.services.audit_service import audit_service

    semester = await semester_service.lock(db, semester_id)
    if not semester.archived:
        await semester_service.require_cohort_free(db, payload.cohort_year, exclude_id=semester_id)
    if semester.cohort_year != payload.cohort_year:
        audit_service.log(db, admin_actor(current_admin.admin_id), "semester.cohort", "semester", semester_id, {
            "from": semester.cohort_year, "to": payload.cohort_year,
        })
        semester.cohort_year = payload.cohort_year
        await db.commit()
    return SemesterResponse.model_validate(semester)


@router.post("/semesters/{semester_id}/assign-batches/preview")
async def preview_assignment(semester_id: int, payload: AssignmentRequest, db: AsyncSession = Depends(get_db)) -> dict:
    result = await semester_service.compute_assignment(db, semester_id, payload.mode, payload.seed)
    result.pop("_proposed")
    result.pop("_current")
    return result


@router.post("/semesters/{semester_id}/assign-batches/apply")
async def apply_assignment(
    semester_id: int,
    payload: AssignmentApply,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    return await semester_service.apply_assignment(
        db, admin_actor(current_admin.admin_id), semester_id, payload.mode, payload.seed, payload.fingerprint
    )


@router.post("/semesters/{semester_id}/cgpa/preview")
async def preview_cgpa(semester_id: int, file: UploadFile = File(...), db: AsyncSession = Depends(get_db)) -> dict:
    return await semester_service.preview_cgpa(db, semester_id, await _read_sheet(file))


@router.post("/semesters/{semester_id}/cgpa/apply")
async def apply_cgpa(
    semester_id: int,
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    return await semester_service.apply_cgpa(db, admin_actor(current_admin.admin_id), semester_id, await _read_sheet(file))


@router.post("/semesters/{semester_id}/undo/{kind}")
async def undo(
    semester_id: int,
    kind: str,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    """Undo the latest CGPA update (kind=cgpa) or batch assignment (kind=batches)."""
    return await semester_service.undo_last(db, admin_actor(current_admin.admin_id), semester_id, kind)
