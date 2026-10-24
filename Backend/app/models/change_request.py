from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, String, Text, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class ChangeRequestType:
    TEAM_CHANGE = "TEAM_CHANGE"        # payload: {"add": [reg], "remove": [reg], "consents": {reg: bool}}
    GUIDE_CHANGE = "GUIDE_CHANGE"      # payload: {"from_faculty_id": int, "to_faculty_id": int}
    TITLE_ABSTRACT = "TITLE_ABSTRACT"  # payload: {"title": str, "abstract": str}
    TEAM_DELETION = "TEAM_DELETION"    # payload: {}
    CDC_CONVERSION = "CDC_CONVERSION"  # payload: {"register_number": reg}; document_id = proof
    ALL = (TEAM_CHANGE, GUIDE_CHANGE, TITLE_ABSTRACT, TEAM_DELETION, CDC_CONVERSION)


class ChangeRequestStatus:
    PENDING_CONSENT = "PENDING_CONSENT"  # TEAM_CHANGE waiting on students being added
    PENDING_HOD = "PENDING_HOD"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    CANCELLED = "CANCELLED"
    OPEN = (PENDING_CONSENT, PENDING_HOD)


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
    hod_remarks: Mapped[str | None] = mapped_column(Text, nullable=True)
    decided_by: Mapped[int | None] = mapped_column(ForeignKey("admin.admin_id"), nullable=True)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    application: Mapped["ProjectApplication"] = relationship()
