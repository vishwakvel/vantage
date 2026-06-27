"""SQLAlchemy declarative base shared by all ORM models.

All ORM classes in ``app/db/models.py`` must inherit from ``Base`` so that
``Base.metadata`` is populated for Alembic autogenerate::

    # alembic/env.py
    from app.db.base import Base
    target_metadata = Base.metadata
"""

from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """Project-wide declarative base.  Inherit from this class in every ORM model."""
