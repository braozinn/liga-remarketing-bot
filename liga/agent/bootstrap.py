"""Bootstrap do cérebro do agente — roda 1 vez sobre histórico.

Lê todas as DMs dos últimos N dias (default 90), pareia perguntas de leads
com suas respostas, classifica em categorias e popula AgentLearningExample.

Custo estimado: ~$5-10 USD pra ~10k mensagens (Haiku).
Tempo: ~30 min.
"""
from __future__ import annotations

import logging
from datetime import datetime, timedelta
from typing import Optional

from db import SessionLocal
from db.models import AgentLearningExample, Lead, LeadMessage

logger = logging.getLogger(__name__)


def bootstrap_voice_profile(
    days_back: int = 90,
    max_pairs: int = 5000,
    classify_each: bool = True,
) -> dict:
    """Roda 1× sobre histórico pra popular AgentLearningExample.

    Heurística de pareamento:
    - Pra cada msg `out` sua, busca a última msg `in` do lead até 2h antes
    - Se achar, forma um par e classifica via Haiku

    Args:
        days_back: quantos dias de histórico considerar (default 90)
        max_pairs: cap de segurança pra não estourar custo
        classify_each: se True, classifica cada par (gasta ~$0.0002 cada).
                       Se False, marca todos como 'complex' (rápido, sem custo).

    Returns dict com stats.
    """
    from .learning import _classify_pair_category, AGENT_CATEGORIES

    cutoff = datetime.utcnow() - timedelta(days=days_back)
    pairs_processed = 0
    pairs_created = 0
    pairs_skipped_existing = 0
    by_cat: dict[str, int] = {}

    with SessionLocal() as s:
        out_msgs = (
            s.query(LeadMessage)
            .filter(LeadMessage.direction == "out")
            .filter(LeadMessage.kind == "text")
            .filter(LeadMessage.created_at >= cutoff)
            .filter(LeadMessage.content.isnot(None))
            .order_by(LeadMessage.created_at.asc())
            .limit(max_pairs)
            .all()
        )
        out_data = [
            (m.id, m.lead_id, m.content, m.created_at)
            for m in out_msgs if (m.content or "").strip()
        ]

    logger.info(
        "[bootstrap] processando %d msgs out dos últimos %d dias",
        len(out_data), days_back,
    )

    for out_id, lead_id, your_reply, sent_at in out_data:
        try:
            with SessionLocal() as s:
                lookback = sent_at - timedelta(hours=2)
                last_in = (
                    s.query(LeadMessage)
                    .filter(LeadMessage.lead_id == lead_id)
                    .filter(LeadMessage.direction == "in")
                    .filter(LeadMessage.created_at >= lookback)
                    .filter(LeadMessage.created_at < sent_at)
                    .order_by(LeadMessage.created_at.desc())
                    .first()
                )
                if not last_in or not (last_in.content or "").strip():
                    continue
                lead_msg = last_in.content

                # Skip se já existe
                existing = (
                    s.query(AgentLearningExample)
                    .filter(AgentLearningExample.lead_id == lead_id)
                    .filter(AgentLearningExample.lead_msg == lead_msg[:5000])
                    .filter(AgentLearningExample.your_reply == your_reply[:5000])
                    .first()
                )
                if existing:
                    pairs_skipped_existing += 1
                    continue

                lead = s.query(Lead).get(lead_id)

            # Classifica
            if classify_each:
                category, confidence = _classify_pair_category(lead_msg, your_reply)
            else:
                category, confidence = "complex", 0.5

            with SessionLocal() as s:
                ex = AgentLearningExample(
                    lead_id=lead_id,
                    category=category,
                    lead_msg=lead_msg[:5000],
                    your_reply=your_reply[:5000],
                    lead_country=getattr(lead, "liga_id_country", None) if lead else None,
                    lead_is_vip=bool(getattr(lead, "is_vip_potential", False)) if lead else False,
                    quality_score=confidence,
                    in_vault=False,
                    created_at=sent_at,  # preserva timestamp original
                )
                s.add(ex)
                s.commit()

            pairs_created += 1
            by_cat[category] = by_cat.get(category, 0) + 1
            pairs_processed += 1

            if pairs_processed % 50 == 0:
                logger.info(
                    "[bootstrap] progresso: %d/%d processados, %d criados",
                    pairs_processed, len(out_data), pairs_created,
                )
        except Exception:
            logger.exception("[bootstrap] erro processando out_id=%s", out_id)

    result = {
        "ok": True,
        "out_msgs_scanned": len(out_data),
        "pairs_created": pairs_created,
        "pairs_skipped_existing": pairs_skipped_existing,
        "by_category": by_cat,
        "days_back": days_back,
    }
    logger.info("[bootstrap] FIM: %s", result)
    return result
