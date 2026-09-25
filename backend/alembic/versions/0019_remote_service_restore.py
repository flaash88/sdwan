"""Fernzugriff: ursprünglichen Dienstzustand je Sitzung speichern

Revision ID: 0019
Revises: 0018
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("remote_sessions", sa.Column(
        "service_restore", sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql"), nullable=True))


def downgrade() -> None:
    op.drop_column("remote_sessions", "service_restore")
