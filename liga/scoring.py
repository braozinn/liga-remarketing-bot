"""Cálculo de score de lead e tier (vip/hot/warm/cold)."""
from __future__ import annotations

import logging

from sqlalchemy import func

from db.models import DailyVolume, Lead, LeadStatus

logger = logging.getLogger(__name__)


def calc_lead_score(lead: Lead, session) -> int:
    """Score de prioridade do lead (0–200).

    Combina:
    - Velocidade de resposta (até 25 pts)
    - Depósito (até 100 pts)
    - Status de conversão (até 30 pts)
    - Engajamento no grupo (até 60 pts)
    - Sequência diária (até 30 pts)
    - Volume acumulado (5 pts por $100)

    Cap final: 200.
    """
    score = 0

    # Velocidade de resposta (usa last_dm_at e created_at)
    if lead.last_dm_at and lead.created_at:
        try:
            horas = (lead.last_dm_at - lead.created_at).total_seconds() / 3600
            if horas < 2:
                score += 25
            elif horas < 24:
                score += 10
        except Exception:
            pass

    # Depósito
    bal = lead.liga_balance or 0.0
    if bal >= 100:
        score += 100
    elif bal > 0:
        score += 50

    # Status: respondeu = engajado / no grupo privado = "convertido"
    if lead.in_private_group:
        score += 30
    if lead.status == LeadStatus.REPLIED.value:
        score += 15

    # Engajamento no grupo
    if lead.in_leads_group:
        score += 20
    if lead.in_private_group:
        score += 40

    # Sequência ativa
    streak = lead.streak_days or 0
    if streak >= 7:
        score += 30
    elif streak >= 3:
        score += 15

    # Volume acumulado
    try:
        total_vol = (
            session.query(func.sum(DailyVolume.volume_usd))
            .filter(DailyVolume.lead_id == lead.id)
            .scalar()
            or 0.0
        )
    except Exception:
        total_vol = 0.0
    score += int(total_vol / 100) * 5

    final = min(score, 200)
    return final


def get_lead_tier(score: int) -> str:
    """Retorna o tier de prioridade do lead a partir do score."""
    if score >= 90:
        return "vip"
    if score >= 60:
        return "hot"
    if score >= 30:
        return "warm"
    return "cold"
