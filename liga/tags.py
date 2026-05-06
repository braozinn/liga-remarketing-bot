"""Tags comportamentais da Liga (derivadas, não persistidas).

Cada lead recebe uma tag que descreve seu engajamento ATUAL na Liga, baseada
em quando enviou o último comprovante. Essas tags são usadas pra direcionar
remarketing diferente:

- engaged:    enviando provas em dia (último envio ontem ou hoje)
- slipping:   parou no meio (último envio há 2+ dias) — alvo de re-engajamento
- eliminated: fora do torneio (estado terminal)
- not_started: qualificou mas ainda não enviou nenhuma prova

Quem ainda está em waiting_id/waiting_deposit/new não tem tag (None).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    BA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
except ImportError:  # pragma: no cover
    BA_TZ = None

from db.models import DailyVolume, Lead


# Tags + metadados pra UI
LIGA_TAGS = ("engaged", "slipping", "eliminated", "not_started")

LIGA_TAG_LABELS = {
    "engaged":     "🔥 Indo bem",
    "slipping":    "⚠️ Parando no meio",
    "eliminated":  "❌ Eliminado",
    "not_started": "⏳ Qualificou, sem prova",
}

LIGA_TAG_COLORS = {
    "engaged":     "success",
    "slipping":    "warning",
    "eliminated":  "dark",
    "not_started": "info",
}


def _today_ba() -> date:
    if BA_TZ:
        return datetime.now(BA_TZ).date()
    return date.today()


def get_liga_tag(lead: Lead, session) -> Optional[str]:
    """Retorna a tag de engajamento do lead na Liga, ou None se não aplicável."""
    state = (lead.liga_state or "new")

    if state == "eliminated":
        return "eliminated"

    # Só leads que estão na competição (ou em risco) recebem tags de engajamento
    if state not in ("active", "at_risk", "finalist", "waitlist"):
        return None

    # Pega a data do último DailyVolume registrado
    last_dv = (
        session.query(DailyVolume)
        .filter(DailyVolume.lead_id == lead.id)
        .order_by(DailyVolume.date.desc())
        .first()
    )

    if last_dv is None:
        return "not_started"

    try:
        last_date = date.fromisoformat(last_dv.date)
    except Exception:
        return "not_started"

    today = _today_ba()
    days_since = (today - last_date).days

    if days_since <= 1:
        # Enviou hoje ou ontem
        return "engaged"
    return "slipping"


def get_liga_tag_label(tag: Optional[str]) -> str:
    if not tag:
        return "—"
    return LIGA_TAG_LABELS.get(tag, tag)


def get_liga_tag_color(tag: Optional[str]) -> str:
    if not tag:
        return "secondary"
    return LIGA_TAG_COLORS.get(tag, "secondary")
