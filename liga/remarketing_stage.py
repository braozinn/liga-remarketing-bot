"""Lógica de avanço de remarketing_stage.

Stages e regras de avanço:

  untouched → R1 enviada → r1_sent_cooldown
  r1_sent_cooldown → +24h → r1_cold
  r1_cold → R2 enviada → r2_sent_cooldown
  r2_sent_cooldown → +7d → r2_cold
  r2_cold → R3 enviada → r3_sent_cooldown
  r3_sent_cooldown → +14d → discarded (após 21d sem resposta)

Em qualquer momento:
  qualquer_stage + lead respondeu → replied
  qualquer_stage + lead entrou no grupo → converted
  qualquer_stage + opt-out → opted_out

Nada disso envia mensagem. É só etiquetagem.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

from sqlalchemy import func

from db import SessionLocal
from db.models import Lead, LeadMessage, LeadStatus, RemarketingStage, Send, SendStatus

logger = logging.getLogger(__name__)


# Cooldowns (sobrescrevíveis via .env)
def _cooldown_after_r1_hours() -> int:
    return int(os.getenv("COOLDOWN_AFTER_R1_HOURS", "24"))


def _cooldown_after_r2_days() -> int:
    return int(os.getenv("COOLDOWN_AFTER_R2_DAYS", "7"))


def _cooldown_after_r3_days() -> int:
    return int(os.getenv("COOLDOWN_AFTER_R3_DAYS", "14"))


def _discard_after_r3_days() -> int:
    return int(os.getenv("DISCARD_AFTER_R3_DAYS", "21"))


def _is_fresh_window_hours() -> int:
    """Lead com msg nas últimas N horas → is_fresh=True"""
    return int(os.getenv("FRESH_WINDOW_HOURS", "24"))


# Labels e cores pra UI (importável pelos templates)
STAGE_LABELS = {
    "untouched":         "🆕 Sem contato",
    "r1_sent_cooldown":  "📤 R1 enviada (cooldown)",
    "r1_cold":           "❄️ Elegível R2",
    "r2_sent_cooldown":  "📤 R2 enviada (cooldown)",
    "r2_cold":           "❄️ Elegível R3",
    "r3_sent_cooldown":  "📤 R3 enviada (último cooldown)",
    "discarded":         "🗑 Descartado",
    "replied":           "✅ Respondeu",
    "converted":         "🏆 Convertido",
    "opted_out":         "🚫 Opt-out",
}

STAGE_COLORS = {
    "untouched":         "secondary",
    "r1_sent_cooldown":  "info",
    "r1_cold":           "warning",
    "r2_sent_cooldown":  "info",
    "r2_cold":           "warning",
    "r3_sent_cooldown":  "info",
    "discarded":         "dark",
    "replied":           "primary",
    "converted":         "success",
    "opted_out":         "danger",
}


def label_for(stage: Optional[str]) -> str:
    if not stage:
        return "—"
    return STAGE_LABELS.get(stage, stage)


def color_for(stage: Optional[str]) -> str:
    if not stage:
        return "light"
    return STAGE_COLORS.get(stage, "secondary")


# ---------------------------------------------------------------------------
# Avanço quando você dispara um script
# ---------------------------------------------------------------------------
def advance_stage_on_send(lead: Lead, session) -> Optional[str]:
    """Chamado quando você dispara MANUALMENTE um script pro lead.

    Avança o stage:
      untouched → r1_sent_cooldown (set r1_sent_at)
      r1_cold   → r2_sent_cooldown (set r2_sent_at)
      r2_cold   → r3_sent_cooldown (set r3_sent_at, set discard_at = now + 21d)

    Retorna o novo stage ou None se não houve mudança válida.
    NÃO envia nada — só atualiza etiqueta após você ter enviado.
    """
    cur = lead.remarketing_stage or "untouched"
    now = datetime.utcnow()

    # Stages terminais — não avança
    if cur in ("converted", "opted_out", "replied", "discarded"):
        logger.debug("[stage] lead=%s em %s — não avança", lead.display_name, cur)
        return None

    if cur == "untouched":
        lead.remarketing_stage = "r1_sent_cooldown"
        lead.r1_sent_at = now
        logger.info("[stage] lead=%s untouched → r1_sent_cooldown", lead.display_name)
        return lead.remarketing_stage

    if cur == "r1_cold":
        lead.remarketing_stage = "r2_sent_cooldown"
        lead.r2_sent_at = now
        logger.info("[stage] lead=%s r1_cold → r2_sent_cooldown", lead.display_name)
        return lead.remarketing_stage

    if cur == "r2_cold":
        lead.remarketing_stage = "r3_sent_cooldown"
        lead.r3_sent_at = now
        lead.discard_at = now + timedelta(days=_discard_after_r3_days())
        logger.info("[stage] lead=%s r2_cold → r3_sent_cooldown (descarte em %s)",
                    lead.display_name, lead.discard_at)
        return lead.remarketing_stage

    # Stages em cooldown — você não DEVERIA estar disparando, mas avisamos
    if cur in ("r1_sent_cooldown", "r2_sent_cooldown", "r3_sent_cooldown"):
        logger.warning(
            "[stage] lead=%s em %s — disparo manual ignorado (em cooldown)",
            lead.display_name, cur,
        )
        return None

    return None


def mark_replied(lead: Lead) -> bool:
    """Quando lead responde — marca stage='replied' (preserva senão for terminal)."""
    cur = lead.remarketing_stage or "untouched"
    if cur in ("converted", "opted_out", "discarded"):
        return False
    if cur != "replied":
        lead.remarketing_stage = "replied"
        return True
    return False


def mark_converted(lead: Lead) -> bool:
    """Lead entrou no grupo privado."""
    if lead.remarketing_stage != "converted":
        lead.remarketing_stage = "converted"
        return True
    return False


def mark_opted_out(lead: Lead) -> bool:
    """Lead pediu pra parar."""
    if lead.remarketing_stage != "opted_out":
        lead.remarketing_stage = "opted_out"
        return True
    return False


# ---------------------------------------------------------------------------
# Cron: avança cooldowns + recalcula is_fresh + descarta velhos
# ---------------------------------------------------------------------------
async def task_cooldown_advance() -> dict:
    """Roda de hora em hora. Avança cooldowns + freshness + descarte automático.

    Não envia nada — só etiqueta.
    """
    now = datetime.utcnow()
    counts = {
        "r1_advanced": 0, "r2_advanced": 0, "r3_to_discarded": 0,
        "fresh_set": 0, "fresh_cleared": 0,
    }

    fresh_cutoff = now - timedelta(hours=_is_fresh_window_hours())
    r1_cutoff = now - timedelta(hours=_cooldown_after_r1_hours())
    r2_cutoff = now - timedelta(days=_cooldown_after_r2_days())
    r3_cutoff = now - timedelta(days=_cooldown_after_r3_days())
    discard_cutoff = now - timedelta(days=_discard_after_r3_days())

    with SessionLocal() as s:
        # 1) r1_sent_cooldown → r1_cold (após COOLDOWN_AFTER_R1_HOURS)
        n1 = (
            s.query(Lead)
            .filter(Lead.remarketing_stage == "r1_sent_cooldown")
            .filter(Lead.r1_sent_at < r1_cutoff)
            .update({Lead.remarketing_stage: "r1_cold"}, synchronize_session=False)
        )
        counts["r1_advanced"] = n1

        # 2) r2_sent_cooldown → r2_cold (após COOLDOWN_AFTER_R2_DAYS)
        n2 = (
            s.query(Lead)
            .filter(Lead.remarketing_stage == "r2_sent_cooldown")
            .filter(Lead.r2_sent_at < r2_cutoff)
            .update({Lead.remarketing_stage: "r2_cold"}, synchronize_session=False)
        )
        counts["r2_advanced"] = n2

        # 3) r3_sent_cooldown → discarded (após DISCARD_AFTER_R3_DAYS)
        n3 = (
            s.query(Lead)
            .filter(Lead.remarketing_stage == "r3_sent_cooldown")
            .filter(Lead.r3_sent_at < discard_cutoff)
            .update({Lead.remarketing_stage: "discarded"}, synchronize_session=False)
        )
        counts["r3_to_discarded"] = n3

        # 4) is_fresh — leads com last_dm_at recente
        n4 = (
            s.query(Lead)
            .filter(Lead.last_dm_at >= fresh_cutoff)
            .filter(Lead.is_fresh.is_(False))
            .update({Lead.is_fresh: True}, synchronize_session=False)
        )
        counts["fresh_set"] = n4

        # 5) is_fresh expirado — leads sem msg recente
        n5 = (
            s.query(Lead)
            .filter((Lead.last_dm_at < fresh_cutoff) | (Lead.last_dm_at.is_(None)))
            .filter(Lead.is_fresh.is_(True))
            .update({Lead.is_fresh: False}, synchronize_session=False)
        )
        counts["fresh_cleared"] = n5

        s.commit()

    if any(counts.values()):
        logger.info("[stage] cooldown advance: %s", counts)
    return counts


# ---------------------------------------------------------------------------
# Helpers de query (pra UI usar)
# ---------------------------------------------------------------------------
def count_by_stage() -> dict:
    """Counts agregados por stage. Útil pro dashboard."""
    with SessionLocal() as s:
        rows = (
            s.query(Lead.remarketing_stage, func.count(Lead.id))
            .group_by(Lead.remarketing_stage)
            .all()
        )
    out = {stage: 0 for stage in STAGE_LABELS}
    for stage, n in rows:
        out[stage or "untouched"] = n
    return out


def is_eligible_for_dispatch(lead: Lead) -> tuple[bool, str]:
    """Decide se você PODE disparar um script pra esse lead agora.

    Retorna (eligible, reason).
    """
    if lead.opted_out:
        return False, "lead pediu opt-out"
    if lead.in_private_group:
        return False, "lead já está no grupo privado"
    if lead.is_fresh:
        return False, "lead mandou DM nas últimas 24h — esperar"
    cur = lead.remarketing_stage or "untouched"
    if cur in ("untouched", "r1_cold", "r2_cold"):
        return True, f"elegível (stage atual: {cur})"
    if cur in ("r1_sent_cooldown", "r2_sent_cooldown", "r3_sent_cooldown"):
        return False, f"em cooldown ({cur})"
    if cur in ("converted", "opted_out", "replied", "discarded"):
        return False, f"stage terminal ({cur})"
    return False, f"stage desconhecido ({cur})"


# ---------------------------------------------------------------------------
# HELPER CENTRALIZADO de seleção de leads pra disparo
# ---------------------------------------------------------------------------
def eligible_leads_for_dispatch(
    session,
    script,
    *,
    campaign=None,
    max_leads: int = 0,
    target_stage_override: Optional[str] = None,
    target_engagement_override: Optional[str] = None,
    exclude_vips: bool = True,
    exclude_lead_ids_extra: Optional[list] = None,
    private_member_telegram_ids: Optional[set] = None,
) -> tuple[list, dict]:
    """Single source of truth: TODOS os caminhos de envio chamam essa função.

    Garante consistência entre /preview-dispatch, run_campaign, run_follow_ups,
    process-queue, etc. Antes era duplicado em 4+ lugares com pequenas diferenças
    que viraram bugs (exemplo: scheduler usava Lead.status==pending, preview
    usava remarketing_stage).

    Returns (leads_to_send, stats):
      leads_to_send: lista de Lead já capada por max_leads, ordenada por last_dm_at desc
      stats: dict com counts (total_eligible, skipped_fresh, skipped_other,
             excluded_manual, capped_by_max, etc)
    """
    from sqlalchemy import desc as _desc, or_ as _or, and_ as _and
    from db.models import Lead, ScriptExcludedLead, LeadStatus

    # Stage e engagement: prioridade ao override (campaign > param explicit > script)
    if campaign is not None and getattr(campaign, "target_remarketing_stage", None):
        effective_stage = campaign.target_remarketing_stage
    else:
        effective_stage = (target_stage_override or "").strip() or script.target_remarketing_stage

    if campaign is not None and getattr(campaign, "target_engagement_tag", None):
        effective_engagement = campaign.target_engagement_tag
    else:
        effective_engagement = target_engagement_override or script.target_engagement_tag

    q = (
        session.query(Lead)
        .filter(Lead.opted_out.is_(False))
        .filter(Lead.in_private_group.is_(False))
        .filter(Lead.status.notin_([
            LeadStatus.BLOCKED.value, LeadStatus.EXCLUDED.value,
        ]))
    )

    if effective_stage:
        q = q.filter(Lead.remarketing_stage == effective_stage)
    if effective_engagement:
        q = q.filter(Lead.engagement_tag == effective_engagement)

    if private_member_telegram_ids:
        q = q.filter(~Lead.telegram_id.in_(list(private_member_telegram_ids)))

    if exclude_vips:
        q = q.filter(_or(Lead.is_vip_potential.is_(False), Lead.is_vip_potential.is_(None)))

    # Exclusões manuais via ScriptExcludedLead
    excluded_ids = set(
        row[0] for row in session.query(ScriptExcludedLead.lead_id)
        .filter(ScriptExcludedLead.script_id == script.id).all()
    )
    if exclude_lead_ids_extra:
        excluded_ids.update(exclude_lead_ids_extra)
    if excluded_ids:
        q = q.filter(~Lead.id.in_(list(excluded_ids)))

    # Ordenação consistente: last_dm_at DESC (mais recentes primeiro), id DESC pra empate
    q = q.order_by(_desc(Lead.last_dm_at), _desc(Lead.id))

    candidates = q.all()

    # Aplica is_eligible_for_dispatch (cooldowns, fresh, etc)
    eligible_active = []
    skipped_fresh = 0
    skipped_other = 0
    for l in candidates:
        ok, reason = is_eligible_for_dispatch(l)
        if ok:
            eligible_active.append(l)
        elif "fresh" in (reason or "") or l.is_fresh:
            skipped_fresh += 1
        else:
            skipped_other += 1

    total_eligible = len(eligible_active)
    capped = False
    if max_leads and max_leads > 0 and total_eligible > max_leads:
        eligible_active = eligible_active[:max_leads]
        capped = True

    stats = {
        "total_candidates": len(candidates),
        "total_eligible_before_cap": total_eligible,
        "after_cap": len(eligible_active),
        "skipped_fresh": skipped_fresh,
        "skipped_other": skipped_other,
        "excluded_manual": len(excluded_ids),
        "capped_by_max": capped,
        "effective_stage": effective_stage,
        "effective_engagement": effective_engagement,
        "max_leads": max_leads,
    }

    return eligible_active, stats


def next_action_for(lead: Lead) -> str:
    """Sugere a próxima ação possível pro lead."""
    cur = lead.remarketing_stage or "untouched"
    if lead.opted_out or cur == "opted_out":
        return "Não tocar — lead pediu opt-out"
    if cur == "converted":
        return "Convertido — manter relacionamento"
    if cur == "discarded":
        return "Descartado — arquivado"
    if cur == "replied":
        return "Conversa em andamento — você responde"

    if lead.is_fresh:
        return "Lead respondeu nas últimas 24h — aguardar"

    if cur == "untouched":
        return "Pode disparar R1 (1º contato)"
    if cur == "r1_sent_cooldown":
        if lead.r1_sent_at:
            unlock = lead.r1_sent_at + timedelta(hours=_cooldown_after_r1_hours())
            hours = max(0, int((unlock - datetime.utcnow()).total_seconds() / 3600))
            return f"Em cooldown R1 — destrava em {hours}h"
        return "Em cooldown R1"
    if cur == "r1_cold":
        return "Pode disparar R2 (durante ação do grupo)"
    if cur == "r2_sent_cooldown":
        if lead.r2_sent_at:
            unlock = lead.r2_sent_at + timedelta(days=_cooldown_after_r2_days())
            days = max(0, (unlock - datetime.utcnow()).days)
            return f"Em cooldown R2 — destrava em {days}d"
        return "Em cooldown R2"
    if cur == "r2_cold":
        return "Pode disparar R3 (última chance)"
    if cur == "r3_sent_cooldown":
        if lead.discard_at:
            days = max(0, (lead.discard_at - datetime.utcnow()).days)
            return f"R3 enviada — descarte automático em {days}d"
        return "R3 enviada — aguardando"
    return "—"
