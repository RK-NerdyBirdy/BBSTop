"""Everything the student dashboard shows, in one request.

Replaces the dashboard's seven separate calls (me, batch, incoming, outgoing, swap-allowed,
phone-status, swap-lock), which were slow while the server wakes up, and adds the project
status, invites and consent requests that the dashboard didn't show at all.
"""

from datetime import datetime

from fastapi import APIRouter, Depends
from pydantic import BaseModel
from sqlalchemy import or_, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import aliased

from app.core.database import get_db
from app.dependencies.student_auth import get_current_student_record
from app.models.batch import Batch
from app.models.change_request import ChangeRequest, ChangeRequestStatus, ChangeRequestType
from app.models.faculty import Faculty
from app.models.project import ApplicationStatus, MemberStatus, ProjectApplication, ProjectMember
from app.models.semester import Semester
from app.models.student import Student
from app.models.student_semester_assignment import StudentSemesterAssignment
from app.models.swap_request import SwapRequest
from app.services.settings_service import settings_service
from app.services.swap_service import swap_candidate_service

router = APIRouter()


class DashboardStudent(BaseModel):
    register_number: str
    name: str
    email: str
    pfp_url: str | None
    has_phone: bool
    open_to_swap: bool


class DashboardProject(BaseModel):
    application_id: int
    status: str
    project_type: str
    title: str | None
    guide_name: str | None


class DashboardSemester(BaseModel):
    semester_id: int
    semester_name: str
    cohort_year: int | None
    batch_name: str
    cgpa: float
    swap_allowed: bool
    swaps_used: int = 0
    swap_limit: int = 1
    registration_open: bool
    project: DashboardProject | None


class DashboardSwap(BaseModel):
    request_id: int
    status: str
    semester_id: int
    other_name: str
    other_register_number: str
    other_batch: str
    created_at: datetime


class StudentDashboard(BaseModel):
    student: DashboardStudent
    semesters: list[DashboardSemester]
    incoming_pending: list[DashboardSwap]  # waiting for this student's answer
    outgoing_active: list[DashboardSwap]  # sent by this student, pending or accepted
    eligible_count: int  # swap partners available now (swap-open semesters only)
    project_invites: int
    team_consents: int


@router.get("/dashboard", response_model=StudentDashboard)
async def student_dashboard(
    db: AsyncSession = Depends(get_db),
    student: Student = Depends(get_current_student_record),
) -> StudentDashboard:
    # Current (non-archived) semesters with batch
    rows = (await db.execute(
        select(StudentSemesterAssignment, Semester, Batch.batch_name)
        .join(Semester, Semester.semester_id == StudentSemesterAssignment.semester_id)
        .join(Batch, Batch.batch_id == StudentSemesterAssignment.batch_id)
        .where(
            StudentSemesterAssignment.register_number == student.register_number,
            StudentSemesterAssignment.active.is_(True),
            Semester.archived.is_(False),
        )
        .order_by(Semester.semester_id.desc())
    )).all()
    assignment_ids = [a.assignment_id for a, _, _ in rows]

    # This student's current application per semester
    projects: dict[int, DashboardProject] = {}
    if rows:
        project_rows = (await db.execute(
            select(ProjectApplication, Faculty.faculty_name)
            .join(ProjectMember, ProjectMember.application_id == ProjectApplication.application_id)
            .outerjoin(Faculty, Faculty.faculty_id == ProjectApplication.faculty_id)
            .where(
                ProjectMember.register_number == student.register_number,
                ProjectMember.status == MemberStatus.ACCEPTED,
                ProjectApplication.semester_id.in_([s.semester_id for _, s, _ in rows]),
            )
        )).all()
        for application, guide_name in project_rows:
            projects[application.semester_id] = DashboardProject(
                application_id=application.application_id,
                status=application.status,
                project_type=application.project_type,
                title=application.title,
                guide_name=guide_name,
            )

    used = await swap_candidate_service.swaps_used(db, [a.assignment_id for a, _, _ in rows])
    swap_limit = await settings_service.get(db, "swap_limit")
    semesters = [
        DashboardSemester(
            semester_id=s.semester_id,
            semester_name=s.semester_name,
            cohort_year=s.cohort_year,
            batch_name=batch_name,
            cgpa=float(a.cgpa),
            swap_allowed=s.swap_allowed,
            swaps_used=used.get(a.assignment_id, 0),
            swap_limit=swap_limit,
            registration_open=s.registration_open,
            project=projects.get(s.semester_id),
        )
        for a, s, batch_name in rows
    ]

    # Swap requests touching this student's current assignments, with the other student's details
    incoming: list[DashboardSwap] = []
    outgoing: list[DashboardSwap] = []
    if assignment_ids:
        other = aliased(StudentSemesterAssignment)
        mine = aliased(StudentSemesterAssignment)
        swap_rows = (await db.execute(
            select(SwapRequest, mine, other, Student.student_name, Batch.batch_name)
            .join(mine, or_(
                mine.assignment_id == SwapRequest.requester_assignment_id,
                mine.assignment_id == SwapRequest.target_assignment_id,
            ))
            .join(other, other.assignment_id == (
                # the assignment on the other side of the request
                SwapRequest.requester_assignment_id + SwapRequest.target_assignment_id - mine.assignment_id
            ))
            .join(Student, Student.register_number == other.register_number)
            .join(Batch, Batch.batch_id == other.batch_id)
            .where(
                mine.assignment_id.in_(assignment_ids),
                SwapRequest.status.in_(["PENDING", "ACCEPTED"]),
            )
            .order_by(SwapRequest.created_at.desc())
        )).all()
        for request, my_assignment, other_assignment, other_name, other_batch in swap_rows:
            item = DashboardSwap(
                request_id=request.request_id,
                status=request.status,
                semester_id=my_assignment.semester_id,
                other_name=other_name,
                other_register_number=other_assignment.register_number,
                other_batch=other_batch,
                created_at=request.created_at,
            )
            if request.target_assignment_id == my_assignment.assignment_id:
                if request.status == "PENDING":
                    incoming.append(item)
            else:
                outgoing.append(item)

    # Swap partners available right now (same rules as Find Swaps)
    eligible = 0
    for a, s, _ in rows:
        if not s.swap_allowed:
            continue
        candidates = await swap_candidate_service.get_swap_candidates(db, a.assignment_id)
        if candidates:
            locked = set((await db.execute(
                select(Student.register_number).where(
                    Student.register_number.in_([c.register_number for c in candidates]),
                    Student.swap_lock.is_(True),
                )
            )).scalars().all())
            eligible += sum(1 for c in candidates if c.register_number not in locked)

    invites = (await db.execute(
        select(ProjectMember.member_id)
        .join(ProjectApplication, ProjectApplication.application_id == ProjectMember.application_id)
        .join(Semester, Semester.semester_id == ProjectApplication.semester_id)
        .where(
            ProjectMember.register_number == student.register_number,
            ProjectMember.status == MemberStatus.INVITED,
            ProjectApplication.status == ApplicationStatus.FORMING,
            Semester.archived.is_(False),
        )
    )).all()

    consents = [
        r for r in (await db.execute(
            select(ChangeRequest).where(
                ChangeRequest.request_type == ChangeRequestType.TEAM_CHANGE,
                ChangeRequest.status == ChangeRequestStatus.PENDING_CONSENT,
                ChangeRequest.payload["consents"].has_key(student.register_number),
            )
        )).scalars().all()
        if r.payload["consents"].get(student.register_number) is None
    ]

    return StudentDashboard(
        student=DashboardStudent(
            register_number=student.register_number,
            name=student.student_name,
            email=student.email,
            pfp_url=student.pfp_url,
            has_phone=bool(student.phone_number),
            open_to_swap=not student.swap_lock,
        ),
        semesters=semesters,
        incoming_pending=incoming,
        outgoing_active=outgoing,
        eligible_count=eligible,
        project_invites=len(invites),
        team_consents=len(consents),
    )
