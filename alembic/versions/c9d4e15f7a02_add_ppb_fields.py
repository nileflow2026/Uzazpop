"""add PPB register fields to medicines

Revision ID: c9d4e15f7a02
Revises: b7f3a9c21d44
Create Date: 2026-07-22 00:00:00.000000

Adds nullable columns for Kenya PPB public register data (replacing the
WHO/OpenFDA importers). All nullable, so safe on a table that already
has rows — nothing existing is touched.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'c9d4e15f7a02'
down_revision: Union[str, None] = 'b7f3a9c21d44'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('medicines', sa.Column('ppb_registration_no', sa.String(length=50), nullable=True))
    op.add_column('medicines', sa.Column('ppb_pack_size', sa.String(length=200), nullable=True))
    op.add_column('medicines', sa.Column('ppb_origin', sa.String(length=20), nullable=True))
    op.add_column('medicines', sa.Column('ppb_distributor', sa.String(length=200), nullable=True))
    op.add_column('medicines', sa.Column('ppb_status', sa.String(length=50), nullable=True))
    op.create_index('ix_medicines_ppb_registration_no', 'medicines', ['ppb_registration_no'])


def downgrade() -> None:
    op.drop_index('ix_medicines_ppb_registration_no', table_name='medicines')
    op.drop_column('medicines', 'ppb_status')
    op.drop_column('medicines', 'ppb_distributor')
    op.drop_column('medicines', 'ppb_origin')
    op.drop_column('medicines', 'ppb_pack_size')
    op.drop_column('medicines', 'ppb_registration_no')
