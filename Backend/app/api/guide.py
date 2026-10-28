from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.dependencies.guide_auth import get_current_guide
from app.models.audit_log import AuditLog
from app.models.faculty import Faculty
from app.models.project import ApplicationStatus as S, ProjectApplication
from app.models.semester import Semester
from app.schemas.project import (
    ApplicationResponse,
    AuditLogResponse,
    ChangeRequestResponse,
    FacultyResponse,
    GuideDashboard,
    Remarks,
    RequiredRemarks,
)
from app.services.change_request_service import change_request_service
from app.services.document_service import document_service
from app.services.project_service import project_service

router = APIRouter()

# A guide sees applications once they've been submitted to them (not while the team is still forming)
GUIDE_VISIBLE = (S.PENDING_GUIDE, S.GUIDE_APPROVED, S.GUIDE_REJECTED, S.GUIDE_FULL, S.DISSOLVED)


@router.get("/me", response_model=FacultyResponse)
async def get_me(guide: Faculty = Depends(get_current_guide)) -> FacultyResponse:
    return FacultyResponse(
        faculty_id=guide.faculty_id,
        employee_id=guide.employee_id,
        faculty_name=guide.faculty_name,
        email=guide.email,
        max_projects=guide.max_projects,
        active=guide.active,
        pfp_url=guide.pfp_url,
        created_at=guide.created_at,
    )


@router.get("/dashboard", response_model=list[GuideDashboard])
async def dashboard(
    db: AsyncSession = Depends(get_db),
    guide: Faculty = Depends(get_current_guide),
) -> list[GuideDashboard]:
    """Capacity per semester: open-registration semesters plus any semester the guide has applications in."""
    pending = dict((await db.execute(
        select(ProjectApplication.semester_id, func.count())
        .where(ProjectApplication.faculty_id == guide.faculty_id, ProjectApplication.status == S.PENDING_GUIDE)
        .group_by(ProjectApplication.semester_id)
    )).all())
    involved = select(ProjectApplication.semester_id).where(ProjectApplication.faculty_id == guide.faculty_id)
    semesters = (await db.execute(
        select(Semester)
        .where(or_(Semester.registration_open.is_(True), Semester.semester_id.in_(involved)))
        .order_by(Semester.semester_id.desc())
    )).scalars().all()

    result = []
    for semester in semesters:
        cap, approved = await project_service.guide_capacity(db, guide, semester.semester_id)
        result.append(GuideDashboard(
            semester_id=semester.semester_id,
            semester_name=semester.semester_name,
            cap=cap,
            approved_count=approved,
            pending_count=pending.get(semester.semester_id, 0),
        ))
    return result


@router.get("/applications", response_model=list[ApplicationResponse])
async def list_applications(
    status: str | None = None,
    semester_id: int | None = None,
    db: AsyncSession = Depends(get_db),
    guide: Faculty = Depends(get_current_guide),
) -> list[ApplicationResponse]:
    conditions = [ProjectApplication.faculty_id == guide.faculty_id]
    conditions.append(ProjectApplication.status == status if status in GUIDE_VISIBLE else ProjectApplication.status.in_(GUIDE_VISIBLE))
    if semester_id is not None:
        conditions.append(ProjectApplication.semester_id == semester_id)
    applications = await project_service.load_many(db, *conditions, order_by=ProjectApplication.submitted_at.asc())
    return await project_service.to_responses(db, applications)


@router.post("/applications/{application_id}/approve", response_model=ApplicationResponse)
async def approve(
    application_id: int,
    payload: Remarks,
    db: AsyncSession = Depends(get_db),
    guide: Faculty = Depends(get_current_guide),
) -> ApplicationResponse:
    application = await project_service.guide_approve(db, guide, application_id, payload.remarks)
    return await project_service.to_response(db, application)


@router.post("/applications/{application_id}/reject", response_model=ApplicationResponse)
async def reject(
    application_id: int,
    payload: RequiredRemarks,
    db: AsyncSession = Depends(get_db),
    guide: Faculty = Depends(get_current_guide),
) -> ApplicationResponse:
    application = await project_service.guide_reject(db, guide, application_id, payload.remarks)
    return await project_service.to_response(db, application)


@router.get("/activity", response_model=list[AuditLogResponse])
async def activity(
    db: AsyncSession = Depends(get_db),
    guide: Faculty = Depends(get_current_guide),
) -> list[AuditLogResponse]:
    """Recent events on this guide's projects (e.g. a member converting to CDC, guide changes)."""
    app_ids = select(func.cast(ProjectApplication.application_id, AuditLog.entity_id.type)).where(
        ProjectApplication.faculty_id == guide.faculty_id
    )
    rows = (await db.execute(
        select(AuditLog)
        .where(AuditLog.entity == "application", AuditLog.entity_id.in_(app_ids))
        .order_by(AuditLog.created_at.desc())
        .limit(50)
    )).scalars().all()
    return [AuditLogResponse.model_validate(r, from_attributes=True) for r in rows]


# ---------- change requests ----------

@router.get("/change-requests", response_model=list[ChangeRequestResponse])
async def list_change_requests(
    waiting: bool = True,
    db: AsyncSession = Depends(get_db),
    guide: Faculty = Depends(get_current_guide),
) -> list[ChangeRequestResponse]:
    """waiting=true: requests this guide must decide (on their projects, or asking them to take one over). false: all involving them."""
    requests = await change_request_service.list_for_guide(db, guide, waiting)
    return await change_request_service.to_responses(db, requests)


@router.post("/change-requests/{request_id}/approve", response_model=ChangeRequestResponse)
async def approve_change_request(
    request_id: int,
    payload: Remarks,
    db: AsyncSession = Depends(get_db),
    guide: Faculty = Depends(get_current_guide),
) -> ChangeRequestResponse:
    request = await change_request_service.guide_decide(db, guide, request_id, True, payload.remarks)
    return (await change_request_service.to_responses(db, [request]))[0]


@router.post("/change-requests/{request_id}/decline", response_model=ChangeRequestResponse)
async def decline_change_request(
    request_id: int,
    payload: RequiredRemarks,
    db: AsyncSession = Depends(get_db),
    guide: Faculty = Depends(get_current_guide),
) -> ChangeRequestResponse:
    request = await change_request_service.guide_decide(db, guide, request_id, False, payload.remarks)
    return (await change_request_service.to_responses(db, [request]))[0]


@router.get("/documents/{document_id}")
async def download_document(
    document_id: int,
    db: AsyncSession = Depends(get_db),
    guide: Faculty = Depends(get_current_guide),
):
    """Proof attached to a change request on one of this guide's projects."""
    if not await change_request_service.guide_can_see_document(db, guide, document_id):
        raise HTTPException(status_code=404, detail="Document not found")
    return await document_service.download(db, document_id)
