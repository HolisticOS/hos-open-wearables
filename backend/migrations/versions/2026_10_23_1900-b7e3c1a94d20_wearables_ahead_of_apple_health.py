"""wearables_ahead_of_apple_health

Moves provider_priority rows that still hold their old default to the new
default, so a connected wearable (Garmin, Polar, ...) is preferred over Apple
Health. Rows someone set to another value are left alone. Values are copied
here rather than imported so the migration doesn't change if the defaults do.

Revision ID: b7e3c1a94d20
Revises: 9f0940493a9b

"""

from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "b7e3c1a94d20"
down_revision: Union[str, None] = "9f0940493a9b"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

# provider -> (old default, new default)
_MOVES = {
    "apple": (1, 20),
    "garmin": (2, 1),
    "polar": (3, 2),
    "suunto": (4, 3),
    "whoop": (5, 4),
}

_UPDATE = sa.text(
    "UPDATE provider_priority SET priority = :to_priority, updated_at = now() "
    "WHERE provider = :provider AND priority = :from_priority"
)


def upgrade() -> None:
    for provider, (old, new) in _MOVES.items():
        op.execute(_UPDATE.bindparams(provider=provider, from_priority=old, to_priority=new))


def downgrade() -> None:
    for provider, (old, new) in _MOVES.items():
        op.execute(_UPDATE.bindparams(provider=provider, from_priority=new, to_priority=old))
