"""Configuração da conexão SQLite + sessão SQLAlchemy."""
from __future__ import annotations

import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

ROOT = Path(__file__).resolve().parent.parent
DB_PATH = ROOT / "data.db"

DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{DB_PATH}")

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {},
    pool_pre_ping=True,
)

SessionLocal = sessionmaker(bind=engine, autoflush=False, autocommit=False, expire_on_commit=False)
Base = declarative_base()


def init_db() -> None:
    """Cria todas as tabelas se ainda não existirem + migrações simples."""
    from . import models  # noqa: F401
    from sqlalchemy import text, inspect

    Base.metadata.create_all(bind=engine)

    # Migrações leves: adiciona colunas novas em tabelas existentes.
    # SQLAlchemy.create_all() NÃO altera tabelas que já existem.
    insp = inspect(engine)
    migrations = [
        # (tabela, coluna, tipo SQL pro ALTER)
        ("script_media", "video_note", "BOOLEAN DEFAULT 0"),
        ("leads", "in_leads_group", "BOOLEAN DEFAULT 0"),
        ("scripts", "mode", "VARCHAR(20) DEFAULT 'forward'"),
        # Liga
        ("leads", "liga_state",       "VARCHAR(30) DEFAULT 'new'"),
        ("leads", "liga_account_id",  "VARCHAR(100)"),
        ("leads", "liga_id_status",          "VARCHAR(20)"),
        ("leads", "liga_id_country",         "VARCHAR(50)"),
        ("leads", "liga_id_balance",         "REAL"),
        ("leads", "liga_id_deposits_sum",    "REAL"),
        ("leads", "liga_id_turnover",        "REAL"),
        ("leads", "liga_id_validated_at",    "DATETIME"),
        ("leads", "liga_id_partner_response", "TEXT"),
        ("leads", "liga_balance",     "REAL DEFAULT 0.0"),
        ("leads", "engagement_tag",            "VARCHAR(40)"),
        ("leads", "engagement_tag_updated_at", "DATETIME"),
        ("leads", "engagement_evidence",       "TEXT"),
        ("leads", "is_vip_potential",          "BOOLEAN DEFAULT 0"),
        ("leads", "opted_out",                 "BOOLEAN DEFAULT 0"),
        ("leads", "opted_out_at",              "DATETIME"),
        ("leads", "rewarm_candidate",          "BOOLEAN DEFAULT 0"),
        ("leads", "last_revalidated_at",       "DATETIME"),
        ("leads", "proof_sent_today", "BOOLEAN DEFAULT 0"),
        ("leads", "lead_score",       "INTEGER DEFAULT 0"),
        ("leads", "last_bot_action",  "VARCHAR(100)"),
        ("leads", "conversation_ctx", "TEXT"),
        ("leads", "streak_days",      "INTEGER DEFAULT 0"),
    ]
    with engine.begin() as conn:
        for table, column, ddl in migrations:
            try:
                if table not in insp.get_table_names():
                    continue
                cols = [c["name"] for c in insp.get_columns(table)]
                if column not in cols:
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
            except Exception:
                pass  # se já existe ou der erro, ignora

        # Data migrations — limpeza de status legados (positive/converted)
        try:
            if "leads" in insp.get_table_names():
                conn.execute(text(
                    "UPDATE leads SET status='replied' "
                    "WHERE status IN ('positive', 'converted')"
                ))
        except Exception:
            pass

        # Data migration: limpa liga_account_id com tamanho fora de 7-9 dígitos
        # (provavelmente lixo do Vision tipo "4834"). Manda pro needs_review
        # pra revisão manual.
        try:
            if "leads" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("leads")]
                if "liga_account_id" in cols and "liga_id_status" in cols:
                    conn.execute(text("""
                        UPDATE leads
                        SET liga_id_status='needs_review',
                            liga_id_partner_response=COALESCE(liga_id_partner_response, '') ||
                                ' [migration] candidato ' || liga_account_id ||
                                ' rejeitado (fora de 7-9 dígitos)',
                            liga_account_id=NULL
                        WHERE liga_account_id IS NOT NULL
                          AND liga_account_id != ''
                          AND (length(liga_account_id) < 7 OR length(liga_account_id) > 9
                               OR liga_account_id GLOB '*[^0-9]*')
                    """))
        except Exception:
            pass


@contextmanager
def get_session():
    session = SessionLocal()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()
