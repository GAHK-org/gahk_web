"""${message}

Revision ${up_revision}, after ${down_revision | comma,n}; written ${create_date}.

Runs before the new release goes live, while the previous release still serves the site: the
previous release's code must keep working with the schema this leaves behind.
"""

from alembic import op
import sqlalchemy as sa
${imports if imports else ""}

revision = ${repr(up_revision)}
down_revision = ${repr(down_revision)}
branch_labels = ${repr(branch_labels)}
depends_on = ${repr(depends_on)}


def upgrade() -> None:
    ${upgrades if upgrades else "pass"}


def downgrade() -> None:
    ${downgrades if downgrades else "pass"}
