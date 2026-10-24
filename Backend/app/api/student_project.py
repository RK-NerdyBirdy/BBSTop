from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.dependencies.student_auth import get_current_student_record
from app.models.batch import Batch
from app.models.change_request import ChangeRequest, ChangeRequestStatus, ChangeRequestType
from app.models.project import ApplicationStatus, MemberStatus, ProjectApplication, ProjectMember
from app.models.semester import Semester
from app.models.student import Student
from app.models.student_semester_assignment import StudentSemesterAssignment
from app.schemas.project import (
    ApplicationResponse,
    CdcCreate,
    CdcResubmit,
    ChangeRequestCreate,
    ChangeRequestResponse,
    DocumentSummary,
    FacultyCapacity,
    InhouseCreate,
    InhouseUpdate,
    InviteCreate,
    InviteResponse,
    RegistrationSemester,
    SettingsResponse,
    StudentLookupResponse,
)
from app.services.change_request_service import change_request_service
from app.services.document_service import document_service
from app.services.project_service import project_service
from app.services.settings_service import settings_service

router = APIRouter()


async def _my_application(db: AsyncSession, student: Student, application_id: int) -> ProjectApplication:
    """Load an application the student is (or was) part of; 404 otherwise."""
    application = await project_service.load(db, application_id)
    if not any(m.register_number == student.register_number for m in application.members):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Application not found")
    return application


async def _current_application_ids(db: AsyncSession, student: Student) -> dict[int, int]:
    """semester_id -> application_id where the student is an accepted member."""
    result = await db.execute(
        select(ProjectMember.semester_id, ProjectMember.application_id).where(
            ProjectMember.register_number == student.register_number,
            ProjectMember.status == MemberStatus.ACCEPTED,
        )
    )
    return dict(result.all())


@router.get("/registration", response_model=list[RegistrationSemester])
async def registration_overview(
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> list[RegistrationSemester]:
    """Every semester the student is enrolled in, with registration status and their current application."""
    rows = (await db.execute(
        select(Semester, Batch.batch_name)
        .join(StudentSemesterAssignment, StudentSemesterAssignment.semester_id == Semester.semester_id)
        .join(Batch, Batch.batch_id == StudentSemesterAssignment.batch_id)
        .where(
            StudentSemesterAssignment.register_number == student.register_number,
            StudentSemesterAssignment.active.is_(True),
        )
        .order_by(Semester.semester_id.desc())
    )).all()

    current = await _current_application_ids(db, student)
    applications = await project_service.load_many(db, ProjectApplication.application_id.in_(current.values()))
    responses = {r.application_id: r for r in await project_service.to_responses(db, applications)}

    invite_rows = (await db.execute(
        select(ProjectMember.semester_id)
        .join(ProjectApplication, ProjectApplication.application_id == ProjectMember.application_id)
        .where(
            ProjectMember.register_number == student.register_number,
            ProjectMember.status == MemberStatus.INVITED,
            ProjectApplication.status == ApplicationStatus.FORMING,
        )
    )).scalars().all()

    return [
        RegistrationSemester(
            semester_id=semester.semester_id,
            semester_name=semester.semester_name,
            registration_open=semester.registration_open,
            batch_name=batch_name,
            application=responses.get(current.get(semester.semester_id)),
            pending_invites=sum(1 for s in invite_rows if s == semester.semester_id),
        )
        for semester, batch_name in rows
    ]


@router.get("/rules", response_model=SettingsResponse)
async def public_rules(
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> SettingsResponse:
    """Registration rules the forms need (team size, available project types, proof size)."""
    return SettingsResponse(**await settings_service.get_all(db))


@router.get("/history", response_model=list[ApplicationResponse])
async def my_history(
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> list[ApplicationResponse]:
    """All applications the student has been an accepted member of, including withdrawn ones."""
    ids = (await db.execute(
        select(ProjectMember.application_id).where(
            ProjectMember.register_number == student.register_number,
            ProjectMember.status.in_([MemberStatus.ACCEPTED, MemberStatus.RELEASED, MemberStatus.REMOVED]),
        )
    )).scalars().all()
    applications = await project_service.load_many(db, ProjectApplication.application_id.in_(ids))
    return await project_service.to_responses(db, applications)


@router.get("/guides", response_model=list[FacultyCapacity])
async def list_guides(
    semester_id: int,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> list[FacultyCapacity]:
    return await project_service.list_guides_with_capacity(db, semester_id)


@router.get("/lookup", response_model=StudentLookupResponse)
async def lookup_student(
    semester_id: int,
    register_number: str,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> StudentLookupResponse:
    """Autofill a teammate's name from their register number (same semester only)."""
    register_number = register_number.strip().upper()
    row = (await db.execute(
        select(Student.student_name, Batch.batch_name)
        .join(StudentSemesterAssignment, StudentSemesterAssignment.register_number == Student.register_number)
        .join(Batch, Batch.batch_id == StudentSemesterAssignment.batch_id)
        .where(
            Student.register_number == register_number,
            StudentSemesterAssignment.semester_id == semester_id,
            StudentSemesterAssignment.active.is_(True),
        )
    )).first()
    if row is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="No student with that register number in this semester")
    return StudentLookupResponse(
        register_number=register_number,
        student_name=row.student_name,
        batch_name=row.batch_name,
        available=not await project_service.has_team(db, register_number, semester_id),
    )


@router.post("/inhouse", response_model=ApplicationResponse, status_code=status.HTTP_201_CREATED)
async def create_inhouse(
    payload: InhouseCreate,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    application = await project_service.create_inhouse(
        db,
        student,
        payload.semester_id,
        payload.project_subtype,
        payload.title,
        payload.abstract,
        payload.faculty_id,
        payload.teammates,
    )
    return await project_service.to_response(db, application)


@router.post("/cdc", response_model=ApplicationResponse, status_code=status.HTTP_201_CREATED)
async def create_cdc(
    payload: CdcCreate,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    application = await project_service.create_cdc(db, student, payload.semester_id, payload.document_id)
    return await project_service.to_response(db, application)


# ---------- invites ----------

@router.get("/invites", response_model=list[InviteResponse])
async def list_invites(
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> list[InviteResponse]:
    invites = (await db.execute(
        select(ProjectMember).where(
            ProjectMember.register_number == student.register_number,
            ProjectMember.status == MemberStatus.INVITED,
        )
    )).scalars().all()
    applications = await project_service.load_many(
        db,
        ProjectApplication.application_id.in_([i.application_id for i in invites]),
        ProjectApplication.status == ApplicationStatus.FORMING,
    )
    by_id = {r.application_id: r for r in await project_service.to_responses(db, applications)}
    return [
        InviteResponse(member_id=i.member_id, status=i.status, application=by_id[i.application_id])
        for i in invites
        if i.application_id in by_id
    ]


@router.post("/invites/{member_id}/accept", response_model=ApplicationResponse)
async def accept_invite(
    member_id: int,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    application = await project_service.respond_to_invite(db, student, member_id, accept=True)
    return await project_service.to_response(db, application)


@router.post("/invites/{member_id}/decline", response_model=ApplicationResponse)
async def decline_invite(
    member_id: int,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    application = await project_service.respond_to_invite(db, student, member_id, accept=False)
    return await project_service.to_response(db, application)


# ---------- documents ----------

@router.post("/documents", response_model=DocumentSummary, status_code=status.HTTP_201_CREATED)
async def upload_document(
    file: UploadFile = File(...),
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> DocumentSummary:
    document = await document_service.upload(db, student, file)
    return DocumentSummary(
        document_id=document.document_id,
        filename=document.filename,
        content_type=document.content_type,
        size_bytes=document.size_bytes,
        created_at=document.created_at,
    )


@router.get("/documents/{document_id}")
async def download_document(
    document_id: int,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
):
    if await document_service.owner_of(db, document_id) != student.register_number:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return await document_service.download(db, document_id)


# ---------- change requests (consent / cancel) ----------

@router.get("/change-requests/consents", response_model=list[ChangeRequestResponse])
async def my_pending_consents(
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> list[ChangeRequestResponse]:
    """TEAM_CHANGE requests that want to add this student and are waiting for their answer."""
    requests = await change_request_service.list_requests(
        db,
        ChangeRequest.request_type == ChangeRequestType.TEAM_CHANGE,
        ChangeRequest.status == ChangeRequestStatus.PENDING_CONSENT,
        ChangeRequest.payload["consents"].has_key(student.register_number),
    )
    requests = [r for r in requests if r.payload["consents"].get(student.register_number) is None]
    return await change_request_service.to_responses(db, requests)


@router.post("/change-requests/{request_id}/consent", response_model=ChangeRequestResponse)
async def consent_to_change(
    request_id: int,
    accept: bool,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ChangeRequestResponse:
    request = await change_request_service.consent(db, student, request_id, accept)
    return (await change_request_service.to_responses(db, [request]))[0]


@router.post("/change-requests/{request_id}/cancel", response_model=ChangeRequestResponse)
async def cancel_change_request(
    request_id: int,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ChangeRequestResponse:
    request = await change_request_service.cancel(db, student, request_id)
    return (await change_request_service.to_responses(db, [request]))[0]


# ---------- a single application ----------

@router.get("/{application_id}", response_model=ApplicationResponse)
async def get_application(
    application_id: int,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    return await project_service.to_response(db, await _my_application(db, student, application_id))


@router.patch("/{application_id}", response_model=ApplicationResponse)
async def update_application(
    application_id: int,
    payload: InhouseUpdate,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    application = await project_service.update_details(
        db, student, application_id, payload.project_subtype, payload.title, payload.abstract, payload.faculty_id
    )
    return await project_service.to_response(db, application)


@router.post("/{application_id}/members", response_model=ApplicationResponse)
async def invite_member(
    application_id: int,
    payload: InviteCreate,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    application = await project_service.invite_member(db, student, application_id, payload.register_number)
    return await project_service.to_response(db, application)


@router.delete("/{application_id}/members/{member_id}", response_model=ApplicationResponse)
async def remove_member(
    application_id: int,
    member_id: int,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    application = await project_service.remove_member(db, student, application_id, member_id)
    return await project_service.to_response(db, application)


@router.post("/{application_id}/submit", response_model=ApplicationResponse)
async def submit_application(
    application_id: int,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    application = await project_service.submit(db, student, application_id)
    return await project_service.to_response(db, application)


@router.post("/{application_id}/withdraw", response_model=ApplicationResponse)
async def withdraw_application(
    application_id: int,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    application = await project_service.withdraw(db, student, application_id)
    return await project_service.to_response(db, application)


@router.post("/{application_id}/leave", response_model=ApplicationResponse)
async def leave_team(
    application_id: int,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    application = await project_service.leave(db, student, application_id)
    return await project_service.to_response(db, application)


@router.post("/{application_id}/cdc-resubmit", response_model=ApplicationResponse)
async def resubmit_cdc(
    application_id: int,
    payload: CdcResubmit,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ApplicationResponse:
    application = await project_service.resubmit_cdc(db, student, application_id, payload.document_id)
    return await project_service.to_response(db, application)


@router.get("/{application_id}/change-requests", response_model=list[ChangeRequestResponse])
async def list_change_requests(
    application_id: int,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> list[ChangeRequestResponse]:
    await _my_application(db, student, application_id)
    requests = await change_request_service.list_requests(db, ChangeRequest.application_id == application_id)
    return await change_request_service.to_responses(db, requests)


@router.post("/{application_id}/change-requests", response_model=ChangeRequestResponse, status_code=status.HTTP_201_CREATED)
async def create_change_request(
    application_id: int,
    payload: ChangeRequestCreate,
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> ChangeRequestResponse:
    request = await change_request_service.create(db, student, application_id, payload)
    return (await change_request_service.to_responses(db, [request]))[0]
