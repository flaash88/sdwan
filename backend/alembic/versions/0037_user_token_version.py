"""user token_version (AUDIT-029: Anmelde-Tokens nach Passwortänderung ungültig)

Revision ID: 0037
Revises: 0036
Create Date: 2026-09-27 17:00:00
"""
from alembic import op
import sqlalchemy as sa

revision = '0037'
down_revision = '0036'
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column('users', sa.Column('token_version', sa.Integer(), nullable=False, server_default='0'))


def downgrade() -> None:
    op.drop_column('users', 'token_version')
