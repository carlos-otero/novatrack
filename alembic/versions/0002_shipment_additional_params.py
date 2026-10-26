"""shipment: add destination_country, destination_postal_code, phone_number_last_4

Revision ID: 0002
Revises: 0001
"""

from typing import Sequence, Union

import sqlalchemy as sa

from alembic import op

revision: str = "0002"
down_revision: Union[str, Sequence[str], None] = "0001"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column("shipments", sa.Column("destination_country", sa.String(length=2), nullable=True))
    op.add_column("shipments", sa.Column("destination_postal_code", sa.String(length=20), nullable=True))
    op.add_column("shipments", sa.Column("phone_number_last_4", sa.String(length=10), nullable=True))


def downgrade() -> None:
    op.drop_column("shipments", "phone_number_last_4")
    op.drop_column("shipments", "destination_postal_code")
    op.drop_column("shipments", "destination_country")
