import csv
import io

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import ValidationError
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.crud.faculty import get_faculty, list_faculty
from app.dependencies.admin_auth import get_current_admin
from app.models.admin import Admin
from app.models.audit_log import AuditLog
from app.models.change_request import ChangeRequest
from app.models.faculty import Faculty
from app.models.project import ApplicationStatus as S, ProjectApplication, ProjectType
from app.models.semester import Semester
from app.schemas.batch import SemesterResponse
from app.schemas.project import (
    ApplicationResponse,
    AuditLogResponse,
    ChangeRequestResponse,
    FacultyCreate,
    FacultyResponse,
    FacultyUpdate,
    RegistrationOpenUpdate,
    Remarks,
    RequiredRemarks,
    SettingsResponse,
    SettingsUpdate,
)
from app.services.audit_service import audit_service
from app.services.change_request_service import change_request_service
from app.services.document_service import document_service
from app.services.project_service import admin_actor, project_service
from app.services.settings_service import settings_service

router = APIRouter(dependencies=[Depends(get_current_admin)])


def _faculty_response(f: Faculty) -> FacultyResponse:
    return FacultyResponse(
        faculty_id=f.faculty_id,
        employee_id=f.employee_id,
        faculty_name=f.faculty_name,
        email=f.email,
        max_projects=f.max_projects,
        active=f.active,
        pfp_url=f.pfp_url,
        created_at=f.created_at,
    )


def _snapshot(f: Faculty) -> dict:
    return {
        "employee_id": f.employee_id,
        "faculty_name": f.faculty_name,
        "email": f.email,
        "max_projects": f.max_projects,
        "active": f.active,
    }


# ============================================
# FACULTY (GUIDES)
# ============================================

@router.get("/faculty", response_model=list[FacultyResponse])
async def get_faculty_list(db: AsyncSession = Depends(get_db)) -> list[FacultyResponse]:
    return [_faculty_response(f) for f in await list_faculty(db)]


@router.post("/faculty", response_model=FacultyResponse, status_code=status.HTTP_201_CREATED)
async def create_faculty(
    payload: FacultyCreate,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> FacultyResponse:
    faculty = Faculty(
        employee_id=payload.employee_id.strip(),
        faculty_name=payload.faculty_name.strip(),
        email=payload.email.lower(),
        max_projects=payload.max_projects,
        active=payload.active,
    )
    db.add(faculty)
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="A guide with this employee ID or email already exists") from error
    audit_service.log(db, admin_actor(current_admin.admin_id), "faculty.create", "faculty", faculty.faculty_id, _snapshot(faculty))
    await db.commit()
    return _faculty_response(faculty)


@router.patch("/faculty/{faculty_id}", response_model=FacultyResponse)
async def update_faculty(
    faculty_id: int,
    payload: FacultyUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> FacultyResponse:
    faculty = await get_faculty(db, faculty_id)
    if faculty is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Guide not found")
    before = _snapshot(faculty)
    if payload.faculty_name is not None:
        faculty.faculty_name = payload.faculty_name.strip()
    if payload.email is not None:
        faculty.email = payload.email.lower()
    if payload.clear_max_projects:
        faculty.max_projects = None
    elif payload.max_projects is not None:
        faculty.max_projects = payload.max_projects
    if payload.active is not None:
        faculty.active = payload.active
    try:
        await db.flush()
    except IntegrityError as error:
        await db.rollback()
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Another guide already uses this email") from error
    after = _snapshot(faculty)
    if before != after:
        audit_service.log(db, admin_actor(current_admin.admin_id), "faculty.update", "faculty", faculty_id, {
            k: {"from": before[k], "to": after[k]} for k in before if before[k] != after[k]
        })
    await db.commit()
    return _faculty_response(faculty)


@router.post("/faculty/upload-csv")
async def upload_faculty_csv(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> dict:
    """CSV columns: employee_id, faculty_name, email, max_projects (optional). Existing employee IDs are updated."""
    if not file.filename or not file.filename.lower().endswith(".csv"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File must be a CSV file")
    try:
        text = (await file.read()).decode("utf-8-sig")
    except UnicodeDecodeError as error:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="File must be UTF-8 encoded") from error

    reader = csv.DictReader(io.StringIO(text))
    fieldnames = {(f or "").strip() for f in (reader.fieldnames or [])}
    missing = {"employee_id", "faculty_name", "email"} - fieldnames
    if missing:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Missing columns: {', '.join(sorted(missing))}")

    created, updated, errors = 0, 0, []
    for row_number, raw in enumerate(reader, start=2):
        row = {(k or "").strip(): (v or "").strip() for k, v in raw.items()}
        try:
            data = FacultyCreate(
                employee_id=row["employee_id"],
                faculty_name=row["faculty_name"],
                email=row["email"],
                max_projects=int(row["max_projects"]) if row.get("max_projects") else None,
            )
        except (ValidationError, ValueError) as error:
            errors.append({"row": row_number, "error": str(error).splitlines()[0]})
            continue

        existing = (await db.execute(select(Faculty).where(Faculty.employee_id == data.employee_id))).scalar_one_or_none()
        try:
            async with db.begin_nested():
                if existing:
                    existing.faculty_name = data.faculty_name
                    existing.email = data.email.lower()
                    existing.max_projects = data.max_projects
                    updated += 1
                else:
                    db.add(Faculty(
                        employee_id=data.employee_id,
                        faculty_name=data.faculty_name,
                        email=data.email.lower(),
                        max_projects=data.max_projects,
                    ))
                    created += 1
        except IntegrityError:
            errors.append({"row": row_number, "error": f"Email {data.email} is already used by another guide"})
            if existing:
                updated -= 1
            else:
                created -= 1

    audit_service.log(db, admin_actor(current_admin.admin_id), "faculty.csv_upload", "faculty", "bulk", {
        "filename": file.filename, "created": created, "updated": updated, "errors": len(errors),
    })
    await db.commit()
    return {"created": created, "updated": updated, "failed": len(errors), "errors": errors}


# ============================================
# RULES / SETTINGS
# ============================================

@router.get("/settings", response_model=SettingsResponse)
async def get_settings_endpoint(db: AsyncSession = Depends(get_db)) -> SettingsResponse:
    return SettingsResponse(**await settings_service.get_all(db))


@router.put("/settings", response_model=SettingsResponse)
async def update_settings_endpoint(
    payload: SettingsUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> SettingsResponse:
    diff = await settings_service.update(db, payload.model_dump(exclude_none=True))
    if diff:
        audit_service.log(db, admin_actor(current_admin.admin_id), "settings.update", "settings", "rules", diff)
    await db.commit()
    return SettingsResponse(**await settings_service.get_all(db))


@router.patch("/semesters/{semester_id}/registration-open", response_model=SemesterResponse)
async def set_registration_open(
    semester_id: int,
    payload: RegistrationOpenUpdate,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> SemesterResponse:
    semester = await db.get(Semester, semester_id)
    if semester is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Semester not found")
    if semester.archived and payload.registration_open:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="This semester is archived and read-only")
    if payload.registration_open and not semester.assignment_locked:
        # Students can now act on their batches: no more full reshuffles
        semester.assignment_locked = True
        await db.commit()
    if semester.registration_open != payload.registration_open:
        semester.registration_open = payload.registration_open
        audit_service.log(db, admin_actor(current_admin.admin_id), "semester.registration_open", "semester", semester_id, {
            "registration_open": payload.registration_open,
        })
        await db.commit()
    return SemesterResponse.model_validate(semester)


# ============================================
# PROJECTS
# ============================================

@router.get("/projects", response_model=list[ApplicationResponse])
async def list_projects(
    semester_id: int | None = None,
    status_filter: str | None = None,
    faculty_id: int | None = None,
    project_type: str | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[ApplicationResponse]:
    """Defaults to what the HOD tracks: guide-approved projects and approved CDC registrations."""
    conditions = []
    if status_filter:
        conditions.append(ProjectApplication.status == status_filter)
    else:
        conditions.append(ProjectApplication.status.in_([S.GUIDE_APPROVED, S.CDC_APPROVED]))
    if semester_id is not None:
        conditions.append(ProjectApplication.semester_id == semester_id)
    if faculty_id is not None:
        conditions.append(ProjectApplication.faculty_id == faculty_id)
    if project_type:
        conditions.append(ProjectApplication.project_type == project_type)
    applications = await project_service.load_many(db, *conditions)
    return await project_service.to_responses(db, applications)


@router.get("/projects/export-csv")
async def export_projects_csv(
    semester_id: int,
    db: AsyncSession = Depends(get_db),
) -> StreamingResponse:
    """One row per student: approved in-house projects and approved CDC registrations."""
    applications = await project_service.load_many(
        db,
        ProjectApplication.semester_id == semester_id,
        ProjectApplication.status.in_([S.GUIDE_APPROVED, S.CDC_APPROVED]),
        order_by=ProjectApplication.application_id,
    )
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow([
        "application_id", "type", "subtype", "title", "guide_employee_id", "guide_name",
        "register_number", "student_name", "batch", "role",
    ])
    for response in await project_service.to_responses(db, applications):
        for member in response.members:
            if member.status != "ACCEPTED":
                continue
            writer.writerow([
                response.application_id,
                response.project_type,
                response.project_subtype or "",
                response.title or "",
                response.guide.employee_id if response.guide else "",
                response.guide.faculty_name if response.guide else "",
                member.register_number,
                member.student_name,
                member.batch_name or "",
                member.role,
            ])
    output.seek(0)
    return StreamingResponse(
        output,
        media_type="text/csv",
        headers={"Content-Disposition": f"attachment; filename=projects_semester_{semester_id}.csv"},
    )


@router.get("/projects/{application_id}", response_model=ApplicationResponse)
async def get_project(application_id: int, db: AsyncSession = Depends(get_db)) -> ApplicationResponse:
    return await project_service.to_response(db, await project_service.load(db, application_id))


@router.get("/projects/{application_id}/change-requests", response_model=list[ChangeRequestResponse])
async def get_project_change_requests(application_id: int, db: AsyncSession = Depends(get_db)) -> list[ChangeRequestResponse]:
    requests = await change_request_service.list_requests(db, ChangeRequest.application_id == application_id)
    return await change_request_service.to_responses(db, requests)


# ============================================
# CDC REGISTRATIONS
# ============================================

@router.get("/cdc", response_model=list[ApplicationResponse])
async def list_cdc(
    status_filter: str = S.PENDING_HOD,
    semester_id: int | None = None,
    db: AsyncSession = Depends(get_db),
) -> list[ApplicationResponse]:
    conditions = [ProjectApplication.project_type == ProjectType.CDC, ProjectApplication.status == status_filter]
    if semester_id is not None:
        conditions.append(ProjectApplication.semester_id == semester_id)
    applications = await project_service.load_many(db, *conditions, order_by=ProjectApplication.submitted_at.asc())
    return await project_service.to_responses(db, applications)


@router.post("/cdc/{application_id}/approve", response_model=ApplicationResponse)
async def approve_cdc(
    application_id: int,
    payload: Remarks,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> ApplicationResponse:
    application = await project_service.hod_decide_cdc(db, current_admin.admin_id, application_id, True, payload.remarks)
    return await project_service.to_response(db, application)


@router.post("/cdc/{application_id}/reject", response_model=ApplicationResponse)
async def reject_cdc(
    application_id: int,
    payload: RequiredRemarks,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> ApplicationResponse:
    application = await project_service.hod_decide_cdc(db, current_admin.admin_id, application_id, False, payload.remarks)
    return await project_service.to_response(db, application)


# ============================================
# CHANGE REQUESTS
# ============================================

@router.get("/change-requests", response_model=list[ChangeRequestResponse])
async def list_change_requests(
    status_filter: str = "PENDING_HOD",
    db: AsyncSession = Depends(get_db),
) -> list[ChangeRequestResponse]:
    requests = await change_request_service.list_requests(db, ChangeRequest.status == status_filter)
    return await change_request_service.to_responses(db, requests)


@router.post("/change-requests/{request_id}/approve", response_model=ChangeRequestResponse)
async def approve_change_request(
    request_id: int,
    payload: Remarks,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> ChangeRequestResponse:
    request = await change_request_service.hod_decide(db, current_admin.admin_id, request_id, True, payload.remarks)
    return (await change_request_service.to_responses(db, [request]))[0]


@router.post("/change-requests/{request_id}/reject", response_model=ChangeRequestResponse)
async def reject_change_request(
    request_id: int,
    payload: RequiredRemarks,
    db: AsyncSession = Depends(get_db),
    current_admin: Admin = Depends(get_current_admin),
) -> ChangeRequestResponse:
    request = await change_request_service.hod_decide(db, current_admin.admin_id, request_id, False, payload.remarks)
    return (await change_request_service.to_responses(db, [request]))[0]


@router.get("/documents/{document_id}")
async def download_document(document_id: int, db: AsyncSession = Depends(get_db)):
    return await document_service.download(db, document_id)


# ============================================
# AUDIT LOG
# ============================================

@router.get("/audit-log")
async def list_audit_log(
    entity: str | None = None,
    entity_id: str | None = None,
    actor_type: str | None = None,
    limit: int = 50,
    offset: int = 0,
    db: AsyncSession = Depends(get_db),
) -> dict:
    conditions = []
    if entity:
        conditions.append(AuditLog.entity == entity)
    if entity_id:
        conditions.append(AuditLog.entity_id == entity_id)
    if actor_type:
        conditions.append(AuditLog.actor_type == actor_type)
    limit = max(1, min(limit, 200))
    total = (await db.execute(select(func.count()).select_from(AuditLog).where(*conditions))).scalar_one()
    rows = (await db.execute(
        select(AuditLog).where(*conditions).order_by(AuditLog.created_at.desc(), AuditLog.log_id.desc()).limit(limit).offset(offset)
    )).scalars().all()
    return {
        "total": total,
        "items": [AuditLogResponse.model_validate(r, from_attributes=True) for r in rows],
    }
