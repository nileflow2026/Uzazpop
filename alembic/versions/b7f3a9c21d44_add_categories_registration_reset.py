"""add categories, non-medical items, self-registration, password reset

Revision ID: b7f3a9c21d44
Revises: 4a2187d3a01d
Create Date: 2026-07-20 00:00:00.000000

Adds new columns to existing tables (medicines, users, mpesa_transactions)
and a new password_reset_codes table. Every new NOT NULL column has a
server_default so this is safe to run against a database that already
has rows — existing rows get a sensible default, nothing is dropped or
rewritten.
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = 'b7f3a9c21d44'
down_revision: Union[str, None] = '4a2187d3a01d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # ── medicines: categorization + non-medical products ──────────────
    op.add_column('medicines', sa.Column(
        'item_type', sa.String(length=20), nullable=False, server_default='medicine'
    ))
    op.add_column('medicines', sa.Column('category', sa.String(length=80), nullable=True))
    op.create_index('ix_medicines_item_type', 'medicines', ['item_type'])
    op.create_index('ix_medicines_category', 'medicines', ['category'])

    # ── users: pharmacy name captured at self-registration ─────────────
    op.add_column('users', sa.Column('pharmacy_name', sa.String(length=200), nullable=True))

    # ── mpesa_transactions: paid self-registration signup flow ─────────
    op.add_column('mpesa_transactions', sa.Column(
        'purpose', sa.String(length=20), nullable=False, server_default='sale'
    ))
    op.add_column('mpesa_transactions', sa.Column('pending_full_name', sa.String(length=120), nullable=True))
    op.add_column('mpesa_transactions', sa.Column('pending_pharmacy_name', sa.String(length=200), nullable=True))
    op.add_column('mpesa_transactions', sa.Column('pending_email', sa.String(length=255), nullable=True))
    op.add_column('mpesa_transactions', sa.Column('pending_password_hash', sa.String(length=255), nullable=True))
    op.add_column('mpesa_transactions', sa.Column('pending_phone', sa.String(length=20), nullable=True))
    op.add_column('mpesa_transactions', sa.Column(
        'registration_completed', sa.Boolean(), nullable=False, server_default=sa.false()
    ))

    # ── password_reset_codes: new table for forgot-password flow ───────
    op.create_table(
        'password_reset_codes',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('user_id', sa.Integer(), sa.ForeignKey('users.id', ondelete='CASCADE'), nullable=False),
        sa.Column('code_hash', sa.String(length=255), nullable=False),
        sa.Column('expires_at', sa.DateTime(), nullable=False),
        sa.Column('used', sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column('attempts', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('created_at', sa.DateTime(), server_default=sa.func.now(), nullable=False),
    )
    op.create_index('ix_password_reset_codes_user_id', 'password_reset_codes', ['user_id'])


def downgrade() -> None:
    op.drop_index('ix_password_reset_codes_user_id', table_name='password_reset_codes')
    op.drop_table('password_reset_codes')

    op.drop_column('mpesa_transactions', 'registration_completed')
    op.drop_column('mpesa_transactions', 'pending_phone')
    op.drop_column('mpesa_transactions', 'pending_password_hash')
    op.drop_column('mpesa_transactions', 'pending_email')
    op.drop_column('mpesa_transactions', 'pending_pharmacy_name')
    op.drop_column('mpesa_transactions', 'pending_full_name')
    op.drop_column('mpesa_transactions', 'purpose')

    op.drop_column('users', 'pharmacy_name')

    op.drop_index('ix_medicines_category', table_name='medicines')
    op.drop_index('ix_medicines_item_type', table_name='medicines')
    op.drop_column('medicines', 'category')
    op.drop_column('medicines', 'item_type')
