"""Configuração da conexão SQLite + sessão SQLAlchemy."""
from __future__ import annotations

import logging
import os
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import declarative_base, sessionmaker

logger = logging.getLogger(__name__)

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
        # Remarketing stage
        ("leads", "remarketing_stage",         "VARCHAR(30) DEFAULT 'untouched'"),
        ("leads", "r1_sent_at",                "DATETIME"),
        ("leads", "r2_sent_at",                "DATETIME"),
        ("leads", "r3_sent_at",                "DATETIME"),
        ("leads", "is_fresh",                  "BOOLEAN DEFAULT 0"),
        ("leads", "discard_at",                "DATETIME"),
        # Script targeting
        ("scripts", "target_remarketing_stage", "VARCHAR(30)"),
        ("scripts", "target_engagement_tag",    "VARCHAR(40)"),
        ("leads", "proof_sent_today", "BOOLEAN DEFAULT 0"),
        ("leads", "lead_score",       "INTEGER DEFAULT 0"),
        ("leads", "last_bot_action",  "VARCHAR(100)"),
        ("leads", "conversation_ctx", "TEXT"),
        ("leads", "streak_days",      "INTEGER DEFAULT 0"),
        # Revisão humana de comprovantes (anti-perda durante torneio)
        ("operation_proofs", "needs_review",  "BOOLEAN DEFAULT 0"),
        ("operation_proofs", "review_reason", "VARCHAR(40)"),
        ("operation_proofs", "validated_by",  "VARCHAR(20)"),
        ("operation_proofs", "validated_at",  "DATETIME"),
        ("operation_proofs", "review_notes",  "TEXT"),
        # Campaigns: excluir VIPs por default em disparos em massa
        ("campaigns", "exclude_vips", "BOOLEAN DEFAULT 1"),
        # Campaigns: override do stage/engagement do script
        ("campaigns", "target_remarketing_stage", "VARCHAR(30)"),
        ("campaigns", "target_engagement_tag", "VARCHAR(40)"),
        # FunnelStep: posição da mídia (bolinha) na sequência
        ("funnel_steps", "media_position", "VARCHAR(20) DEFAULT 'before'"),
        # Bloco 3: lifecycle único como fonte da verdade do estado do lead
        ("leads", "lifecycle", "VARCHAR(20) DEFAULT 'new'"),
        ("leads", "blocked", "BOOLEAN DEFAULT 0"),
        # Funnel + AgentSuggestion: novas tabelas (criadas via create_all),
        # mas também adicionamos colunas em caso de migração futura
    ]
    # Migrações em transação: cada ALTER TABLE roda no SEU PRÓPRIO escopo
    # transacional. Se uma falhar, faz rollback ESSA migração específica
    # mas as outras continuam. Antes era 'engine.begin()' grande que se
    # uma falhasse no meio podia deixar banco inconsistente.
    for table, column, ddl in migrations:
        try:
            if table not in insp.get_table_names():
                continue
            cols = [c["name"] for c in insp.get_columns(table)]
            if column not in cols:
                with engine.begin() as conn:  # transação per-migração
                    conn.execute(text(f'ALTER TABLE {table} ADD COLUMN {column} {ddl}'))
                    logger.info("[migration] ✓ %s.%s adicionada", table, column)
        except Exception:
            logger.exception("[migration] erro adicionando %s.%s — pulando", table, column)
            # Rollback automático pelo with block. Outras migrações continuam.

    with engine.begin() as conn:

        # Data migrations — limpeza de status legados (positive/converted)
        try:
            if "leads" in insp.get_table_names():
                conn.execute(text(
                    "UPDATE leads SET status='replied' "
                    "WHERE status IN ('positive', 'converted')"
                ))
        except Exception:
            pass

        # ═══ Migration: normaliza NULL flags pra 0 ═══════════════════════════
        # Sem isso, queries com `.is_(False)` ignoram leads legados com NULL.
        try:
            for col in ("opted_out", "in_private_group", "is_fresh",
                        "is_vip_potential", "rewarm_candidate", "blocked",
                        "in_leads_group"):
                cols_check = [c["name"] for c in insp.get_columns("leads")]
                if col in cols_check:
                    conn.execute(text(f"UPDATE leads SET {col}=0 WHERE {col} IS NULL"))
        except Exception:
            logger.exception("[migration] erro normalizando NULL flags")

        # ═══ Bloco 3: popula lifecycle UMA VEZ (idempotente via Setting) ═════
        # Roda só na primeira inicialização. Após isso, lifecycle é fonte da
        # verdade e atualizado em runtime via liga/lifecycle.py mark_*().
        try:
            cols_now = [c["name"] for c in insp.get_columns("leads")]
            if "lifecycle" in cols_now:
                # Checa flag de migração já feita
                already_done = conn.execute(text(
                    "SELECT value FROM settings WHERE key='lifecycle_migration_done'"
                )).fetchone()

                if not already_done:
                    # vip: já no grupo privado
                    conn.execute(text(
                        "UPDATE leads SET lifecycle='vip' "
                        "WHERE in_private_group=1 AND (lifecycle IS NULL OR lifecycle='new')"
                    ))
                    # deposited: tem balance OU deposits >= 20
                    conn.execute(text(
                        "UPDATE leads SET lifecycle='deposited' "
                        "WHERE lifecycle='new' "
                        "AND (liga_id_balance >= 20 OR liga_id_deposits_sum >= 20)"
                    ))
                    # lead: criou conta (validated) OU teve interação real
                    conn.execute(text(
                        "UPDATE leads SET lifecycle='lead' "
                        "WHERE lifecycle='new' "
                        "AND (liga_id_status='validated' "
                        "     OR status IN ('replied', 'contacted', 'positive'))"
                    ))
                    # Marca como feita
                    conn.execute(text(
                        "INSERT INTO settings (key, value) VALUES ('lifecycle_migration_done', '1')"
                    ))
                    logger.info("[migration] lifecycle populado UMA vez (flag setada)")
                else:
                    logger.debug("[migration] lifecycle já populado (flag presente) — skip")
        except Exception:
            logger.exception("[migration] erro populando lifecycle")

        # Data migration: popula remarketing_stage baseado em sends/replies/grupo
        # As UPDATEs são idempotentes — só agem em leads ainda em 'untouched'
        try:
            if "leads" in insp.get_table_names() and "sends" in insp.get_table_names():
                cols = [c["name"] for c in insp.get_columns("leads")]
                if "remarketing_stage" in cols:
                    # Sempre roda — UPDATEs são idempotentes
                    if True:
                        # 1) opted_out → opted_out
                        conn.execute(text(
                            "UPDATE leads SET remarketing_stage='opted_out' "
                            "WHERE opted_out=1 AND (remarketing_stage IS NULL OR remarketing_stage='untouched')"
                        ))
                        # 2) in_private_group → converted
                        conn.execute(text(
                            "UPDATE leads SET remarketing_stage='converted' "
                            "WHERE in_private_group=1 AND (remarketing_stage IS NULL OR remarketing_stage='untouched')"
                        ))
                        # 3) status='replied' → replied (independente de sends)
                        conn.execute(text(
                            "UPDATE leads SET remarketing_stage='replied' "
                            "WHERE status='replied' "
                            "AND (remarketing_stage IS NULL OR remarketing_stage='untouched')"
                        ))
                        # 4) Sem sends → untouched
                        conn.execute(text("""
                            UPDATE leads SET remarketing_stage='untouched'
                            WHERE id NOT IN (SELECT DISTINCT lead_id FROM sends WHERE status='sent')
                            AND (remarketing_stage IS NULL OR remarketing_stage = '')
                        """))
                        # 5) 1 send → r1_cold (assume cooldown já passou pra leads antigos)
                        conn.execute(text("""
                            UPDATE leads SET
                                remarketing_stage = 'r1_cold',
                                r1_sent_at = (SELECT MIN(sent_at) FROM sends s WHERE s.lead_id=leads.id AND s.status='sent')
                            WHERE id IN (
                                SELECT lead_id FROM sends WHERE status='sent'
                                GROUP BY lead_id HAVING COUNT(*) = 1
                            )
                            AND (remarketing_stage IS NULL OR remarketing_stage='' OR remarketing_stage='untouched')
                        """))
                        # 6) 2 sends → r2_cold
                        conn.execute(text("""
                            UPDATE leads SET
                                remarketing_stage = 'r2_cold',
                                r1_sent_at = (SELECT MIN(sent_at) FROM sends s WHERE s.lead_id=leads.id AND s.status='sent'),
                                r2_sent_at = (SELECT MAX(sent_at) FROM sends s WHERE s.lead_id=leads.id AND s.status='sent')
                            WHERE id IN (
                                SELECT lead_id FROM sends WHERE status='sent'
                                GROUP BY lead_id HAVING COUNT(*) = 2
                            )
                            AND (remarketing_stage IS NULL OR remarketing_stage='' OR remarketing_stage='untouched')
                        """))
                        # 7) 3+ sends → r3_cold
                        conn.execute(text("""
                            UPDATE leads SET
                                remarketing_stage = 'r3_cold',
                                r1_sent_at = (SELECT MIN(sent_at) FROM sends s WHERE s.lead_id=leads.id AND s.status='sent'),
                                r3_sent_at = (SELECT MAX(sent_at) FROM sends s WHERE s.lead_id=leads.id AND s.status='sent')
                            WHERE id IN (
                                SELECT lead_id FROM sends WHERE status='sent'
                                GROUP BY lead_id HAVING COUNT(*) >= 3
                            )
                            AND (remarketing_stage IS NULL OR remarketing_stage='' OR remarketing_stage='untouched')
                        """))
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
