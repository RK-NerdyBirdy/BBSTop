from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ChangeRequestType:
    TEAM_CHANGE = "TEAM_CHANGE"          # payload: {"add": [reg], "remove": [reg], "consents": {reg: bool}}
    GUIDE_CHANGE = "GUIDE_CHANGE"        # payload: {"from_faculty_id", "to_faculty_id", "title", "abstract", "declines": [...]}
    TITLE_ABSTRACT = "TITLE_ABSTRACT"    # payload: {"title": {"from", "to"}, "abstract": {"from", "to"}}
    TEAM_DELETION = "TEAM_DELETION"      # payload: {}
    CDC_CONVERSION = "CDC_CONVERSION"    # payload: {"register_number": reg, "escalated": bool}; document_id = proof
    CATEGORY_CHANGE = "CATEGORY_CHANGE"  # payload: {"register_number", "from_subtype", "to_subtype"}; document_id = optional proof
    ALL = (TEAM_CHANGE, GUIDE_CHANGE, TITLE_ABSTRACT, TEAM_DELETION, CDC_CONVERSION, CATEGORY_CHANGE)


class ChangeRequestStatus:
    """Every change goes to the project's guide first. Only CDC_CONVERSION then needs the HOD.

    TEAM_CHANGE      [PENDING_CONSENT ->] PENDING_GUIDE -> APPROVED
    TITLE_ABSTRACT,
    TEAM_DELETION,
    CATEGORY_CHANGE  PENDING_GUIDE -> APPROVED (the mover then registers a new project with a guide)
    GUIDE_CHANGE     PENDING_GUIDE -> AWAITING_DETAILS (team picks a new guide) -> PENDING_NEW_GUIDE -> APPROVED
                     (a declining new guide sends it back to AWAITING_DETAILS)
    CDC_CONVERSION   PENDING_GUIDE -> PENDING_HOD -> APPROVED
                     (a declining guide leaves it GUIDE_DECLINED; the student may escalate to the HOD)
    """

    PENDING_CONSENT = "PENDING_CONSENT"      # TEAM_CHANGE waiting on students being added
    PENDING_GUIDE = "PENDING_GUIDE"          # waiting on the project's current guide
    GUIDE_DECLINED = "GUIDE_DECLINED"        # CDC only: guide declined, student may escalate to the HOD
    PENDING_HOD = "PENDING_HOD"              # CDC only
    AWAITING_DETAILS = "AWAITING_DETAILS"    # GUIDE_CHANGE: current guide agreed, team to pick a new guide
    PENDING_NEW_GUIDE = "PENDING_NEW_GUIDE"  # GUIDE_CHANGE: waiting on the new guide
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    OPEN = (PENDING_CONSENT, PENDING_GUIDE, GUIDE_DECLINED, PENDING_HOD, AWAITING_DETAILS, PENDING_NEW_GUIDE)


class ChangeRequest(Base):
    __tablename__ = "change_request"

    request_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    application_id: Mapped[int] = mapped_column(
        ForeignKey("project_application.application_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    requested_by: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("student.register_number", ondelete="CASCADE"),
        nullable=False,
    )
    request_type: Mapped[str] = mapped_column(String(30), nullable=False)
    payload: Mapped[dict] = mapped_column(JSONB, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False)
    document_id: Mapped[int | None] = mapped_column(
        ForeignKey("document.document_id", ondelete="SET NULL"),
        nullable=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    # The project's guide acknowledging (or declining) the request
    guide_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    guide_decided_by: Mapped[int | None] = mapped_column(ForeignKey("faculty.faculty_id"), nullable=True)
    guide_decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    hod_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("admin.admin_id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    application: Mapped["ProjectApplication"] = relationship()
