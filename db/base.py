from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    """All database models inherit from this base and share its metadata."""
