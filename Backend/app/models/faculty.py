from __future__ import annotations

from datetime import datetime

import sqlalchemy as sa
from sqlalchemy import Boolean, DateTime, Integer, String, func
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Faculty(Base):
    """A project guide (VIT professor). Signs in with Google on the faculty domain."""

    __tablename__ = "faculty"

    faculty_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    employee_id: Mapped[str] = mapped_column(String(20), nullable=False, unique=True)
    faculty_name: Mapped[str] = mapped_column(String(100), nullable=False)
    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    # Per-guide override of the default_guide_cap rule; NULL means "use the default"
    max_projects: Mapped[int | None] = mapped_column(Integer, nullable=True)
    pfp_url: Mapped[str | None] = mapped_column(String(512))
    active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        server_default=sa.text("true"),
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
