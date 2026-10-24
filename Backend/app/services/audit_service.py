from dataclasses import dataclass

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.audit_log import AuditLog


@dataclass(frozen=True)
class Actor:
    """Who performed an action: student (register number), guide (faculty_id), admin (admin_id) or system."""

    actor_type: str
    actor_id: str


SYSTEM = Actor("system", "system")


class AuditService:
    @staticmethod
    def log(
        db: AsyncSession,
        actor: Actor,
        action: str,
        entity: str,
        entity_id: int | str,
        details: dict | None = None,
    ) -> None:
        """Add an audit row to the current transaction (committed together with the change it records)."""
        db.add(
            AuditLog(
                actor_type=actor.actor_type,
                actor_id=str(actor.actor_id),
                action=action,
                entity=entity,
                entity_id=str(entity_id),
                details=details,
            )
        )


audit_service = AuditService()
