"""Lifecycle do Lead — fonte única da verdade do estado.

ANTES (caos): tinha `status` (LeadStatus enum), `liga_state` (estado do
funil VIP), `engagement_tag` (sub-tag), `remarketing_stage` (rodadas R1-R3),
todos misturados sem regra clara de quem manda em quem.

AGORA: 1 campo `lifecycle` com 4 valores determinísticos:
- new → catalogamos mas nunca conversamos
- lead → conversamos, mas ainda não criou conta Quotex
- deposited → criou conta E depositou >= mínimo ($20)
- vip → entrou no grupo privado (conversão final)

Outros campos (status legado, engagement_tag, remarketing_stage) viram
sub-tags ou flags — nunca substituem o lifecycle como fonte da verdade.

REGRA DE OURO: NUNCA mude lifecycle fora deste módulo. Sempre use as
funções `mark_*` que documentam a transição + atualizam timestamps.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Literal, Optional

from db import SessionLocal
from db.models import Lead

logger = logging.getLogger(__name__)

LifecycleValue = Literal["new", "lead", "deposited", "vip"]

# Transições válidas (grafo direcionado)
# new → lead → deposited → vip
# Pode pular etapas (ex: lead já chega com print de saldo $50 → vai direto pra deposited)
# Mas NUNCA volta atrás (vip não vira lead de novo)
ALLOWED_TRANSITIONS = {
    "new": {"lead", "deposited", "vip"},        # pode pular
    "lead": {"deposited", "vip"},                # pode pular waiting_deposit
    "deposited": {"vip"},                        # depósito feito → próxima é grupo
    "vip": set(),                                # final — não muda mais
}


def get_lifecycle(lead_id: int) -> Optional[str]:
    """Retorna o lifecycle atual do lead, ou None se lead não existe."""
    with SessionLocal() as s:
        lead = s.query(Lead).get(lead_id)
        return lead.lifecycle if lead else None


def _set_lifecycle(lead_id: int, new_value: LifecycleValue, reason: str) -> bool:
    """Atualiza lifecycle com validação de transição. Uso interno.

    Use as funções `mark_*` específicas em vez de chamar isso direto.

    Returns True se atualizou, False se transição inválida ou lead não existe.
    """
    with SessionLocal() as s:
        lead = s.query(Lead).get(lead_id)
        if not lead:
            logger.warning("[lifecycle] lead %d não existe", lead_id)
            return False

        current = lead.lifecycle or "new"
        if current == new_value:
            logger.debug("[lifecycle] lead %d já está em %s — noop", lead_id, new_value)
            return True

        if new_value not in ALLOWED_TRANSITIONS.get(current, set()):
            logger.warning(
                "[lifecycle] TRANSIÇÃO INVÁLIDA pra lead %d: %s → %s (motivo: %s)",
                lead_id, current, new_value, reason,
            )
            return False

        lead.lifecycle = new_value
        lead.updated_at = datetime.utcnow()
        # Mantém last_bot_action pra trace de quem causou a mudança
        lead.last_bot_action = f"lifecycle:{current}->{new_value}:{reason[:80]}"
        s.commit()
        logger.info(
            "[lifecycle] lead=%d %s → %s (motivo: %s)",
            lead_id, current, new_value, reason,
        )
        return True


# ═══ FUNÇÕES PÚBLICAS — use estas pra mudar lifecycle ═══════════════════════

def mark_as_lead(lead_id: int, reason: str = "primeira_interacao") -> bool:
    """Lead conversou pela primeira vez (saiu do 'new')."""
    return _set_lifecycle(lead_id, "lead", reason)


def mark_as_deposited(lead_id: int, reason: str = "deposito_validado") -> bool:
    """Lead criou conta E depositou >= mínimo. Próxima etapa é entrar no grupo."""
    return _set_lifecycle(lead_id, "deposited", reason)


def mark_as_vip(lead_id: int, reason: str = "entrou_no_grupo") -> bool:
    """Lead entrou no grupo privado VIP — conversão FINAL."""
    return _set_lifecycle(lead_id, "vip", reason)


def mark_as_blocked(lead_id: int, reason: str = "lead_bloqueou_bot") -> bool:
    """Lead bloqueou o bot. Não muda lifecycle, só seta flag.

    Bot pode continuar mandando msg pra outros leads — esse fica imune.
    """
    with SessionLocal() as s:
        lead = s.query(Lead).get(lead_id)
        if not lead:
            return False
        lead.blocked = True
        lead.last_bot_action = f"blocked:{reason[:80]}"
        s.commit()
        logger.info("[lifecycle] lead=%d BLOCKED (%s)", lead_id, reason)
        return True


def mark_as_opted_out(lead_id: int, reason: str = "lead_pediu_parar") -> bool:
    """Lead pediu pra parar de receber msg. Set flag opted_out.

    Não muda lifecycle (lead pode estar em qualquer estágio quando pede pra
    parar). Só impede futuros envios.
    """
    with SessionLocal() as s:
        lead = s.query(Lead).get(lead_id)
        if not lead:
            return False
        lead.opted_out = True
        lead.opted_out_at = datetime.utcnow()
        lead.last_bot_action = f"opted_out:{reason[:80]}"
        s.commit()
        logger.info("[lifecycle] lead=%d OPTED_OUT (%s)", lead_id, reason)
        return True


# ═══ HELPERS — leitura ═══════════════════════════════════════════════════════

def is_eligible_for_remarketing(lead: Lead) -> tuple[bool, str]:
    """Verifica se um lead pode receber mensagem de remarketing.

    Filtros aplicados:
    - opted_out=False
    - blocked=False
    - in_private_group=False (já é VIP, não precisa remarketing)
    - lifecycle != 'vip'

    Returns (elegivel, motivo_se_nao).
    """
    if getattr(lead, "opted_out", False):
        return False, "opted_out"
    if getattr(lead, "blocked", False):
        return False, "blocked"
    if getattr(lead, "in_private_group", False):
        return False, "ja_no_grupo_vip"
    if (lead.lifecycle or "new") == "vip":
        return False, "lifecycle_vip"
    return True, ""


def stats_by_lifecycle() -> dict:
    """Retorna contagem de leads por lifecycle pra o painel."""
    from sqlalchemy import func as _func
    with SessionLocal() as s:
        rows = (
            s.query(Lead.lifecycle, _func.count(Lead.id))
            .group_by(Lead.lifecycle)
            .all()
        )
    counts = {"new": 0, "lead": 0, "deposited": 0, "vip": 0}
    for lc, cnt in rows:
        counts[lc or "new"] = cnt
    return counts
