"""VRRP-Gegenstelle: Adresse, Beschreibung, letzter Ping

Revision ID: 0017
Revises: 0016
"""

import sqlalchemy as sa
from alembic import op

import app.db

revision = "0017"
down_revision = "0016"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("vrrp_instances", sa.Column("peer_address", sa.String(64), nullable=True))
    op.add_column("vrrp_instances", sa.Column("peer_description", sa.String(100), nullable=True))
    op.add_column("vrrp_instances", sa.Column("peer_reachable", sa.Boolean(), nullable=True))
    op.add_column("vrrp_instances", sa.Column("peer_rtt_ms", sa.Float(), nullable=True))
    op.add_column("vrrp_instances", sa.Column("peer_checked_at", app.db.UTCDateTime(timezone=True), nullable=True))


def downgrade() -> None:
    for c in ("peer_checked_at", "peer_rtt_ms", "peer_reachable", "peer_description", "peer_address"):
        op.drop_column("vrrp_instances", c)
