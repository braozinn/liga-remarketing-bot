"""Tracker - escuta DMs respondidas e novos membros do grupo privado."""
from __future__ import annotations

import logging
import re
from datetime import datetime

from telethon import events

from db import SessionLocal
from db.models import Lead, LeadStatus, Script, ScriptVariant, Send, SendStatus
from utils import classify_reply_heuristic

from .client import get_client, get_private_group_entity
from .categorizer import detect_deposit_intent, update_lead_engagement


# Padrões de opt-out — se o lead manda isso, vira BLOCKED + opted_out=True
_OPT_OUT_PATTERNS = re.compile(
    r"\b("
    r"stop|para de mandar|para com isso|me deixa|déjame en paz|dejame en paz|"
    r"d[eé]jame|no insistas|no me molestes|no me escribas|no me escriba|"
    r"basta|chega|n[aã]o quero|n[aã]o tenho interesse|no quiero|no me interesa|"
    r"unsubscribe|baixa de mi|s[aá]came|sacame|elimin[aá]me|elimin[aá]me|"
    r"bloque[aá]rte|te bloqueo|denuncio|denuncia"
    r")\b",
    re.IGNORECASE,
)


def detect_opt_out(text: str) -> str | None:
    """Detecta intenção de opt-out (parar de receber). Retorna match ou None."""
    if not text:
        return None
    m = _OPT_OUT_PATTERNS.search(text)
    return m.group(0) if m else None
from .liga_handlers import (
    handle_active_waiting_proof,
    handle_unknown,
    handle_waiting_deposit,
    handle_waiting_id,
    handle_waitlist,
)

logger = logging.getLogger(__name__)


LIGA_HANDLERS = {
    "waiting_id":      handle_waiting_id,
    "waiting_deposit": handle_waiting_deposit,
    "active":          handle_active_waiting_proof,
    "waitlist":        handle_waitlist,
}


def _bump_metrics(session, send: Send, classification: str):
    """Incrementa contadores no Script e (se houver) na Variant."""
    script = session.query(Script).get(send.script_id) if send.script_id else None
    variant = session.query(ScriptVariant).get(send.variant_id) if send.variant_id else None
    for obj in (script, variant):
        if obj is None:
            continue
        obj.replies_count = (obj.replies_count or 0) + 1
        if classification == "positive":
            obj.positive_count = (obj.positive_count or 0) + 1
        if classification == "conversion":
            obj.conversions_count = (obj.conversions_count or 0) + 1


def _bump_conversion_only(session, send: Send):
    script = session.query(Script).get(send.script_id) if send.script_id else None
    variant = session.query(ScriptVariant).get(send.variant_id) if send.variant_id else None
    for obj in (script, variant):
        if obj is None:
            continue
        obj.conversions_count = (obj.conversions_count or 0) + 1


async def start_reply_listener() -> None:
    client = await get_client()

    @client.on(events.NewMessage(incoming=True))
    async def _on_dm(event):
        try:
            if not event.is_private:
                return
            sender = await event.get_sender()
            if sender is None or sender.is_self or getattr(sender, "bot", False):
                return

            with SessionLocal() as session:
                lead = session.query(Lead).filter_by(telegram_id=sender.id).one_or_none()
                if not lead:
                    return

                # --- Roteamento Liga (estado da jornada) ---------------------
                # Roda ANTES da lógica de métricas — não a substitui.
                liga_state = getattr(lead, "liga_state", None) or "new"
                handler = LIGA_HANDLERS.get(liga_state)
                if handler is not None:
                    try:
                        await handler(event, lead, session, client)
                        # Recategoriza após o handler (ex: pode ter passado de
                        # waiting_id pra waiting_deposit, balance mudou, etc.)
                        reply_text_for_intent = (event.message.message or "").strip()
                        deposit_match = detect_deposit_intent(reply_text_for_intent)
                        update_lead_engagement(lead, session, deposit_promise_match=deposit_match, commit=False)
                        session.commit()
                    except Exception:
                        logger.exception("[liga] erro no handler %s", liga_state)
                        session.rollback()

                # --- Métricas existentes (não remover!) ----------------------
                last_send = (
                    session.query(Send)
                    .filter(Send.lead_id == lead.id)
                    .filter(Send.status == SendStatus.SENT.value)
                    .filter(Send.replied.is_(False))
                    .order_by(Send.sent_at.desc())
                    .first()
                )
                if last_send is None:
                    return

                reply_text = (event.message.message or "").strip()
                cls = classify_reply_heuristic(reply_text)
                classification = cls["classification"]

                last_send.replied = True
                last_send.replied_at = datetime.utcnow()
                last_send.reply_text = reply_text
                last_send.reply_classification = classification

                _bump_metrics(session, last_send, classification)

                # Detecta opt-out — se sim, marca BLOCKED + opted_out=True
                opt_out_match = detect_opt_out(reply_text)
                if opt_out_match:
                    lead.status = LeadStatus.BLOCKED.value
                    lead.opted_out = True
                    lead.opted_out_at = datetime.utcnow()
                    logger.warning(
                        "[opt_out] lead=%s pediu pra parar (match: %r) — BLOCKED",
                        lead.display_name, opt_out_match,
                    )
                    # NÃO responde, não faz mais nada
                elif lead.status in (LeadStatus.PENDING.value, LeadStatus.CONTACTED.value):
                    # Lead status: qualquer resposta promove pra REPLIED.
                    # CONVERTED é determinado por in_private_group ou ação manual.
                    lead.status = LeadStatus.REPLIED.value

                # Categoriza o lead em tempo real — usa a mensagem que chegou agora
                # pra detectar promessa de depósito + recalcula tag de engajamento.
                deposit_match = detect_deposit_intent(reply_text)
                update_lead_engagement(lead, session, deposit_promise_match=deposit_match, commit=False)

                session.commit()
                logger.info("Reply de %s -> %s", lead.display_name, classification)
        except Exception:
            logger.exception("Erro processando reply")

    @client.on(events.ChatAction)
    async def _on_chat_action(event):
        try:
            is_join = getattr(event, "user_added", False) or getattr(event, "user_joined", False)
            if not is_join:
                return
            try:
                group = await get_private_group_entity()
            except Exception:
                return
            if abs(event.chat_id) != abs(group.id):
                return

            user = await event.get_user()
            if not user:
                return

            with SessionLocal() as session:
                lead = session.query(Lead).filter_by(telegram_id=user.id).one_or_none()
                if not lead:
                    return
                lead.in_private_group = True
                last_send = (
                    session.query(Send)
                    .filter(Send.lead_id == lead.id)
                    .filter(Send.status == SendStatus.SENT.value)
                    .order_by(Send.sent_at.desc())
                    .first()
                )
                if last_send and not last_send.replied:
                    last_send.reply_classification = "conversion"
                    _bump_conversion_only(session, last_send)
                # in_private_group=True é o sinal canônico de "convertido".
                # Promove o status só se ainda estiver em pending/contacted.
                if lead.status in (LeadStatus.PENDING.value, LeadStatus.CONTACTED.value):
                    lead.status = LeadStatus.REPLIED.value
                session.commit()
                logger.info("Lead %s entrou no grupo!", lead.display_name)
        except Exception:
            logger.exception("Erro processando entrada no grupo")

    logger.info("Listener de respostas ativo.")
