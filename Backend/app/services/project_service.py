"""Project registration rules: team formation, consent, guide review, CDC registration.

All state transitions live here so the routes stay thin. Each public method validates,
mutates, writes an audit row, fires a notification and commits.

Lock order (to avoid deadlocks): faculty row -> application row -> student rows (sorted).
"""

from datetime import datetime, timezone

from fastapi import HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from app.crud.faculty import approved_counts, effective_cap, get_faculty, get_faculty_for_update
from app.models.batch import Batch
from app.models.change_request import ChangeRequest, ChangeRequestStatus, ChangeRequestType
from app.models.document import Document
from app.models.faculty import Faculty
from app.models.project import (
    ApplicationStatus as S,
    MemberRole,
    MemberStatus,
    ProjectApplication,
    ProjectMember,
    ProjectType,
)
from app.models.semester import Semester
from app.models.student import Student
from app.models.student_semester_assignment import StudentSemesterAssignment
from app.schemas.project import (
    ApplicationResponse,
    DocumentSummary,
    FacultyCapacity,
    GuideSummary,
    MemberResponse,
)
from app.services.audit_service import SYSTEM, Actor, audit_service
from app.services.notify import notify
from app.services.settings_service import settings_service

# Leader can change team / details in these states (not while the guide is reviewing)
EDITABLE = (S.FORMING, S.GUIDE_REJECTED, S.GUIDE_FULL)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _error(code: int, detail: str) -> HTTPException:
    return HTTPException(status_code=code, detail=detail)


def student_actor(student: Student) -> Actor:
    return Actor("student", student.register_number)


def guide_actor(faculty: Faculty) -> Actor:
    return Actor("guide", str(faculty.faculty_id))


def admin_actor(admin_id: int) -> Actor:
    return Actor("admin", str(admin_id))


class ProjectService:
    # ------------------------------------------------------------------ loading

    @staticmethod
    def _application_query():
        return select(ProjectApplication).options(
            selectinload(ProjectApplication.members).selectinload(ProjectMember.student),
            selectinload(ProjectApplication.faculty),
            selectinload(ProjectApplication.semester),
        )

    async def load(self, db: AsyncSession, application_id: int) -> ProjectApplication:
        result = await db.execute(
            self._application_query()
            .where(ProjectApplication.application_id == application_id)
            .execution_options(populate_existing=True)
        )
        application = result.scalar_one_or_none()
        if application is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Application not found")
        return application

    async def load_many(self, db: AsyncSession, *conditions, order_by=None) -> list[ProjectApplication]:
        stmt = self._application_query().where(*conditions).execution_options(populate_existing=True)
        stmt = stmt.order_by(order_by if order_by is not None else ProjectApplication.created_at.desc())
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def _lock_application(self, db: AsyncSession, application_id: int) -> ProjectApplication:
        result = await db.execute(
            select(ProjectApplication)
            .where(ProjectApplication.application_id == application_id)
            .with_for_update()
        )
        application = result.scalar_one_or_none()
        if application is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Application not found")
        application = await self.load(db, application_id)
        if application.semester.archived:
            raise _error(status.HTTP_400_BAD_REQUEST, "This semester is archived and read-only")
        return application

    # ------------------------------------------------------------------ serialising

    async def to_responses(self, db: AsyncSession, applications: list[ProjectApplication]) -> list[ApplicationResponse]:
        if not applications:
            return []

        regs = {m.register_number for a in applications for m in a.members}
        semester_ids = {a.semester_id for a in applications}
        batch_rows = await db.execute(
            select(
                StudentSemesterAssignment.register_number,
                StudentSemesterAssignment.semester_id,
                Batch.batch_name,
            )
            .join(Batch, Batch.batch_id == StudentSemesterAssignment.batch_id)
            .where(
                StudentSemesterAssignment.register_number.in_(regs),
                StudentSemesterAssignment.semester_id.in_(semester_ids),
            )
        )
        batch_by_key = {(reg, sem): name for reg, sem, name in batch_rows.all()}

        doc_ids = {a.proof_document_id for a in applications if a.proof_document_id}
        docs = await self.document_summaries(db, doc_ids)

        responses = []
        for a in applications:
            members = [
                MemberResponse(
                    member_id=m.member_id,
                    register_number=m.register_number,
                    student_name=m.student.student_name,
                    email=m.student.email,
                    batch_name=batch_by_key.get((m.register_number, a.semester_id)),
                    role=m.role,
                    status=m.status,
                    responded_at=m.responded_at,
                )
                for m in a.members
                if m.status in (MemberStatus.INVITED, MemberStatus.ACCEPTED, MemberStatus.DECLINED)
                or a.status in S.CLOSED
            ]
            # Leader first, then by invite order
            members.sort(key=lambda m: (m.role != MemberRole.LEADER, m.member_id))
            responses.append(
                ApplicationResponse(
                    application_id=a.application_id,
                    semester_id=a.semester_id,
                    semester_name=a.semester.semester_name,
                    leader_register_number=a.leader_register_number,
                    project_type=a.project_type,
                    project_subtype=a.project_subtype,
                    title=a.title,
                    abstract=a.abstract,
                    status=a.status,
                    origin_request_id=a.origin_request_id,
                    guide=GuideSummary(
                        faculty_id=a.faculty.faculty_id,
                        employee_id=a.faculty.employee_id,
                        faculty_name=a.faculty.faculty_name,
                        email=a.faculty.email,
                    )
                    if a.faculty
                    else None,
                    proof_document=docs.get(a.proof_document_id),
                    guide_remarks=a.guide_remarks,
                    hod_remarks=a.hod_remarks,
                    members=members,
                    submitted_at=a.submitted_at,
                    decided_at=a.decided_at,
                    created_at=a.created_at,
                    updated_at=a.updated_at,
                )
            )
        return responses

    async def to_response(self, db: AsyncSession, application: ProjectApplication) -> ApplicationResponse:
        return (await self.to_responses(db, [application]))[0]

    @staticmethod
    async def document_summaries(db: AsyncSession, doc_ids: set[int]) -> dict[int, DocumentSummary]:
        if not doc_ids:
            return {}
        rows = await db.execute(
            select(
                Document.document_id,
                Document.filename,
                Document.content_type,
                Document.size_bytes,
                Document.created_at,
            ).where(Document.document_id.in_(doc_ids))
        )
        return {
            r.document_id: DocumentSummary(
                document_id=r.document_id,
                filename=r.filename,
                content_type=r.content_type,
                size_bytes=r.size_bytes,
                created_at=r.created_at,
            )
            for r in rows.all()
        }

    # ------------------------------------------------------------------ shared checks

    @staticmethod
    async def get_assignment(db: AsyncSession, register_number: str, semester_id: int) -> StudentSemesterAssignment | None:
        result = await db.execute(
            select(StudentSemesterAssignment).where(
                StudentSemesterAssignment.register_number == register_number,
                StudentSemesterAssignment.semester_id == semester_id,
                StudentSemesterAssignment.active.is_(True),
            )
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _require_open_semester(db: AsyncSession, semester_id: int, application: ProjectApplication | None = None) -> Semester:
        semester = await db.get(Semester, semester_id)
        if semester is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Semester not found")
        # A project registered after a category change carries on while registration is closed
        if not semester.registration_open and not (application and application.origin_request_id):
            raise _error(status.HTTP_400_BAD_REQUEST, "Project registration is closed for this semester")
        return semester

    @staticmethod
    async def approved_move(db: AsyncSession, register_number: str, semester_id: int) -> ChangeRequest | None:
        """The student's latest guide-approved category change in this semester, if any."""
        result = await db.execute(
            select(ChangeRequest)
            .join(ProjectApplication, ProjectApplication.application_id == ChangeRequest.application_id)
            .where(
                ProjectApplication.semester_id == semester_id,
                ChangeRequest.request_type == ChangeRequestType.CATEGORY_CHANGE,
                ChangeRequest.status == ChangeRequestStatus.APPROVED,
                ChangeRequest.requested_by == register_number,
            )
            .order_by(ChangeRequest.decided_at.desc())
            .limit(1)
        )
        return result.scalar_one_or_none()

    @staticmethod
    async def _check_team_size(db: AsyncSession, subtype: str | None, size: int, *, check_min: bool = True) -> None:
        low, high = await settings_service.team_limits(db, subtype)
        label = (subtype or "").replace("_", "-").title()
        if size > high:
            raise _error(status.HTTP_400_BAD_REQUEST, f"A team can have at most {high} members")
        if check_min and size < low:
            raise _error(status.HTTP_400_BAD_REQUEST, f"A {label} team needs at least {low} members")

    @staticmethod
    async def _lock_students(db: AsyncSession, register_numbers: set[str]) -> dict[str, Student]:
        students: dict[str, Student] = {}
        for reg in sorted(register_numbers):
            result = await db.execute(
                select(Student).where(Student.register_number == reg).with_for_update()
            )
            student = result.scalar_one_or_none()
            if student is not None:
                students[reg] = student
        return students

    @staticmethod
    async def has_team(db: AsyncSession, register_number: str, semester_id: int) -> bool:
        result = await db.execute(
            select(ProjectMember.member_id).where(
                ProjectMember.register_number == register_number,
                ProjectMember.semester_id == semester_id,
                ProjectMember.status == MemberStatus.ACCEPTED,
            )
        )
        return result.first() is not None

    @staticmethod
    async def _decline_other_invites(
        db: AsyncSession, register_number: str, semester_id: int, except_member_id: int | None = None
    ) -> list[int]:
        """Auto-decline a student's outstanding invites once they're committed to a team / CDC."""
        stmt = select(ProjectMember).where(
            ProjectMember.register_number == register_number,
            ProjectMember.semester_id == semester_id,
            ProjectMember.status == MemberStatus.INVITED,
        )
        if except_member_id is not None:
            stmt = stmt.where(ProjectMember.member_id != except_member_id)
        result = await db.execute(stmt.with_for_update())
        declined = []
        for invite in result.scalars().all():
            invite.status = MemberStatus.DECLINED
            invite.responded_at = _now()
            declined.append(invite.application_id)
        return declined

    async def guide_capacity(self, db: AsyncSession, faculty: Faculty, semester_id: int) -> tuple[int, int]:
        default_cap = await settings_service.get(db, "default_guide_cap")
        counts = await approved_counts(db, semester_id)
        return effective_cap(faculty, default_cap), counts.get(faculty.faculty_id, 0)

    async def list_guides_with_capacity(self, db: AsyncSession, semester_id: int) -> list[FacultyCapacity]:
        default_cap = await settings_service.get(db, "default_guide_cap")
        counts = await approved_counts(db, semester_id)
        result = await db.execute(
            select(Faculty).where(Faculty.active.is_(True)).order_by(Faculty.faculty_name)
        )
        guides = []
        for f in result.scalars().all():
            cap = effective_cap(f, default_cap)
            approved = counts.get(f.faculty_id, 0)
            guides.append(
                FacultyCapacity(
                    faculty_id=f.faculty_id,
                    employee_id=f.employee_id,
                    faculty_name=f.faculty_name,
                    email=f.email,
                    cap=cap,
                    approved_count=approved,
                    slots_left=max(cap - approved, 0),
                )
            )
        return guides

    async def _require_available_guide(self, db: AsyncSession, faculty_id: int, semester_id: int) -> Faculty:
        faculty = await get_faculty(db, faculty_id)
        if faculty is None or not faculty.active:
            raise _error(status.HTTP_400_BAD_REQUEST, "Selected guide is not available")
        cap, approved = await self.guide_capacity(db, faculty, semester_id)
        if approved >= cap:
            raise _error(status.HTTP_409_CONFLICT, f"{faculty.faculty_name} has no project slots left. Please pick another guide.")
        return faculty

    @staticmethod
    async def _validate_subtype(db: AsyncSession, subtype: str) -> None:
        enabled = await settings_service.get(db, "enabled_subtypes")
        if subtype not in enabled:
            raise _error(status.HTTP_400_BAD_REQUEST, f"Project type {subtype} is not available")

    async def _validate_invitee(
        self, db: AsyncSession, register_number: str, semester_id: int, leader: str
    ) -> Student:
        if register_number == leader:
            raise _error(status.HTTP_400_BAD_REQUEST, "You can't invite yourself")
        student = await db.get(Student, register_number)
        if student is None or await self.get_assignment(db, register_number, semester_id) is None:
            raise _error(status.HTTP_400_BAD_REQUEST, f"{register_number} is not a student of this semester")
        if await self.has_team(db, register_number, semester_id):
            raise _error(status.HTTP_409_CONFLICT, f"{register_number} is already in a team or registered for CDC")
        return student

    @staticmethod
    def _live_members(application: ProjectApplication) -> list[ProjectMember]:
        return [m for m in application.members if m.status in (MemberStatus.INVITED, MemberStatus.ACCEPTED)]

    @staticmethod
    def _member_emails(application: ProjectApplication, statuses=(MemberStatus.ACCEPTED,)) -> list[str]:
        return [m.student.email for m in application.members if m.status in statuses]

    @staticmethod
    def _require_leader(application: ProjectApplication, student: Student) -> None:
        if application.leader_register_number != student.register_number:
            raise _error(status.HTTP_403_FORBIDDEN, "Only the team leader can do this")

    @staticmethod
    async def _flush(db: AsyncSession, conflict_detail: str) -> None:
        try:
            await db.flush()
        except IntegrityError as error:
            await db.rollback()
            raise _error(status.HTTP_409_CONFLICT, conflict_detail) from error

    @staticmethod
    async def require_own_document(db: AsyncSession, document_id: int, student: Student) -> Document:
        document = await db.get(Document, document_id)
        if document is None or document.owner_register_number != student.register_number:
            raise _error(status.HTTP_400_BAD_REQUEST, "Proof document not found")
        return document

    # ------------------------------------------------------------------ student: create

    async def create_inhouse(
        self,
        db: AsyncSession,
        student: Student,
        semester_id: int,
        subtype: str,
        title: str,
        abstract: str,
        faculty_id: int,
        teammates: list[str],
    ) -> ProjectApplication:
        semester = await db.get(Semester, semester_id)
        if semester is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Semester not found")
        # After a guide-approved category change the student registers again, even with registration closed,
        # but only in the category they moved to
        origin: ChangeRequest | None = None
        if not semester.registration_open:
            origin = await self.approved_move(db, student.register_number, semester_id)
            if origin is None:
                raise _error(status.HTTP_400_BAD_REQUEST, "Project registration is closed for this semester")
            if subtype != origin.payload["to_subtype"]:
                raise _error(status.HTTP_400_BAD_REQUEST, "Register in the category your guide approved the move to")
        else:
            await self._validate_subtype(db, subtype)

        teammates = [t.strip().upper() for t in teammates if t.strip()]
        if len(set(teammates)) != len(teammates):
            raise _error(status.HTTP_400_BAD_REQUEST, "The same teammate is listed twice")
        await self._check_team_size(db, subtype, 1 + len(teammates))

        await self._lock_students(db, {student.register_number})
        if await self.get_assignment(db, student.register_number, semester_id) is None:
            raise _error(status.HTTP_400_BAD_REQUEST, "You are not enrolled in this semester")
        if await self.has_team(db, student.register_number, semester_id):
            raise _error(status.HTTP_409_CONFLICT, "You already have an active application this semester")
        invitees = [
            await self._validate_invitee(db, reg, semester_id, student.register_number) for reg in teammates
        ]
        faculty = await self._require_available_guide(db, faculty_id, semester_id)

        application = ProjectApplication(
            semester_id=semester_id,
            leader_register_number=student.register_number,
            project_type=ProjectType.INHOUSE,
            project_subtype=subtype,
            title=title.strip(),
            abstract=abstract.strip(),
            faculty_id=faculty.faculty_id,
            origin_request_id=origin.request_id if origin else None,
            status=S.FORMING if invitees else S.PENDING_GUIDE,
            submitted_at=None if invitees else _now(),
        )
        db.add(application)
        await db.flush()

        db.add(ProjectMember(
            application_id=application.application_id,
            register_number=student.register_number,
            semester_id=semester_id,
            role=MemberRole.LEADER,
            status=MemberStatus.ACCEPTED,
            responded_at=_now(),
        ))
        for invitee in invitees:
            db.add(ProjectMember(
                application_id=application.application_id,
                register_number=invitee.register_number,
                semester_id=semester_id,
                role=MemberRole.MEMBER,
                status=MemberStatus.INVITED,
            ))
        await self._decline_other_invites(db, student.register_number, semester_id)
        await self._flush(db, "You already have an active application this semester")

        audit_service.log(db, student_actor(student), "application.create", "application", application.application_id, {
            "type": ProjectType.INHOUSE,
            "subtype": subtype,
            "title": application.title,
            "faculty_id": faculty.faculty_id,
            "invited": teammates,
            "status": application.status,
            "origin_request_id": application.origin_request_id,
        })
        await db.commit()

        if invitees:
            notify("team.invited", [i.email for i in invitees], {"application_id": application.application_id, "leader": student.student_name})
        else:
            notify("application.submitted", [faculty.email], {"application_id": application.application_id})
        return await self.load(db, application.application_id)

    async def create_cdc(self, db: AsyncSession, student: Student, semester_id: int, document_id: int) -> ProjectApplication:
        await self._require_open_semester(db, semester_id)
        if not await settings_service.get(db, "cdc_enabled"):
            raise _error(status.HTTP_400_BAD_REQUEST, "CDC registration is currently disabled")
        await self._lock_students(db, {student.register_number})
        if await self.get_assignment(db, student.register_number, semester_id) is None:
            raise _error(status.HTTP_400_BAD_REQUEST, "You are not enrolled in this semester")
        if await self.has_team(db, student.register_number, semester_id):
            raise _error(status.HTTP_409_CONFLICT, "You already have an active application this semester")
        await self.require_own_document(db, document_id, student)

        application = ProjectApplication(
            semester_id=semester_id,
            leader_register_number=student.register_number,
            project_type=ProjectType.CDC,
            proof_document_id=document_id,
            status=S.PENDING_HOD,
            submitted_at=_now(),
        )
        db.add(application)
        await db.flush()
        db.add(ProjectMember(
            application_id=application.application_id,
            register_number=student.register_number,
            semester_id=semester_id,
            role=MemberRole.LEADER,
            status=MemberStatus.ACCEPTED,
            responded_at=_now(),
        ))
        await self._decline_other_invites(db, student.register_number, semester_id)
        await self._flush(db, "You already have an active application this semester")

        audit_service.log(db, student_actor(student), "application.create", "application", application.application_id, {
            "type": ProjectType.CDC,
            "document_id": document_id,
            "status": application.status,
        })
        await db.commit()
        notify("cdc.submitted", ["hod"], {"application_id": application.application_id, "student": student.register_number})
        return await self.load(db, application.application_id)

    # ------------------------------------------------------------------ student: invites

    async def respond_to_invite(self, db: AsyncSession, student: Student, member_id: int, accept: bool) -> ProjectApplication:
        invite = await db.get(ProjectMember, member_id)
        if invite is None or invite.register_number != student.register_number:
            raise _error(status.HTTP_404_NOT_FOUND, "Invite not found")

        application = await self._lock_application(db, invite.application_id)
        await self._lock_students(db, {student.register_number})
        invite = next(m for m in application.members if m.member_id == member_id)
        if invite.status != MemberStatus.INVITED or application.status != S.FORMING:
            raise _error(status.HTTP_400_BAD_REQUEST, "This invite is no longer open")

        if accept:
            if await self.has_team(db, student.register_number, application.semester_id):
                raise _error(status.HTTP_409_CONFLICT, "You are already in a team or registered for CDC this semester")
            invite.status = MemberStatus.ACCEPTED
            invite.responded_at = _now()
            await self._decline_other_invites(db, student.register_number, application.semester_id, except_member_id=member_id)
            # Everyone has answered, nobody is still pending and the team is big enough -> goes to the guide
            low, _ = await settings_service.team_limits(db, application.project_subtype)
            accepted = sum(1 for m in application.members if m.status == MemberStatus.ACCEPTED)
            if not any(m.status == MemberStatus.INVITED for m in application.members) and accepted >= low:
                application.status = S.PENDING_GUIDE
                application.submitted_at = _now()
        else:
            invite.status = MemberStatus.DECLINED
            invite.responded_at = _now()
        await self._flush(db, "You are already in a team or registered for CDC this semester")

        audit_service.log(db, student_actor(student), "invite.accept" if accept else "invite.decline", "application", application.application_id, {
            "member_id": member_id,
            "application_status": application.status,
        })
        await db.commit()

        leader_email = next(m.student.email for m in application.members if m.role == MemberRole.LEADER)
        notify("invite.accepted" if accept else "invite.declined", [leader_email], {"application_id": application.application_id, "student": student.register_number})
        if application.status == S.PENDING_GUIDE:
            notify("application.submitted", [application.faculty.email], {"application_id": application.application_id})
        return await self.load(db, application.application_id)

    # ------------------------------------------------------------------ leader: edit team / details

    async def invite_member(self, db: AsyncSession, student: Student, application_id: int, register_number: str) -> ProjectApplication:
        application = await self._lock_application(db, application_id)
        self._require_leader(application, student)
        if application.project_type != ProjectType.INHOUSE or application.status not in EDITABLE:
            raise _error(status.HTTP_400_BAD_REQUEST, "The team can't be changed at this stage")
        await self._require_open_semester(db, application.semester_id, application)

        register_number = register_number.strip().upper()
        if any(m.register_number == register_number for m in self._live_members(application)):
            raise _error(status.HTTP_400_BAD_REQUEST, f"{register_number} is already on this team")
        await self._check_team_size(db, application.project_subtype, len(self._live_members(application)) + 1, check_min=False)
        invitee = await self._validate_invitee(db, register_number, application.semester_id, student.register_number)

        db.add(ProjectMember(
            application_id=application.application_id,
            register_number=register_number,
            semester_id=application.semester_id,
            role=MemberRole.MEMBER,
            status=MemberStatus.INVITED,
        ))
        previous = application.status
        application.status = S.FORMING
        await self._flush(db, f"{register_number} is already invited")

        audit_service.log(db, student_actor(student), "member.invite", "application", application_id, {
            "register_number": register_number, "from_status": previous,
        })
        await db.commit()
        notify("team.invited", [invitee.email], {"application_id": application_id, "leader": student.student_name})
        return await self.load(db, application_id)

    async def remove_member(self, db: AsyncSession, student: Student, application_id: int, member_id: int) -> ProjectApplication:
        application = await self._lock_application(db, application_id)
        self._require_leader(application, student)
        if application.status not in EDITABLE:
            raise _error(status.HTTP_400_BAD_REQUEST, "The team can't be changed at this stage. Send a change request to your guide instead.")
        member = next((m for m in self._live_members(application) if m.member_id == member_id), None)
        if member is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Member not found")
        if member.role == MemberRole.LEADER:
            raise _error(status.HTTP_400_BAD_REQUEST, "The leader can't be removed. Withdraw the application instead.")

        previous_member_status = member.status
        member.status = MemberStatus.REMOVED
        member.responded_at = _now()
        await db.flush()

        audit_service.log(db, student_actor(student), "member.remove", "application", application_id, {
            "register_number": member.register_number, "was": previous_member_status,
        })
        await db.commit()
        notify("team.removed", [member.student.email], {"application_id": application_id})
        return await self.load(db, application_id)

    async def leave(self, db: AsyncSession, student: Student, application_id: int) -> ProjectApplication:
        """A non-leader member leaves before guide approval (after approval it's a change request to the guide)."""
        application = await self._lock_application(db, application_id)
        member = next(
            (m for m in application.members if m.register_number == student.register_number and m.status == MemberStatus.ACCEPTED),
            None,
        )
        if member is None:
            raise _error(status.HTTP_404_NOT_FOUND, "You are not on this team")
        if member.role == MemberRole.LEADER:
            raise _error(status.HTTP_400_BAD_REQUEST, "The leader can't leave. Withdraw the application instead.")
        if application.status not in EDITABLE + (S.PENDING_GUIDE,):
            raise _error(status.HTTP_400_BAD_REQUEST, "You can't leave an approved project. Ask your guide to move you to CDC or another category instead.")

        previous = application.status
        member.status = MemberStatus.REMOVED
        member.responded_at = _now()
        # Team changed under the guide's nose: send it back to the leader to resubmit
        if previous == S.PENDING_GUIDE:
            application.status = S.FORMING
            application.submitted_at = None
        audit_service.log(db, student_actor(student), "member.leave", "application", application_id, {"from_status": previous})
        await db.commit()

        notify_to = [m.student.email for m in application.members if m.role == MemberRole.LEADER]
        if previous == S.PENDING_GUIDE and application.faculty:
            notify_to.append(application.faculty.email)
        notify("team.member_left", notify_to, {"application_id": application_id, "student": student.register_number})
        return await self.load(db, application_id)

    async def update_details(
        self,
        db: AsyncSession,
        student: Student,
        application_id: int,
        subtype: str | None,
        title: str | None,
        abstract: str | None,
        faculty_id: int | None,
    ) -> ProjectApplication:
        application = await self._lock_application(db, application_id)
        self._require_leader(application, student)
        if application.project_type != ProjectType.INHOUSE or application.status not in EDITABLE:
            raise _error(status.HTTP_400_BAD_REQUEST, "Details can't be changed at this stage")

        changes: dict[str, dict] = {}
        if subtype is not None and subtype != application.project_subtype:
            if application.origin_request_id:
                raise _error(status.HTTP_400_BAD_REQUEST, "This project's category was set by your approved category change")
            await self._validate_subtype(db, subtype)
            await self._check_team_size(db, subtype, len(self._live_members(application)), check_min=False)
            changes["project_subtype"] = {"from": application.project_subtype, "to": subtype}
            application.project_subtype = subtype
        if title is not None and title.strip() != application.title:
            changes["title"] = {"from": application.title, "to": title.strip()}
            application.title = title.strip()
        if abstract is not None and abstract.strip() != application.abstract:
            changes["abstract"] = {"from": application.abstract, "to": abstract.strip()}
            application.abstract = abstract.strip()
        if faculty_id is not None and faculty_id != application.faculty_id:
            await self._require_available_guide(db, faculty_id, application.semester_id)
            changes["faculty_id"] = {"from": application.faculty_id, "to": faculty_id}
            application.faculty_id = faculty_id

        if changes:
            audit_service.log(db, student_actor(student), "application.update", "application", application_id, changes)
            await db.commit()
        return await self.load(db, application_id)

    async def submit(self, db: AsyncSession, student: Student, application_id: int) -> ProjectApplication:
        """(Re)submit to the guide: after a rejection, a full guide, or when invites were declined."""
        application = await self._lock_application(db, application_id)
        self._require_leader(application, student)
        if application.project_type != ProjectType.INHOUSE or application.status not in EDITABLE:
            raise _error(status.HTTP_400_BAD_REQUEST, "This application can't be submitted right now")
        if any(m.status == MemberStatus.INVITED for m in application.members):
            raise _error(status.HTTP_400_BAD_REQUEST, "Some teammates haven't responded to their invite yet")
        await self._check_team_size(db, application.project_subtype, sum(1 for m in application.members if m.status == MemberStatus.ACCEPTED))
        await self._require_open_semester(db, application.semester_id, application)
        faculty = await self._require_available_guide(db, application.faculty_id, application.semester_id)

        previous = application.status
        application.status = S.PENDING_GUIDE
        application.submitted_at = _now()
        application.guide_remarks = None
        audit_service.log(db, student_actor(student), "application.submit", "application", application_id, {"from_status": previous})
        await db.commit()
        notify("application.submitted", [faculty.email], {"application_id": application_id})
        return await self.load(db, application_id)

    async def withdraw(self, db: AsyncSession, student: Student, application_id: int) -> ProjectApplication:
        application = await self._lock_application(db, application_id)
        self._require_leader(application, student)
        if application.status not in S.PRE_APPROVAL:
            raise _error(status.HTTP_400_BAD_REQUEST, "Approved applications can't be withdrawn. Ask your guide to delete the project instead.")

        notify_emails = self._member_emails(application, (MemberStatus.INVITED, MemberStatus.ACCEPTED))
        previous = application.status
        self._release_members(application)
        application.status = S.WITHDRAWN
        audit_service.log(db, student_actor(student), "application.withdraw", "application", application_id, {"from_status": previous})
        await db.commit()

        if previous == S.PENDING_GUIDE and application.faculty:
            notify_emails.append(application.faculty.email)
        notify("application.withdrawn", notify_emails, {"application_id": application_id})
        return await self.load(db, application_id)

    async def resubmit_cdc(self, db: AsyncSession, student: Student, application_id: int, document_id: int) -> ProjectApplication:
        application = await self._lock_application(db, application_id)
        self._require_leader(application, student)
        if application.project_type != ProjectType.CDC or application.status != S.CDC_REJECTED:
            raise _error(status.HTTP_400_BAD_REQUEST, "Only a rejected CDC application can be resubmitted")
        await self._require_open_semester(db, application.semester_id)
        await self.require_own_document(db, document_id, student)

        application.proof_document_id = document_id
        application.status = S.PENDING_HOD
        application.submitted_at = _now()
        audit_service.log(db, student_actor(student), "application.resubmit", "application", application_id, {"document_id": document_id})
        await db.commit()
        notify("cdc.submitted", ["hod"], {"application_id": application_id, "student": student.register_number})
        return await self.load(db, application_id)

    @staticmethod
    def _release_members(application: ProjectApplication) -> None:
        for member in application.members:
            if member.status in (MemberStatus.INVITED, MemberStatus.ACCEPTED):
                member.status = MemberStatus.RELEASED
                member.responded_at = _now()

    # ------------------------------------------------------------------ guide review

    async def guide_approve(self, db: AsyncSession, faculty: Faculty, application_id: int, remarks: str | None) -> ProjectApplication:
        # Lock the guide row first: serialises concurrent approvals so the cap can't be exceeded
        await get_faculty_for_update(db, faculty.faculty_id)
        application = await self._lock_application(db, application_id)
        if application.faculty_id != faculty.faculty_id:
            raise _error(status.HTTP_403_FORBIDDEN, "This application isn't addressed to you")
        if application.status != S.PENDING_GUIDE:
            raise _error(status.HTTP_400_BAD_REQUEST, "This application is not awaiting your review")

        cap, approved = await self.guide_capacity(db, faculty, application.semester_id)
        if approved >= cap:
            raise _error(status.HTTP_409_CONFLICT, f"You have reached your limit of {cap} projects")

        application.status = S.GUIDE_APPROVED
        application.guide_remarks = remarks
        application.decided_at = _now()
        audit_service.log(db, guide_actor(faculty), "application.guide_approve", "application", application_id, {"remarks": remarks})

        # Guide is now full: close every other pending application to them so teams can move on
        closed: list[ProjectApplication] = []
        if approved + 1 >= cap:
            closed = await self.load_many(
                db,
                ProjectApplication.faculty_id == faculty.faculty_id,
                ProjectApplication.semester_id == application.semester_id,
                ProjectApplication.status == S.PENDING_GUIDE,
                ProjectApplication.application_id != application_id,
            )
            for other in closed:
                other.status = S.GUIDE_FULL
                other.decided_at = _now()
                audit_service.log(db, SYSTEM, "application.guide_full", "application", other.application_id, {"faculty_id": faculty.faculty_id})
        await db.commit()

        notify("application.guide_approved", self._member_emails(application) + ["hod"], {"application_id": application_id})
        for other in closed:
            notify("application.guide_full", self._member_emails(other), {"application_id": other.application_id})
        return await self.load(db, application_id)

    async def guide_reject(self, db: AsyncSession, faculty: Faculty, application_id: int, remarks: str) -> ProjectApplication:
        application = await self._lock_application(db, application_id)
        if application.faculty_id != faculty.faculty_id:
            raise _error(status.HTTP_403_FORBIDDEN, "This application isn't addressed to you")
        if application.status != S.PENDING_GUIDE:
            raise _error(status.HTTP_400_BAD_REQUEST, "This application is not awaiting your review")

        application.status = S.GUIDE_REJECTED
        application.guide_remarks = remarks
        application.decided_at = _now()
        audit_service.log(db, guide_actor(faculty), "application.guide_reject", "application", application_id, {"remarks": remarks})
        await db.commit()
        notify("application.guide_rejected", self._member_emails(application), {"application_id": application_id, "remarks": remarks})
        return await self.load(db, application_id)

    # ------------------------------------------------------------------ HOD: CDC registrations

    async def hod_decide_cdc(self, db: AsyncSession, admin_id: int, application_id: int, approve: bool, remarks: str | None) -> ProjectApplication:
        application = await self._lock_application(db, application_id)
        if application.project_type != ProjectType.CDC or application.status != S.PENDING_HOD:
            raise _error(status.HTTP_400_BAD_REQUEST, "This CDC application is not awaiting a decision")
        if not approve and not remarks:
            raise _error(422, "Please give a reason for rejecting")

        application.status = S.CDC_APPROVED if approve else S.CDC_REJECTED
        application.hod_remarks = remarks
        application.decided_at = _now()
        audit_service.log(db, admin_actor(admin_id), "application.cdc_approve" if approve else "application.cdc_reject", "application", application_id, {"remarks": remarks})
        await db.commit()
        notify("cdc.approved" if approve else "cdc.rejected", self._member_emails(application), {"application_id": application_id, "remarks": remarks})
        return await self.load(db, application_id)


project_service = ProjectService()
