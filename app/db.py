from contextlib import contextmanager
from typing import Iterator

from sqlalchemy import inspect, text
from sqlmodel import Session, SQLModel, create_engine, select

from .config import get_settings
from .models import KV, Profile

engine = create_engine(get_settings().db_url, connect_args={"check_same_thread": False})


def _add_missing_columns() -> None:
    """Minimal migration: add columns that were added to models after the DB was created."""
    insp = inspect(engine)
    with engine.begin() as conn:
        for table in SQLModel.metadata.sorted_tables:
            if not insp.has_table(table.name):
                continue
            existing = {c["name"] for c in insp.get_columns(table.name)}
            for col in table.columns:
                if col.name in existing:
                    continue
                ddl_type = col.type.compile(dialect=engine.dialect)
                default = col.default.arg if col.default is not None and not callable(col.default.arg) else None
                if isinstance(default, bool):
                    default = int(default)
                default_sql = f" DEFAULT {default!r}" if default is not None else ""
                conn.execute(text(f'ALTER TABLE "{table.name}" ADD COLUMN "{col.name}" {ddl_type}{default_sql}'))


def init_db() -> None:
    SQLModel.metadata.create_all(engine)
    _add_missing_columns()
    with Session(engine) as s:
        if not s.get(Profile, 1):
            s.add(Profile(id=1))
            s.commit()


@contextmanager
def session_scope() -> Iterator[Session]:
    with Session(engine) as s:
        yield s


def get_session() -> Iterator[Session]:
    with Session(engine) as s:
        yield s


def get_profile(s: Session) -> Profile:
    return s.get(Profile, 1) or Profile(id=1)


def kv_get(s: Session, key: str, default: str = "") -> str:
    row = s.get(KV, key)
    return row.value if row else default


def kv_set(s: Session, key: str, value: str) -> None:
    row = s.get(KV, key) or KV(key=key)
    row.value = value
    s.add(row)
    s.commit()


__all__ = ["engine", "init_db", "session_scope", "get_session", "get_profile", "kv_get", "kv_set", "select"]
