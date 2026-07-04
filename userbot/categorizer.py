"""Categoriza leads em tags de engajamento, em tempo real ou em batch.

As tags ditam qual tipo de remarketing o lead recebe.

Tags:
- first_contact_no_reply : recebeu o 1º contato (vídeo bolinha + script de registro)
                           e não respondeu nada
- account_no_deposit     : criou a conta e mandou o ID, mas saldo real = 0
- remarketed_no_account  : sem conta, mas já recebeu N+ disparos de remarketing
- deposit_promised       : tem conta E mencionou intenção de depositar (texto)
- deposited              : tem saldo real > 0 (depositou de fato)
- None                   : não classificado ainda (lead muito novo / sem dados)
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import re
from datetime import datetime
from typing import Optional

from telethon.errors import FloodWaitError

from db import SessionLocal
from db.models import Lead, Send, SendStatus

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Catálogo de tags
# ---------------------------------------------------------------------------
ENGAGEMENT_TAGS = (
    "first_contact_no_reply",
    "account_no_deposit",
    "remarketed_no_account",
    "deposit_promised",
    "deposited",
)

ENGAGEMENT_TAG_LABELS = {
    "first_contact_no_reply": "🥶 Frio (sem resposta ao 1º contato)",
    "account_no_deposit":     "💤 Conta criada, sem depósito",
    "remarketed_no_account":  "📨 Remarketing sem conta",
    "deposit_promised":       "💰 Prometeu depositar",
    "deposited":              "✅ Depositou",
}

ENGAGEMENT_TAG_COLORS = {
    "first_contact_no_reply": "secondary",
    "account_no_deposit":     "info",
    "remarketed_no_account":  "warning",
    "deposit_promised":       "primary",
    "deposited":              "success",
}


def get_engagement_tag_label(tag: Optional[str]) -> str:
    if not tag:
        return "—"
    return ENGAGEMENT_TAG_LABELS.get(tag, tag)


def get_engagement_tag_color(tag: Optional[str]) -> str:
    if not tag:
        return "light text-dark"
    return ENGAGEMENT_TAG_COLORS.get(tag, "secondary")


# ---------------------------------------------------------------------------
# Detector de "intenção de depositar" — heurístico, ES + PT
# ---------------------------------------------------------------------------
# Token genérico que casa "deposito", "depósito", "depositar", "depositó", "deposit" etc.
# (acento opcional no 'o', sufixo de até 5 chars)
_DEP = r"dep[oó]sit[oóa]?\w{0,5}"

_DEPOSIT_INTENT_PATTERNS = [
    # ES — formas verbais com "voy a / voy / tengo que"
    re.compile(r"\b(voy|voi|ire|ir[ée]|tengo que)\s+a?\s*(depositar|poner|meter|colocar|cargar|hacer\s+el\s+(d[eé]p[oó]sito|pago))", re.IGNORECASE),
    # ES — "depositaré" futuro
    re.compile(r"\bdep[oó]sit?ar[éeí]\b", re.IGNORECASE),
    # ES — "ya depositó/pagué/cargué"
    re.compile(rf"\bya\s+(d[eé]p[oó]sit[oó]?|pago|cargo|carg[oé]|pagu[eé]|hice\s+el\s+d[eé]p[oó]sito)", re.IGNORECASE),
    # ES — "cuando cobre/reciba/tenga plata"
    re.compile(r"\b(cuando|cuanto)\s+(cobre|cobro|me\s+pague|reciba|recibo|tenga\s+plata|tenga\s+dinero)", re.IGNORECASE),
    # ES — "deposito + dia/semana/mes próxima"
    re.compile(rf"\b({_DEP}|pago|cargar|cargo)\s+(la|el)\s+(semana|mes|d[ií]a)\s+(que\s+viene|pr[oó]xim[oa])", re.IGNORECASE),
    # ES — "en X días deposit" ou "deposit en X días"
    re.compile(rf"\b(en|dentro\s+de)\s+(\d+|un[oa]?s?|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s+(d[ií]as?|horas?|semanas?)\s+.{{0,15}}{_DEP}", re.IGNORECASE),
    re.compile(rf"\b{_DEP}\b.{{0,20}}\b(en|dentro\s+de)\s+(\d+|un[oa]?s?|dos|tres|cuatro|cinco|seis|siete|ocho|nueve|diez)\s+(d[ií]as?|horas?|semanas?)", re.IGNORECASE),

    # PT — formas verbais
    re.compile(r"\b(vou|irei|tenho que)\s+(depositar|colocar|p[ôo]r|fazer\s+o\s+d[eé]p[oó]sito)", re.IGNORECASE),
    re.compile(r"\bdepositarei\b", re.IGNORECASE),
    re.compile(rf"\bquando\s+(receber|cair|pagar)\b.*\b{_DEP}", re.IGNORECASE),

    # GENÉRICO — temporal + deposit em qualquer ordem (até 50 chars de distância)
    re.compile(rf"\b(hoy|hoje|ma[ñn]ana|amanh[ãa]|despu[eé]s|depois|esta\s+(noche|semana|tarde))\b.{{0,50}}\b{_DEP}", re.IGNORECASE | re.DOTALL),
    re.compile(rf"\b{_DEP}\b.{{0,50}}\b(hoy|hoje|ma[ñn]ana|amanh[ãa]|despu[eé]s|depois|esta\s+(noche|semana|tarde))\b", re.IGNORECASE | re.DOTALL),

    # Genérico — número + "USD"/"$" + "deposito" próximos
    re.compile(rf"(?:\$\s*\d+\s+(?:de\s+)?{_DEP}|{_DEP}\s*\$?\s*\d+\s*(?:USD|usd|d[oó]lar))", re.IGNORECASE),
]


def detect_deposit_intent(text: str) -> Optional[str]:
    """Retorna o trecho que casou (até 80 chars) ou None."""
    if not text:
        return None
    text = text.strip()
    if len(text) > 1000:
        text = text[:1000]
    for pat in _DEPOSIT_INTENT_PATTERNS:
        m = pat.search(text)
        if m:
            start = max(0, m.start() - 10)
            end = min(len(text), m.end() + 30)
            return text[start:end].strip()[:120]
    return None


# ---------------------------------------------------------------------------
# Classificador principal — usa só dados em DB (rápido)
# ---------------------------------------------------------------------------
def categorize_lead_from_db(lead: Lead, session, deposit_promise_match: Optional[str] = None) -> tuple[Optional[str], dict]:
    """Decide a tag de engajamento usando dados já no banco + opcional deposit_promise_match.

    Retorna (tag, evidence_dict). evidence é o que justifica a classificação.
    """
    evidence = {}

    # Conta os Sends do lead
    try:
        sends = (
            session.query(Send)
            .filter(Send.lead_id == lead.id)
            .filter(Send.status == SendStatus.SENT.value)
            .all()
        )
        sends_count = len(sends)
        any_replied = any(s.replied for s in sends)
    except Exception:
        sends_count = 0
        any_replied = False

    has_account_id = bool((lead.liga_account_id or "").strip())
    id_validated = lead.liga_id_status == "validated"
    real_balance = (
        (lead.liga_id_balance if lead.liga_id_balance is not None else None)
        or (lead.liga_balance if (lead.liga_balance or 0) > 0 else None)
        or 0
    )
    # "Depositou" = pôs dinheiro EM QUALQUER MOMENTO. Checa o total depositado
    # (deposits_sum), NÃO só o saldo atual — quem depositou $85 e operou pra $0
    # de saldo AINDA depositou. Antes só olhava balance e marcava errado.
    has_deposit = (
        (lead.liga_id_balance or 0) > 0
        or (lead.liga_balance or 0) > 0
        or (lead.liga_id_deposits_sum or 0) > 0
    )
    has_replied = any_replied or lead.status == "replied"

    evidence.update({
        "sends_count": sends_count,
        "any_replied": any_replied,
        "has_account_id": has_account_id,
        "id_validated": id_validated,
        "real_balance": real_balance,
        "has_deposit": has_deposit,
    })

    # 1) Já depositou (saldo real > 0) — prioridade máxima
    if has_deposit:
        evidence["reason"] = "saldo_real > 0"
        return "deposited", evidence

    # 2) Tem conta + mencionou depósito futuro
    if has_account_id and deposit_promise_match:
        evidence["reason"] = "tem conta + intent_match"
        evidence["match"] = deposit_promise_match
        return "deposit_promised", evidence

    # 3) Tem conta mas saldo zero
    if has_account_id:
        evidence["reason"] = "tem ID + saldo 0"
        return "account_no_deposit", evidence

    # 4) Sem conta + recebeu remarketing (2+ envios)
    if sends_count >= 2:
        evidence["reason"] = "sem ID + 2+ disparos"
        return "remarketed_no_account", evidence

    # 5) Sem conta + recebeu 1 contato e nunca respondeu
    if sends_count >= 1 and not has_replied:
        evidence["reason"] = "1º contato sem resposta"
        return "first_contact_no_reply", evidence

    # 6) Mencionou depósito sem ter conta — também conta como prometeu
    if deposit_promise_match:
        evidence["reason"] = "sem conta + intent_match"
        evidence["match"] = deposit_promise_match
        return "deposit_promised", evidence

    # Sem dados suficientes
    return None, evidence


def update_lead_engagement(
    lead: Lead,
    session,
    *,
    deposit_promise_match: Optional[str] = None,
    commit: bool = False,
) -> Optional[str]:
    """Recalcula engagement_tag do lead e salva. Retorna a tag nova."""
    new_tag, evidence = categorize_lead_from_db(lead, session, deposit_promise_match)
    old_tag = lead.engagement_tag

    if new_tag != old_tag or deposit_promise_match:
        lead.engagement_tag = new_tag
        lead.engagement_tag_updated_at = datetime.utcnow()
        try:
            lead.engagement_evidence = json.dumps(evidence, ensure_ascii=False, default=str)[:2000]
        except Exception:
            lead.engagement_evidence = None
        if commit:
            session.commit()
        if new_tag != old_tag:
            logger.info(
                "[engagement] lead=%s %s -> %s (%s)",
                lead.display_name, old_tag or "—", new_tag or "—",
                evidence.get("reason", "?"),
            )
    return new_tag


# ---------------------------------------------------------------------------
# Análise de DMs de um lead pra detectar intenção de depósito (varre histórico)
# ---------------------------------------------------------------------------
async def scan_lead_dms_for_deposit_intent(client, lead: Lead, max_messages: int = 50) -> Optional[str]:
    """Procura nos N últimos DMs do lead alguma menção de intenção de depositar.
    Retorna o trecho match ou None.
    """
    try:
        async for msg in client.iter_messages(lead.telegram_id, limit=max_messages):
            # Só mensagens do LEAD (não do bot)
            if msg.out:
                continue
            text = (msg.message or "").strip()
            if not text:
                continue
            match = detect_deposit_intent(text)
            if match:
                return match
    except FloodWaitError as e:
        logger.warning("[engagement] FloodWait %ds em user_id=%s", e.seconds, lead.telegram_id)
    except Exception:
        logger.debug("[engagement] erro varrendo DMs", exc_info=True)
    return None


# ---------------------------------------------------------------------------
# Batch — recategoriza TODOS os leads (com fetch de DMs pra detectar intenção)
# ---------------------------------------------------------------------------
class CategorizationProgress:
    """State global pra acompanhar o progresso de uma reclassificação em massa."""
    running = False
    started_at: Optional[datetime] = None
    finished_at: Optional[datetime] = None
    total = 0
    done = 0
    counts: dict = {}
    last_lead = ""

    @classmethod
    def reset(cls):
        cls.running = True
        cls.started_at = datetime.utcnow()
        cls.finished_at = None
        cls.total = 0
        cls.done = 0
        cls.counts = {tag: 0 for tag in ENGAGEMENT_TAGS}
        cls.counts["null"] = 0
        cls.last_lead = ""

    @classmethod
    def snapshot(cls) -> dict:
        return {
            "running": cls.running,
            "started_at": cls.started_at.isoformat() if cls.started_at else None,
            "finished_at": cls.finished_at.isoformat() if cls.finished_at else None,
            "total": cls.total,
            "done": cls.done,
            "percent": round(100 * cls.done / cls.total, 1) if cls.total else 0.0,
            "counts": cls.counts,
            "last_lead": cls.last_lead,
        }


async def recategorize_all_leads(
    *,
    scan_messages: bool = True,
    max_messages_per_lead: int = 30,
    delay_between_leads: float = 0.4,
) -> dict:
    """Reclassifica TODOS os leads. Pode demorar muito. Idempotente.

    - scan_messages=True: também faz fetch de DMs pra detectar intenção de depósito
    - delay pra ser educado com a API do Telegram
    """
    if CategorizationProgress.running:
        return {"error": "Já tem uma reclassificação rodando"}

    from .client import get_client
    client = await get_client() if scan_messages else None

    CategorizationProgress.reset()

    # Carrega lista de leads (snapshot dos IDs pra não bloquear sessão)
    with SessionLocal() as s:
        lead_ids = [row[0] for row in s.query(Lead.id).all()]
    CategorizationProgress.total = len(lead_ids)
    logger.info("[engagement] reclassificação iniciada: %d leads", len(lead_ids))

    try:
        for lead_id in lead_ids:
            try:
                with SessionLocal() as s:
                    lead = s.query(Lead).get(lead_id)
                    if lead is None:
                        continue
                    CategorizationProgress.last_lead = lead.display_name

                    deposit_match = None
                    if scan_messages and client is not None and not lead.in_private_group:
                        try:
                            deposit_match = await scan_lead_dms_for_deposit_intent(
                                client, lead, max_messages=max_messages_per_lead,
                            )
                        except FloodWaitError as e:
                            await asyncio.sleep(min(e.seconds, 60))
                            deposit_match = None
                        except Exception:
                            deposit_match = None

                    new_tag = update_lead_engagement(
                        lead, s, deposit_promise_match=deposit_match, commit=True,
                    )
                    key = new_tag if new_tag else "null"
                    CategorizationProgress.counts[key] = CategorizationProgress.counts.get(key, 0) + 1
                CategorizationProgress.done += 1

                # Pequeno delay pra Telegram
                if scan_messages and delay_between_leads > 0:
                    await asyncio.sleep(delay_between_leads)
            except Exception:
                logger.exception("[engagement] erro processando lead %s", lead_id)
                CategorizationProgress.done += 1
    finally:
        CategorizationProgress.running = False
        CategorizationProgress.finished_at = datetime.utcnow()
        logger.info("[engagement] reclassificação concluída: %s", CategorizationProgress.snapshot())

    return CategorizationProgress.snapshot()
