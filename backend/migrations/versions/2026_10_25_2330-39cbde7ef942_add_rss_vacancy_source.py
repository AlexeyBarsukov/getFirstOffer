"""add rss vacancy source

Revision ID: 39cbde7ef942
Revises: 96a4dbb46910
Create Date: 2026-10-25 23:30:00.717397

"""

from collections.abc import Sequence

from alembic import op

# revision identifiers, used by Alembic.
revision: str = "39cbde7ef942"
down_revision: str | Sequence[str] | None = "96a4dbb46910"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

CONSTRAINT = "ck_vacancies_source_name"


def upgrade() -> None:
    """Новое значение first_seen_source: rss (вакансия найдена через RSS-ленту hh.ru)."""
    op.drop_constraint(op.f(CONSTRAINT), "vacancies", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT), "vacancies", "first_seen_source IN ('api', 'rss', 'parser')"
    )


def downgrade() -> None:
    op.execute("UPDATE vacancies SET first_seen_source = 'parser' WHERE first_seen_source = 'rss'")
    op.drop_constraint(op.f(CONSTRAINT), "vacancies", type_="check")
    op.create_check_constraint(
        op.f(CONSTRAINT), "vacancies", "first_seen_source IN ('api', 'parser')"
    )
