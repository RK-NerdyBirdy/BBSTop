"""Change requests on guide-approved projects.

Every request goes to the project's guide first. The guide's decision is final, except:
- CDC_CONVERSION also needs the HOD; if the guide declines, the student may escalate to the HOD.
- GUIDE_CHANGE: once the current guide agrees, the team picks a new guide, who must accept.

Types: TEAM_CHANGE, GUIDE_CHANGE, TITLE_ABSTRACT, TEAM_DELETION (leader only), and
CDC_CONVERSION, CATEGORY_CHANGE (any member, for themselves). A CATEGORY_CHANGE releases
the student from the team; they then register a new project in the new category with a guide.
Approval applies the change in the same transaction as the audit row.
"""

from fastapi import status
from sqlalchemy import Integer, and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.crud.faculty import get_faculty, get_faculty_for_update
from app.models.change_request import ChangeRequest, ChangeRequestStatus as CR, ChangeRequestType as T
from app.models.faculty import Faculty
from app.models.project import (
    ApplicationStatus as S,
    MemberRole,
    MemberStatus,
    ProjectApplication,
    ProjectMember,
    ProjectSubtype,
    ProjectType,
)
from app.models.student import Student
from app.schemas.project import ChangeRequestCreate, ChangeRequestResponse, GuideChangeDetails
from app.services.audit_service import Actor, audit_service
from app.services.notify import notify
from app.services.project_service import _error, _now, admin_actor, guide_actor, project_service, student_actor
from app.services.settings_service import settings_service

LEADER_ONLY = (T.TEAM_CHANGE, T.GUIDE_CHANGE, T.TITLE_ABSTRACT, T.TEAM_DELETION)
# A member asks to leave the team for themselves; only one of these may be open per student
MEMBER_MOVES = (T.CDC_CONVERSION, T.CATEGORY_CHANGE)


class ChangeRequestService:
    # ------------------------------------------------------------------ serialising

    async def to_responses(self, db: AsyncSession, requests: list[ChangeRequest]) -> list[ChangeRequestResponse]:
        if not requests:
            return []
        regs = {r.requested_by for r in requests}
        names = dict((await db.execute(
            select(Student.register_number, Student.student_name).where(Student.register_number.in_(regs))
        )).all())
        app_ids = {r.application_id for r in requests}
        apps = {row.application_id: row for row in (await db.execute(
            select(ProjectApplication.application_id, ProjectApplication.title, ProjectApplication.faculty_id)
            .where(ProjectApplication.application_id.in_(app_ids))
        )).all()}
        faculty_ids = {a.faculty_id for a in apps.values() if a.faculty_id} | {
            r.payload.get("to_faculty_id") for r in requests if r.payload.get("to_faculty_id")
        }
        guide_names = dict((await db.execute(
            select(Faculty.faculty_id, Faculty.faculty_name).where(Faculty.faculty_id.in_(faculty_ids))
        )).all()) if faculty_ids else {}
        docs = await project_service.document_summaries(db, {r.document_id for r in requests if r.document_id})
        return [
            ChangeRequestResponse(
                request_id=r.request_id,
                application_id=r.application_id,
                application_title=apps[r.application_id].title if r.application_id in apps else None,
                requested_by=r.requested_by,
                requested_by_name=names.get(r.requested_by, r.requested_by),
                request_type=r.request_type,
                payload=r.payload,
                reason=r.reason,
                document=docs.get(r.document_id),
                status=r.status,
                guide_name=guide_names.get(apps[r.application_id].faculty_id) if r.application_id in apps else None,
                new_guide_name=guide_names.get(r.payload.get("to_faculty_id")),
                guide_remarks=r.guide_remarks,
                guide_decided_at=r.guide_decided_at,
                hod_remarks=r.hod_remarks,
                decided_at=r.decided_at,
                created_at=r.created_at,
            )
            for r in requests
        ]

    @staticmethod
    async def list_requests(db: AsyncSession, *conditions) -> list[ChangeRequest]:
        result = await db.execute(
            select(ChangeRequest).where(*conditions).order_by(ChangeRequest.created_at.desc())
        )
        return list(result.scalars().all())

    async def list_for_guide(self, db: AsyncSession, faculty: Faculty, waiting: bool) -> list[ChangeRequest]:
        """Requests waiting on this guide (as the project's guide, or as the proposed new guide), or all that involve them."""
        to_me = ChangeRequest.payload["to_faculty_id"].astext.cast(Integer) == faculty.faculty_id
        mine = ProjectApplication.faculty_id == faculty.faculty_id
        if waiting:
            condition = or_(
                and_(ChangeRequest.status == CR.PENDING_GUIDE, mine),
                and_(ChangeRequest.status == CR.PENDING_NEW_GUIDE, to_me),
            )
        else:
            condition = or_(
                mine,
                ChangeRequest.guide_decided_by == faculty.faculty_id,
                and_(ChangeRequest.request_type == T.GUIDE_CHANGE, to_me),
            )
        result = await db.execute(
            select(ChangeRequest)
            .join(ProjectApplication, ProjectApplication.application_id == ChangeRequest.application_id)
            .where(condition, ChangeRequest.status != CR.PENDING_CONSENT)
            .order_by(ChangeRequest.created_at.desc())
        )
        return list(result.scalars().all())

    @staticmethod
    async def guide_can_see_document(db: AsyncSession, faculty: Faculty, document_id: int) -> bool:
        """A guide may open proof attached to a request on one of their projects."""
        result = await db.execute(
            select(ChangeRequest.request_id)
            .join(ProjectApplication, ProjectApplication.application_id == ChangeRequest.application_id)
            .where(
                ChangeRequest.document_id == document_id,
                or_(ProjectApplication.faculty_id == faculty.faculty_id, ChangeRequest.guide_decided_by == faculty.faculty_id),
            )
        )
        return result.first() is not None

    @staticmethod
    async def _lock(db: AsyncSession, request_id: int) -> ChangeRequest:
        result = await db.execute(
            select(ChangeRequest).where(ChangeRequest.request_id == request_id).with_for_update()
        )
        request = result.scalar_one_or_none()
        if request is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Change request not found")
        return request

    @staticmethod
    def _accepted(application: ProjectApplication) -> list[ProjectMember]:
        return [m for m in application.members if m.status == MemberStatus.ACCEPTED]

    @staticmethod
    def _guide_email(application: ProjectApplication) -> str | None:
        return application.faculty.email if application.faculty else None

    # ------------------------------------------------------------------ student: create / consent / cancel

    async def create(self, db: AsyncSession, student: Student, application_id: int, data: ChangeRequestCreate) -> ChangeRequest:
        if data.request_type not in T.ALL:
            raise _error(status.HTTP_400_BAD_REQUEST, "Unknown request type")
        application = await project_service._lock_application(db, application_id)
        accepted = self._accepted(application)
        if not any(m.register_number == student.register_number for m in accepted):
            raise _error(status.HTTP_403_FORBIDDEN, "You are not a member of this project")
        if application.project_type != ProjectType.INHOUSE or application.status != S.GUIDE_APPROVED:
            raise _error(status.HTTP_400_BAD_REQUEST, "Change requests are only for guide-approved projects. Before approval, edit or withdraw the application directly.")
        if data.request_type in LEADER_ONLY:
            project_service._require_leader(application, student)

        if data.request_type in MEMBER_MOVES:
            open_same = await self.list_requests(
                db,
                ChangeRequest.application_id == application_id,
                ChangeRequest.request_type.in_(MEMBER_MOVES),
                ChangeRequest.requested_by == student.register_number,
                ChangeRequest.status.in_(CR.OPEN),
            )
        else:
            open_same = await self.list_requests(
                db,
                ChangeRequest.application_id == application_id,
                ChangeRequest.request_type == data.request_type,
                ChangeRequest.status.in_(CR.OPEN),
            )
        if open_same:
            raise _error(status.HTTP_409_CONFLICT, "There is already an open request of this kind")

        payload, request_status, document_id = await self._build_payload(db, student, application, data)
        request = ChangeRequest(
            application_id=application_id,
            requested_by=student.register_number,
            request_type=data.request_type,
            payload=payload,
            reason=data.reason.strip(),
            document_id=document_id,
            status=request_status,
        )
        db.add(request)
        await db.flush()
        audit_service.log(db, student_actor(student), "change_request.create", "change_request", request.request_id, {
            "application_id": application_id, "type": data.request_type, "payload": payload,
        })
        await db.commit()

        if request_status == CR.PENDING_CONSENT:
            emails = [s.email for s in (await self._students(db, payload["add"])).values()]
            notify("change_request.consent_needed", emails, {"request_id": request.request_id})
        else:
            notify("change_request.created", [self._guide_email(application)], {"request_id": request.request_id})
        return request

    async def _build_payload(self, db: AsyncSession, student: Student, application: ProjectApplication, data: ChangeRequestCreate):
        accepted_regs = {m.register_number for m in self._accepted(application)}
        if data.request_type == T.TEAM_CHANGE:
            add = sorted({r.strip().upper() for r in data.add if r.strip()})
            remove = sorted({r.strip().upper() for r in data.remove if r.strip()})
            if not add and not remove:
                raise _error(status.HTTP_400_BAD_REQUEST, "Add or remove at least one member")
            if set(add) & set(remove):
                raise _error(status.HTTP_400_BAD_REQUEST, "A student can't be both added and removed")
            for reg in remove:
                if reg not in accepted_regs:
                    raise _error(status.HTTP_400_BAD_REQUEST, f"{reg} is not on this team")
                if reg == application.leader_register_number:
                    raise _error(status.HTTP_400_BAD_REQUEST, "The leader can't be removed")
            for reg in add:
                if reg in accepted_regs:
                    raise _error(status.HTTP_400_BAD_REQUEST, f"{reg} is already on this team")
                await project_service._validate_invitee(db, reg, application.semester_id, application.leader_register_number)
            await project_service._check_team_size(db, application.project_subtype, len(accepted_regs) - len(remove) + len(add))
            payload = {"add": add, "remove": remove, "consents": {reg: None for reg in add}}
            return payload, (CR.PENDING_CONSENT if add else CR.PENDING_GUIDE), None

        if data.request_type == T.GUIDE_CHANGE:
            # The new guide is picked after the current guide agrees to let the team go
            payload = {"from_faculty_id": application.faculty_id, "to_faculty_id": None, "title": None, "abstract": None, "declines": []}
            return payload, CR.PENDING_GUIDE, None

        if data.request_type == T.TITLE_ABSTRACT:
            if data.title is None and data.abstract is None:
                raise _error(status.HTTP_400_BAD_REQUEST, "Provide a new title and/or abstract")
            return self._details_payload(application, data.title, data.abstract), CR.PENDING_GUIDE, None

        if data.request_type == T.TEAM_DELETION:
            return {}, CR.PENDING_GUIDE, None

        if data.request_type == T.CATEGORY_CHANGE:
            to_subtype = data.to_subtype
            if to_subtype not in ProjectSubtype.ALL or to_subtype == application.project_subtype:
                raise _error(status.HTTP_400_BAD_REQUEST, "Pick a different in-house category")
            await project_service._validate_subtype(db, to_subtype)
            if to_subtype in await settings_service.get(db, "category_change_proof") and data.document_id is None:
                raise _error(status.HTTP_400_BAD_REQUEST, "Attach proof for this move (for example, your internship offer)")
            if data.document_id is not None:
                await project_service.require_own_document(db, data.document_id, student)
            payload = {"register_number": student.register_number, "from_subtype": application.project_subtype, "to_subtype": to_subtype}
            return payload, CR.PENDING_GUIDE, data.document_id

        # CDC_CONVERSION
        if not await settings_service.get(db, "cdc_enabled"):
            raise _error(status.HTTP_400_BAD_REQUEST, "CDC registration is currently disabled")
        if data.document_id is None:
            raise _error(status.HTTP_400_BAD_REQUEST, "Attach proof of your internship")
        await project_service.require_own_document(db, data.document_id, student)
        return {"register_number": student.register_number, "escalated": False}, CR.PENDING_GUIDE, data.document_id

    @staticmethod
    def _details_payload(application: ProjectApplication, title: str | None, abstract: str | None) -> dict:
        return {
            "title": {"from": application.title, "to": title.strip()} if title else None,
            "abstract": {"from": application.abstract, "to": abstract.strip()} if abstract else None,
        }

    @staticmethod
    async def _students(db: AsyncSession, regs: list[str]) -> dict[str, Student]:
        if not regs:
            return {}
        result = await db.execute(select(Student).where(Student.register_number.in_(regs)))
        return {s.register_number: s for s in result.scalars().all()}

    async def consent(self, db: AsyncSession, student: Student, request_id: int, accept: bool) -> ChangeRequest:
        request = await self._lock(db, request_id)
        consents = dict(request.payload.get("consents") or {})
        if request.request_type != T.TEAM_CHANGE or student.register_number not in consents:
            raise _error(status.HTTP_404_NOT_FOUND, "Change request not found")
        if request.status != CR.PENDING_CONSENT or consents[student.register_number] is not None:
            raise _error(status.HTTP_400_BAD_REQUEST, "This request is no longer waiting for your answer")

        consents[student.register_number] = accept
        # JSONB columns need a new object to register the change
        request.payload = {**request.payload, "consents": consents}
        if not accept:
            request.status = CR.CANCELLED
            request.decided_at = _now()
        elif all(v is True for v in consents.values()):
            request.status = CR.PENDING_GUIDE
        audit_service.log(db, student_actor(student), "change_request.consent" if accept else "change_request.decline", "change_request", request_id, {})
        await db.commit()

        requester = await db.get(Student, request.requested_by)
        if not accept:
            notify("change_request.declined", [requester.email], {"request_id": request_id, "student": student.register_number})
        elif request.status == CR.PENDING_GUIDE:
            application = await project_service.load(db, request.application_id)
            notify("change_request.created", [self._guide_email(application)], {"request_id": request_id})
        return request

    async def cancel(self, db: AsyncSession, student: Student, request_id: int) -> ChangeRequest:
        request = await self._lock(db, request_id)
        if request.requested_by != student.register_number:
            raise _error(status.HTTP_403_FORBIDDEN, "Only the student who raised this request can cancel it")
        if request.status not in CR.OPEN:
            raise _error(status.HTTP_400_BAD_REQUEST, "This request is already closed")
        request.status = CR.CANCELLED
        request.decided_at = _now()
        audit_service.log(db, student_actor(student), "change_request.cancel", "change_request", request_id, {})
        await db.commit()
        return request

    async def escalate(self, db: AsyncSession, student: Student, request_id: int) -> ChangeRequest:
        """A CDC move the guide declined goes to the HOD at the student's request."""
        request = await self._lock(db, request_id)
        if request.requested_by != student.register_number:
            raise _error(status.HTTP_403_FORBIDDEN, "Only the student who raised this request can escalate it")
        if request.request_type != T.CDC_CONVERSION or request.status != CR.GUIDE_DECLINED:
            raise _error(status.HTTP_400_BAD_REQUEST, "Only a CDC move your guide declined can be sent to the HOD")
        request.status = CR.PENDING_HOD
        request.payload = {**request.payload, "escalated": True}
        audit_service.log(db, student_actor(student), "change_request.escalate", "change_request", request_id, {})
        await db.commit()
        notify("change_request.escalated", ["hod"], {"request_id": request_id})
        return request

    async def submit_guide_details(self, db: AsyncSession, student: Student, request_id: int, data: GuideChangeDetails) -> ChangeRequest:
        """GUIDE_CHANGE after the current guide agreed: the leader names the new guide (and may update the details)."""
        peek = await db.get(ChangeRequest, request_id)
        if peek is None or peek.request_type != T.GUIDE_CHANGE:
            raise _error(status.HTTP_404_NOT_FOUND, "Change request not found")
        application = await project_service._lock_application(db, peek.application_id)
        project_service._require_leader(application, student)
        request = await self._lock(db, request_id)
        if request.status != CR.AWAITING_DETAILS:
            raise _error(status.HTTP_400_BAD_REQUEST, "This request isn't waiting for a new guide")
        if data.to_faculty_id == application.faculty_id:
            raise _error(status.HTTP_400_BAD_REQUEST, "Pick a different guide")
        new_guide = await project_service._require_available_guide(db, data.to_faculty_id, application.semester_id)

        request.payload = {
            **request.payload,
            "to_faculty_id": new_guide.faculty_id,
            **self._details_payload(application, data.title, data.abstract),
        }
        request.status = CR.PENDING_NEW_GUIDE
        audit_service.log(db, student_actor(student), "change_request.new_guide", "change_request", request_id, {
            "to_faculty_id": new_guide.faculty_id, "title": data.title, "abstract_changed": data.abstract is not None,
        })
        await db.commit()
        notify("change_request.new_guide_asked", [new_guide.email], {"request_id": request_id})
        return request

    # ------------------------------------------------------------------ guide decision

    async def guide_decide(self, db: AsyncSession, faculty: Faculty, request_id: int, approve: bool, remarks: str | None) -> ChangeRequest:
        if not approve and not remarks:
            raise _error(422, "Please give a reason for declining")
        actor = guide_actor(faculty)

        peek = await db.get(ChangeRequest, request_id)
        if peek is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Change request not found")
        as_new_guide = peek.status == CR.PENDING_NEW_GUIDE
        # Lock order: faculty -> application -> request. Taking over a project uses a slot, so serialise on the guide.
        if approve and as_new_guide:
            await get_faculty_for_update(db, faculty.faculty_id)
        application = await project_service._lock_application(db, peek.application_id)
        request = await self._lock(db, request_id)

        if request.status == CR.PENDING_GUIDE:
            if application.faculty_id != faculty.faculty_id:
                raise _error(status.HTTP_403_FORBIDDEN, "This request isn't addressed to you")
        elif request.status == CR.PENDING_NEW_GUIDE:
            if request.payload.get("to_faculty_id") != faculty.faculty_id:
                raise _error(status.HTTP_403_FORBIDDEN, "This request isn't addressed to you")
        else:
            raise _error(status.HTTP_400_BAD_REQUEST, "This request is not awaiting your decision")
        if approve and application.status != S.GUIDE_APPROVED:
            raise _error(status.HTTP_400_BAD_REQUEST, "The project is no longer active")

        notices: list[tuple[str, list[str], dict]] = []
        requester = await db.get(Student, request.requested_by)
        leader_email = next((m.student.email for m in self._accepted(application) if m.role == MemberRole.LEADER), None)

        if as_new_guide:
            if approve:
                notices = await self._apply(db, actor, application, request)
                request.status = CR.APPROVED
                request.decided_at = _now()
            else:
                # Back to the team to pick someone else; the current guide stays until a new one accepts
                declines = list(request.payload.get("declines") or [])
                declines.append({"faculty_id": faculty.faculty_id, "remarks": remarks, "at": _now().isoformat()})
                request.payload = {**request.payload, "to_faculty_id": None, "declines": declines}
                request.status = CR.AWAITING_DETAILS
                notices = [("change_request.new_guide_declined", [leader_email], {"request_id": request_id, "remarks": remarks})]
            action = "change_request.new_guide_accept" if approve else "change_request.new_guide_decline"
        else:
            request.guide_remarks = remarks
            request.guide_decided_by = faculty.faculty_id
            request.guide_decided_at = _now()
            if not approve:
                # A declined CDC move can still go to the HOD; everything else ends here
                request.status = CR.GUIDE_DECLINED if request.request_type == T.CDC_CONVERSION else CR.REJECTED
                if request.status == CR.REJECTED:
                    request.decided_at = _now()
                notices = [("change_request.guide_declined", [requester.email], {"request_id": request_id, "remarks": remarks})]
            elif request.request_type == T.CDC_CONVERSION:
                request.status = CR.PENDING_HOD
                notices = [("change_request.created", ["hod"], {"request_id": request_id})]
            elif request.request_type == T.GUIDE_CHANGE:
                request.status = CR.AWAITING_DETAILS
                notices = [("change_request.pick_new_guide", [requester.email], {"request_id": request_id})]
            else:
                notices = await self._apply(db, actor, application, request)
                request.status = CR.APPROVED
                request.decided_at = _now()
            action = "change_request.guide_approve" if approve else "change_request.guide_decline"

        audit_service.log(db, actor, action, "change_request", request_id, {
            "application_id": application.application_id, "type": request.request_type, "remarks": remarks, "status": request.status,
        })
        await project_service._flush(db, "A student in this request has since joined another team")
        await db.commit()

        if request.status == CR.APPROVED:
            notify("change_request.approved", [requester.email], {"request_id": request_id, "remarks": remarks})
        for event, recipients, context in notices:
            notify(event, recipients, context)
        return request

    # ------------------------------------------------------------------ HOD decision (CDC moves)

    async def hod_decide(self, db: AsyncSession, admin_id: int, request_id: int, approve: bool, remarks: str | None) -> ChangeRequest:
        if not approve and not remarks:
            raise _error(422, "Please give a reason for rejecting")
        actor = admin_actor(admin_id)

        peek = await db.get(ChangeRequest, request_id)
        if peek is None:
            raise _error(status.HTTP_404_NOT_FOUND, "Change request not found")
        application = await project_service._lock_application(db, peek.application_id)
        request = await self._lock(db, request_id)
        if request.status != CR.PENDING_HOD:
            raise _error(status.HTTP_400_BAD_REQUEST, "This request is not awaiting a decision")
        if approve and application.status != S.GUIDE_APPROVED:
            raise _error(status.HTTP_400_BAD_REQUEST, "The project is no longer active")

        notices: list[tuple[str, list[str], dict]] = []
        if approve:
            notices = await self._apply(db, actor, application, request)

        request.status = CR.APPROVED if approve else CR.REJECTED
        request.hod_remarks = remarks
        request.decided_by = admin_id
        request.decided_at = _now()
        audit_service.log(db, actor, "change_request.approve" if approve else "change_request.reject", "change_request", request_id, {
            "application_id": application.application_id, "type": request.request_type, "remarks": remarks,
        })
        await project_service._flush(db, "A student in this request has since joined another team")
        await db.commit()

        requester = await db.get(Student, request.requested_by)
        notify("change_request.approved" if approve else "change_request.rejected", [requester.email], {"request_id": request_id, "remarks": remarks})
        for event, recipients, context in notices:
            notify(event, recipients, context)
        return request

    # ------------------------------------------------------------------ applying an approved change

    async def _apply(self, db: AsyncSession, actor: Actor, application: ProjectApplication, request: ChangeRequest) -> list:
        """Apply an approved change. Returns notifications to send after commit."""
        app_id = application.application_id
        guide_email = self._guide_email(application)
        member_emails = [m.student.email for m in self._accepted(application)]
        p = request.payload

        if request.request_type == T.TEAM_CHANGE:
            await project_service._lock_students(db, set(p["add"]) | set(p["remove"]))
            for reg in p["add"]:
                if await project_service.has_team(db, reg, application.semester_id):
                    raise _error(status.HTTP_409_CONFLICT, f"{reg} has since joined another team")
            await project_service._check_team_size(db, application.project_subtype, len(self._accepted(application)) - len(p["remove"]) + len(p["add"]))
            for member in self._accepted(application):
                if member.register_number in p["remove"]:
                    member.status = MemberStatus.REMOVED
                    member.responded_at = _now()
            await db.flush()
            for reg in p["add"]:
                db.add(ProjectMember(
                    application_id=app_id,
                    register_number=reg,
                    semester_id=application.semester_id,
                    role=MemberRole.MEMBER,
                    status=MemberStatus.ACCEPTED,
                    responded_at=_now(),
                ))
                await project_service._decline_other_invites(db, reg, application.semester_id)
            audit_service.log(db, actor, "application.team_change", "application", app_id, {"add": p["add"], "remove": p["remove"]})
            added = [s.email for s in (await self._students(db, p["add"] + p["remove"])).values()]
            return [("project.team_changed", member_emails + added + [guide_email], {"application_id": app_id})]

        if request.request_type == T.GUIDE_CHANGE:
            new_guide = await get_faculty(db, p["to_faculty_id"])
            if new_guide is None or not new_guide.active:
                raise _error(status.HTTP_400_BAD_REQUEST, "The new guide is no longer available")
            cap, approved = await project_service.guide_capacity(db, new_guide, application.semester_id)
            if approved >= cap:
                raise _error(status.HTTP_409_CONFLICT, f"{new_guide.faculty_name} has no project slots left")
            self._apply_details(application, p)
            application.faculty_id = new_guide.faculty_id
            audit_service.log(db, actor, "application.guide_change", "application", app_id, {
                "from": p["from_faculty_id"], "to": new_guide.faculty_id, "title": p.get("title"), "abstract": p.get("abstract"),
            })
            return [("project.guide_changed", member_emails + [guide_email, new_guide.email], {"application_id": app_id})]

        if request.request_type == T.TITLE_ABSTRACT:
            self._apply_details(application, p)
            audit_service.log(db, actor, "application.details_change", "application", app_id, {"title": p.get("title"), "abstract": p.get("abstract")})
            return [("project.details_changed", member_emails + [guide_email], {"application_id": app_id})]

        if request.request_type == T.TEAM_DELETION:
            project_service._release_members(application)
            application.status = S.DISSOLVED
            await self._cancel_other_open(db, app_id, request.request_id)
            audit_service.log(db, actor, "application.dissolve", "application", app_id, {"reason": "team_deletion"})
            return [("project.dissolved", member_emails + [guide_email], {"application_id": app_id})]

        if request.request_type == T.CATEGORY_CHANGE:
            reg = p["register_number"]
            dissolved = await self._release_member(db, application, request)
            audit_service.log(db, actor, "application.category_change", "application", app_id, {
                "register_number": reg,
                "from_subtype": p["from_subtype"],
                "to_subtype": p["to_subtype"],
                "project_dissolved": dissolved,
                "new_leader": application.leader_register_number if not dissolved else None,
            })
            return [(
                "project.member_changed_category",
                member_emails + [guide_email],
                {"application_id": app_id, "student": reg, "to_subtype": p["to_subtype"], "project_dissolved": dissolved},
            )]

        # CDC_CONVERSION
        return await self._convert_to_cdc(db, actor, application, request, guide_email, member_emails)

    @staticmethod
    def _apply_details(application: ProjectApplication, payload: dict) -> None:
        if payload.get("title"):
            application.title = payload["title"]["to"]
        if payload.get("abstract"):
            application.abstract = payload["abstract"]["to"]

    async def _release_member(self, db: AsyncSession, application: ProjectApplication, request: ChangeRequest) -> bool:
        """Take the requesting student off the team. Returns True if that dissolved the project."""
        app_id = application.application_id
        reg = request.payload["register_number"]
        await project_service._lock_students(db, {m.register_number for m in self._accepted(application)})
        member = next((m for m in self._accepted(application) if m.register_number == reg), None)
        if member is None:
            raise _error(status.HTTP_400_BAD_REQUEST, "The student is no longer on this team")

        member.status = MemberStatus.RELEASED
        member.responded_at = _now()
        remaining = [m for m in self._accepted(application) if m.member_id != member.member_id]
        dissolved = not remaining

        if dissolved:
            # Last member left: project dissolves and the guide's slot frees up
            application.status = S.DISSOLVED
            await self._cancel_other_open(db, app_id, request.request_id)
        elif member.role == MemberRole.LEADER:
            # Hand leadership to the longest-standing remaining member
            new_leader = min(remaining, key=lambda m: m.member_id)
            new_leader.role = MemberRole.LEADER
            application.leader_register_number = new_leader.register_number
            await self._cancel_other_open(db, app_id, request.request_id, leader_types_only=True)
        await db.flush()  # free the student's one-team slot
        return dissolved

    async def _convert_to_cdc(self, db, actor, application, request, guide_email, member_emails) -> list:
        app_id = application.application_id
        reg = request.payload["register_number"]
        dissolved = await self._release_member(db, application, request)

        cdc = ProjectApplication(
            semester_id=application.semester_id,
            leader_register_number=reg,
            project_type=ProjectType.CDC,
            proof_document_id=request.document_id,
            status=S.CDC_APPROVED,
            hod_remarks=f"Converted from project #{app_id}",
            submitted_at=request.created_at,
            decided_at=_now(),
        )
        db.add(cdc)
        await db.flush()
        db.add(ProjectMember(
            application_id=cdc.application_id,
            register_number=reg,
            semester_id=application.semester_id,
            role=MemberRole.LEADER,
            status=MemberStatus.ACCEPTED,
            responded_at=_now(),
        ))
        await project_service._decline_other_invites(db, reg, application.semester_id)
        audit_service.log(db, actor, "application.cdc_conversion", "application", app_id, {
            "register_number": reg,
            "cdc_application_id": cdc.application_id,
            "project_dissolved": dissolved,
            "new_leader": application.leader_register_number if not dissolved else None,
        })
        return [(
            "project.member_converted_to_cdc",
            member_emails + [guide_email],
            {"application_id": app_id, "student": reg, "project_dissolved": dissolved},
        )]

    async def _cancel_other_open(self, db: AsyncSession, application_id: int, except_id: int, leader_types_only: bool = False) -> None:
        """Close open requests that no longer make sense (project gone, or leader changed)."""
        others = await self.list_requests(
            db,
            ChangeRequest.application_id == application_id,
            ChangeRequest.request_id != except_id,
            ChangeRequest.status.in_(CR.OPEN),
        )
        for other in others:
            if leader_types_only and other.request_type not in LEADER_ONLY:
                continue
            other.status = CR.CANCELLED
            other.hod_remarks = "Closed automatically: the project changed"
            other.decided_at = _now()


change_request_service = ChangeRequestService()
