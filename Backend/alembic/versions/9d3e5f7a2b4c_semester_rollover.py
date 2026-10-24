"""Semester roll-over: cohort year, archived and assignment-locked flags

Revision ID: 9d3e5f7a2b4c
Revises: 8c2d4e6f1a3b
Create Date: 2026-10-24 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "9d3e5f7a2b4c"
down_revision: Union[str, None] = "8c2d4e6f1a3b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("semester", sa.Column("cohort_year", sa.Integer(), nullable=True))
    op.add_column("semester", sa.Column("archived", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.add_column("semester", sa.Column("assignment_locked", sa.Boolean(), server_default=sa.text("false"), nullable=False))
    op.create_index(op.f("ix_semester_cohort_year"), "semester", ["cohort_year"], unique=False)

    # Existing semesters are named like "24 Fall Sem 2026-27": take the leading two digits as the admission year
    op.execute(
        "UPDATE semester SET cohort_year = 2000 + substring(semester_name from '^\\s*(\\d{2})\\D')::int "
        "WHERE semester_name ~ '^\\s*\\d{2}\\D'"
    )
    # Semesters that already exist have been in use, so a full reshuffle is no longer allowed
    op.execute("UPDATE semester SET assignment_locked = true")


def downgrade() -> None:
    op.drop_index(op.f("ix_semester_cohort_year"), table_name="semester")
    op.drop_column("semester", "assignment_locked")
    op.drop_column("semester", "archived")
    op.drop_column("semester", "cohort_year")
