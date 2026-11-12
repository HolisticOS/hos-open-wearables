"""merge the HOS fork's head into upstream 0.11

The fork's only migration (b7e3c1a94d20, wearables ahead of Apple Health)
branched off 9f0940493a9b before upstream added its later migrations. A
database at b7e3c1a94d20 applies upstream's chain from 9f0940493a9b, then
lands here. No schema change of its own.

Revision ID: c4d8e2f1a7b3
Revises: b7e3c1a94d20, 3f6b9c2d8e41

"""

from typing import Sequence, Union

# revision identifiers, used by Alembic.
revision: str = "c4d8e2f1a7b3"
down_revision: Union[str, tuple[str, ...], None] = ("b7e3c1a94d20", "3f6b9c2d8e41")
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
