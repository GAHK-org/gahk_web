"""The initial schema: the site state, the image cache and the outbox.

Revision 0001, the first. It replaces the hand-written 0001_initial.sql and 0002_image_variants.sql
from before Alembic (no production database had them yet) and makes the same tables.

Runs before the new release goes live, while the previous release still serves the site: the
previous release's code must keep working with the schema this leaves behind.
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0001"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    site_state = op.create_table(
        "site_state",
        sa.Column("id", sa.SmallInteger(), server_default=sa.text("1"), autoincrement=False, nullable=False),
        sa.Column("snapshot", postgresql.JSONB(none_as_null=True), nullable=True),
        sa.Column("revision", sa.Text(), nullable=True),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rendered_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("rendered_on", sa.Date(), nullable=True),
        sa.Column("rendered_version", sa.Text(), nullable=True),
        sa.CheckConstraint("id = 1", name=op.f("site_state_id_check")),
        sa.PrimaryKeyConstraint("id", name=op.f("site_state_pkey")),
    )
    op.bulk_insert(site_state, [{"id": 1}])

    op.create_table(
        "images",
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.Column("content_type", sa.Text(), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("stored_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("width", sa.Integer(), nullable=True),
        sa.Column("height", sa.Integer(), nullable=True),
        sa.Column("variant_widths", postgresql.ARRAY(sa.Integer()), nullable=True),
        sa.Column("variant_note", sa.Text(), nullable=True),
        sa.Column("variant_attempts", sa.SmallInteger(), server_default=sa.text("0"), nullable=False),
        sa.CheckConstraint("sha256 ~ '^[0-9a-f]{64}$'", name=op.f("images_sha256_check")),
        sa.CheckConstraint("width > 0", name=op.f("images_width_check")),
        sa.CheckConstraint("height > 0", name=op.f("images_height_check")),
        sa.PrimaryKeyConstraint("sha256", name=op.f("images_pkey")),
    )
    op.create_table(
        "image_files",
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("sha256", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("rel_path", name=op.f("image_files_pkey")),
    )
    op.create_table(
        "generated_files",
        sa.Column("rel_path", sa.Text(), nullable=False),
        sa.Column("url", sa.Text(), nullable=True),
        sa.Column("rendered_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("rel_path", name=op.f("generated_files_pkey")),
    )
    op.create_table(
        "applications",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("kind", sa.Text(), nullable=False),
        sa.Column("fields", postgresql.JSONB(), nullable=False),
        sa.Column("submitted_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("kind IN ('rundvisning', 'fremleje')", name=op.f("applications_kind_check")),
        sa.PrimaryKeyConstraint("id", name=op.f("applications_pkey")),
    )
    op.create_table(
        "form_rate_limit",
        sa.Column("client", sa.Text(), nullable=False),
        sa.Column("at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index(op.f("form_rate_limit_client_at"), "form_rate_limit", ["client", "at"])
    op.create_table(
        "visits",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("views", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.Column("visitors", sa.Integer(), server_default=sa.text("0"), nullable=False),
        sa.PrimaryKeyConstraint("day", "path", name=op.f("visits_pkey")),
    )
    op.create_table(
        "visit_seen",
        sa.Column("day", sa.Date(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("visitor", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("day", "path", "visitor", name=op.f("visit_seen_pkey")),
    )


def downgrade() -> None:
    for table in (
        "visit_seen",
        "visits",
        "form_rate_limit",
        "applications",
        "generated_files",
        "image_files",
        "images",
        "site_state",
    ):
        op.drop_table(table)
