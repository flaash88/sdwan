"""Backups: Ersteller; Altbestand = "unbekannt"

Revision ID: 0018
Revises: 0017
"""

import sqlalchemy as sa
from alembic import op

revision = "0018"
down_revision = "0017"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("config_backups", sa.Column("created_by", sa.String(255), nullable=True))
    op.execute("UPDATE config_backups SET created_by = 'unbekannt' WHERE created_by IS NULL")


def downgrade() -> None:
    op.drop_column("config_backups", "created_by")
