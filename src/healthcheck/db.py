"""Modelos e engine SQLAlchemy.

Usa SQLite por padrão para os testes iniciais. Para produção, aponte
database.url (config.yaml) ou a env var HEALTHCHECK_DB_URL para o Oracle,
por exemplo:

    oracle+oracledb://usuario:senha@host:1521/?service_name=ORCLPDB1

Como o schema usa apenas tipos padrão do SQLAlchemy (String, DateTime,
Boolean, Integer, Float), a troca de banco não deve exigir mudanças no
restante do código — apenas `pip install oracledb`.
"""
from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, Index, Integer, String, create_engine, event
from sqlalchemy.orm import DeclarativeBase, Mapped, Session, mapped_column, sessionmaker

from .config import PROJECT_ROOT

ERROR_MAX_LEN = 500
# Valor da coluna `error` quando o Cloudflare respondeu com o desafio
# anti-bot em vez de repassar a requisição ao sistema. Essas linhas são
# gravadas com success=False, mas NÃO são queda: a apuração as trata como
# rodada sem medição (ver sla.py). Fica como marcador na coluna existente
# para não exigir migração de schema (SQLite hoje, Oracle depois).
CLOUDFLARE_CHALLENGE = "bloqueio_cloudflare"


class Base(DeclarativeBase):
    pass


class Check(Base):
    __tablename__ = "checks"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    target_key: Mapped[str] = mapped_column(String(64), nullable=False)
    # Guardado como UTC "naive" (sem tzinfo): SQLite não preserva timezone
    # de forma confiável, e nem todo setup Oracle usa TIMESTAMP WITH TIME
    # ZONE. Todo o código trata este valor como UTC implicitamente.
    checked_at: Mapped[datetime] = mapped_column(DateTime(), nullable=False)
    success: Mapped[bool] = mapped_column(Boolean, nullable=False)
    status_code: Mapped[int | None] = mapped_column(Integer, nullable=True)
    response_time_ms: Mapped[float | None] = mapped_column(Float, nullable=True)
    error: Mapped[str | None] = mapped_column(String(ERROR_MAX_LEN), nullable=True)

    __table_args__ = (
        Index("ix_checks_target_key_checked_at", "target_key", "checked_at"),
    )


def _normalize_sqlite_url(db_url: str) -> str:
    """Resolve caminhos relativos de SQLite (sqlite:///data/...) contra a
    raiz do projeto, e garante que o diretório exista, independente do
    diretório de trabalho de onde o script foi chamado (systemd, cron, etc)."""
    prefix = "sqlite:///"
    if not db_url.startswith(prefix):
        return db_url
    rest = db_url[len(prefix):]
    if rest.startswith("/"):
        return db_url  # já é um caminho absoluto (sqlite:////...)
    if not rest or rest.startswith(":memory:") or "mode=memory" in rest:
        return db_url  # banco em memória não tem caminho em disco

    abs_path = (PROJECT_ROOT / rest).resolve()
    abs_path.parent.mkdir(parents=True, exist_ok=True)
    return f"{prefix}{abs_path}"


def make_engine(db_url: str):
    db_url = _normalize_sqlite_url(db_url)
    is_sqlite = db_url.startswith("sqlite:")
    connect_args = {"check_same_thread": False, "timeout": 30} if is_sqlite else {}
    engine = create_engine(db_url, connect_args=connect_args, future=True)

    if is_sqlite:
        # Sem WAL, a leitura longa do relatório bloqueia o commit do daemon e
        # a rodada inteira é perdida ("database is locked").
        @event.listens_for(engine, "connect")
        def _set_sqlite_pragmas(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.execute("PRAGMA busy_timeout=30000")
            cursor.close()

    return engine


def init_db(engine) -> None:
    Base.metadata.create_all(engine)


def make_session_factory(engine) -> sessionmaker[Session]:
    return sessionmaker(bind=engine, expire_on_commit=False, future=True)


def utcnow() -> datetime:
    """UTC "naive" (sem tzinfo) — ver comentário no modelo Check."""
    return datetime.now(timezone.utc).replace(tzinfo=None)
