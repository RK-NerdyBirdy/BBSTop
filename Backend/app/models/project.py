from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ProjectType:
    INHOUSE = "INHOUSE"
    CDC = "CDC"


class ProjectSubtype:
    CAPSTONE = "CAPSTONE"
    NON_CDC = "NON_CDC"
    SAP = "SAP"
    ALL = (CAPSTONE, NON_CDC, SAP)


class ApplicationStatus:
    # In-house
    FORMING = "FORMING"                # waiting for invited teammates to accept
    PENDING_GUIDE = "PENDING_GUIDE"    # all accepted, waiting on the guide
    GUIDE_APPROVED = "GUIDE_APPROVED"  # locked; visible to HOD; counts toward guide cap
    GUIDE_REJECTED = "GUIDE_REJECTED"  # leader can edit and resubmit
    GUIDE_FULL = "GUIDE_FULL"          # guide hit their cap; leader picks another guide
    DISSOLVED = "DISSOLVED"            # last member left (CDC) or HOD approved team deletion
    # CDC
    PENDING_HOD = "PENDING_HOD"
    CDC_APPROVED = "CDC_APPROVED"
    CDC_REJECTED = "CDC_REJECTED"      # student can resubmit with new proof
    # Both
    WITHDRAWN = "WITHDRAWN"

    # Leader may still withdraw / edit in these states
    PRE_APPROVAL = (FORMING, PENDING_GUIDE, GUIDE_REJECTED, GUIDE_FULL, PENDING_HOD, CDC_REJECTED)
    # Members of an application in these states are "taken" for the semester
    CLOSED = (WITHDRAWN, DISSOLVED)


class MemberRole:
    LEADER = "LEADER"
    MEMBER = "MEMBER"


class MemberStatus:
    INVITED = "INVITED"
    ACCEPTED = "ACCEPTED"
    DECLINED = "DECLINED"
    REMOVED = "REMOVED"    # removed by leader before approval, or via HOD-approved change
    RELEASED = "RELEASED"  # application withdrawn / dissolved / member converted to CDC


class ProjectApplication(Base):
    __tablename__ = "project_application"
    __table_args__ = (
        sa.CheckConstraint("project_type IN ('INHOUSE','CDC')", name="ck_application_type"),
        sa.Index("ix_application_faculty_status", "faculty_id", "semester_id", "status"),
    )

    application_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    semester_id: Mapped[int] = mapped_column(
        ForeignKey("semester.semester_id", ondelete="CASCADE"),
        nullable=False,
    )
    leader_register_number: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("student.register_number", ondelete="CASCADE"),
        nullable=False,
    )
    project_type: Mapped[str] = mapped_column(String(20), nullable=False)
    project_subtype: Mapped[str | None] = mapped_column(String(20), nullable=True)
    title: Mapped[str | None] = mapped_column(String(255), nullable=True)
    abstract: Mapped[str | None] = mapped_column(Text, nullable=True)
    faculty_id: Mapped[int | None] = mapped_column(
        ForeignKey("faculty.faculty_id"),
        nullable=True,
    )
    proof_document_id: Mapped[int | None] = mapped_column(
        ForeignKey("document.document_id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    guide_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    hod_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    members: Mapped[list["ProjectMember"]] = relationship(
        back_populates="application",
        cascade="all, delete-orphan",
        order_by="ProjectMember.member_id",
    )
    faculty: Mapped["Faculty | None"] = relationship()
    leader: Mapped["Student"] = relationship(foreign_keys=[leader_register_number])
    semester: Mapped["Semester"] = relationship()


class ProjectMember(Base):
    __tablename__ = "project_member"
    __table_args__ = (
        # A student can be an ACCEPTED member of only one live application per semester.
        # Withdrawn / dissolved applications release their members, freeing this slot.
        sa.Index(
            "uq_project_member_one_team",
            "register_number",
            "semester_id",
            unique=True,
            postgresql_where=sa.text("status = 'ACCEPTED'"),
        ),
        # The same student can't be invited twice to the same application while the invite is live
        sa.Index(
            "uq_project_member_application_student",
            "application_id",
            "register_number",
            unique=True,
            postgresql_where=sa.text("status IN ('INVITED','ACCEPTED')"),
        ),
    )

    member_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("project_application.application_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    register_number: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("student.register_number", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    semester_id: Mapped[int] = mapped_column(
        ForeignKey("semester.semester_id", ondelete="CASCADE"),
        nullable=False,
    )
    role: Mapped[str] = mapped_column(String(10), nullable=False)
    status: Mapped[str] = mapped_column(String(10), nullable=False)
    responded_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    application: Mapped["ProjectApplication"] = relationship(back_populates="members")
    student: Mapped["Student"] = relationship()
