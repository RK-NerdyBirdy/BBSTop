"""Guide-first change requests: guide decision columns, category moves, per-category team size

Revision ID: ae4f6a8b3c5d
Revises: 9d3e5f7a2b4c
Create Date: 2026-10-28 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "ae4f6a8b3c5d"
down_revision: Union[str, None] = "9d3e5f7a2b4c"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("change_request", sa.Column("guide_remarks", sa.Text(), nullable=True))
    op.add_column("change_request", sa.Column("guide_decided_by", sa.Integer(), nullable=True))
    op.add_column("change_request", sa.Column("guide_decided_at", sa.DateTime(timezone=True), nullable=True))
    op.create_foreign_key(
        "change_request_guide_decided_by_fkey", "change_request", "faculty", ["guide_decided_by"], ["faculty_id"]
    )
    op.add_column("project_application", sa.Column("origin_request_id", sa.Integer(), nullable=True))

    # The single max_team_size rule becomes a min/max per in-house category, keeping the HOD's max
    op.execute(
        """
        INSERT INTO app_setting (key, value)
        SELECT 'team_size', jsonb_build_object(
            'CAPSTONE', jsonb_build_object('min', 1, 'max', value),
            'NON_CDC', jsonb_build_object('min', 1, 'max', value),
            'SAP', jsonb_build_object('min', 1, 'max', value)
        )
        FROM app_setting WHERE key = 'max_team_size'
        ON CONFLICT (key) DO NOTHING
        """
    )
    op.execute("DELETE FROM app_setting WHERE key = 'max_team_size'")


def downgrade() -> None:
    op.execute(
        """
        INSERT INTO app_setting (key, value)
        SELECT 'max_team_size', value -> 'CAPSTONE' -> 'max' FROM app_setting WHERE key = 'team_size'
        ON CONFLICT (key) DO NOTHING
        """
    )
    op.execute("DELETE FROM app_setting WHERE key IN ('team_size', 'category_change_proof')")
    op.drop_column("project_application", "origin_request_id")
    op.drop_constraint("change_request_guide_decided_by_fkey", "change_request", type_="foreignkey")
    op.drop_column("change_request", "guide_decided_at")
    op.drop_column("change_request", "guide_decided_by")
    op.drop_column("change_request", "guide_remarks")
