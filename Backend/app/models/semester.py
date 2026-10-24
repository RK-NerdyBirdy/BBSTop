from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


class Semester(Base):
    __tablename__ = "semester"

    semester_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    semester_name: Mapped[str] = mapped_column(String(100), nullable=False)
    swap_allowed: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=sa.text("false"),
    )
    # Project registration window for this semester (HOD toggles it, like swap_allowed)
    registration_open: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=sa.text("false"),
    )
    # Admission year of the cohort this semester belongs to (e.g. 2024 for "24 Fall Sem")
    cohort_year: Mapped[int | None] = mapped_column(Integer, nullable=True, index=True)
    # Archived semesters are hidden from students and read-only for everyone
    archived: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=sa.text("false"),
    )
    # Set once swaps or project registration open: after that only a minimal-move rebalance is allowed
    assignment_locked: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=sa.text("false"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )

    batches: Mapped[list["Batch"]] = relationship(
        back_populates="semester",
        cascade="all, delete-orphan",
    )
    assignments: Mapped[list["StudentSemesterAssignment"]] = relationship(
        back_populates="semester",
        cascade="all, delete-orphan",
    )
