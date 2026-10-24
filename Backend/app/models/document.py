from __future__ import annotations

from datetime import datetime

from sqlalchemy import DateTime, ForeignKey, Integer, LargeBinary, String, func
from sqlalchemy.orm import Mapped, deferred, mapped_column

from app.models.base import Base


class Document(Base):
    """An uploaded proof file (e.g. CDC internship offer letter).

    Stored in Postgres because Render's disk is ephemeral. `content` is deferred so
    listing documents never loads the file bytes.
    """

    __tablename__ = "document"

    document_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    owner_register_number: Mapped[str] = mapped_column(
        String(20),
        ForeignKey("student.register_number", ondelete="CASCADE"),
        nullable=False,
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    content_type: Mapped[str] = mapped_column(String(100), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    content: Mapped[bytes] = deferred(mapped_column(LargeBinary, nullable=False))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        nullable=False,
    )
