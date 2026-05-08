"""Tracker - escuta DMs respondidas e novos membros do grupo privado."""
from __future__ import annotations

import logging
import os
import re
from datetime import datetime

from telethon import events


def _auto_reply_enabled() -> bool:
    """Lê flag AUTO_REPLY do .env. Default: 0 (modo passivo). Quando 0, handlers
    da Liga NÃO respondem leads automaticamente — só catalogam dados."""
    return os.getenv("AUTO_REPLY", "0").strip() in ("1", "true", "True", "yes")

from db import SessionLocal
from db.models import Lead, LeadMessage, LeadStatus, Script, ScriptVariant, Send, SendStatus
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


async def _passive_image_capture(event, lead, session, client) -> bool:
    """Em modo passivo (AUTO_REPLY=0), quando lead manda screenshot:
    - Extrai ID + saldo da imagem (Claude Vision)
    - Valida no @QuotexPartnerBot
    - Atualiza lead na hora
    - SEM responder nada ao lead

    Retorna True se extraiu/atualizou algo.
    """
    # Skipa se lead já tem ID validado/inválido
    if lead.liga_id_status in ("validated", "invalid"):
        return False
    # Skipa se já estiver no grupo / opted_out
    if lead.in_private_group or getattr(lead, "opted_out", False):
        return False
    # Só processa se for imagem
    msg = event.message
    is_image = bool(getattr(msg, "photo", None))
    if not is_image:
        doc = getattr(msg, "document", None)
        if doc and (getattr(doc, "mime_type", "") or "").startswith("image/"):
            is_image = True
    if not is_image:
        return False

    # Baixa a imagem
    try:
        import io
        buf = io.BytesIO()
        await msg.download_media(buf)
        img_bytes = buf.getvalue()
        if not img_bytes:
            return False
    except Exception:
        logger.debug("[passivo] erro baixando imagem", exc_info=True)
        return False

    # Roda Claude Vision
    try:
        from ai.providers import analyze_account_screenshot, check_image_seen_for_other_lead
        result = analyze_account_screenshot(img_bytes, lead_id=lead.id)
    except Exception:
        logger.debug("[passivo] erro analisando imagem", exc_info=True)
        return False

    # Anti-fraude: imagem já vista pra outro lead
    img_hash = result.get("_image_hash")
    if img_hash:
        try:
            other = check_image_seen_for_other_lead(img_hash, lead.id)
            if other:
                logger.warning(
                    "[passivo] %s mandou print já visto pro lead_id=%d (FRAUDE?)",
                    lead.display_name, other,
                )
                # Marca como rejeitado mas NÃO altera ID
                from db.models import OperationProof
                from datetime import datetime as _dt
                import json as _json
                rej = OperationProof(
                    lead_id=lead.id,
                    proof_date=_dt.utcnow().strftime("%Y-%m-%d"),
                    volume_usd=result.get("saldo_real_usd") or 0.0,
                    confidence=(result.get("confianca") or "baixa")[:10],
                    validated=False,
                    needs_review=True,
                    review_reason="duplicate_image",
                    raw_ai_response=_json.dumps(
                        {**result, "rejected_reason": "duplicate_image",
                         "duplicate_of_lead_id": other},
                        ensure_ascii=False,
                    ),
                )
                session.add(rej)
                # Notifica admin via DM
                try:
                    from liga.notifications import notify_vision_failed
                    import asyncio
                    asyncio.create_task(notify_vision_failed(
                        client, lead, reason="duplicate_image",
                        details=f"mesma imagem já enviada por lead_id={other}",
                    ))
                except Exception:
                    pass
                return True
        except Exception:
            pass

    # Vision falhou em extrair ID OU saldo → registra pra revisão
    if not result.get("id_conta") and not result.get("saldo_real_usd"):
        try:
            from db.models import OperationProof
            from datetime import datetime as _dt
            import json as _json
            rej = OperationProof(
                lead_id=lead.id,
                proof_date=_dt.utcnow().strftime("%Y-%m-%d"),
                volume_usd=0.0,
                confidence=(result.get("confianca") or "baixa")[:10],
                validated=False,
                needs_review=True,
                review_reason="vision_failed",
                raw_ai_response=_json.dumps(result, ensure_ascii=False)[:50000],
            )
            session.add(rej)
            from liga.notifications import notify_vision_failed
            import asyncio
            asyncio.create_task(notify_vision_failed(
                client, lead, reason="vision_failed",
                details=result.get("erro") or "IA não retornou ID nem saldo",
            ))
            logger.info("[passivo] vision falhou pra %s — admin notificado", lead.display_name)
        except Exception:
            logger.debug("[passivo] erro registrando vision_failed", exc_info=True)
        return False

    # Extrai ID
    from .leads import _looks_like_valid_id, validate_id_via_partner_bot
    cand_id = result.get("id_conta")
    saldo_real = result.get("saldo_real_usd")
    confianca = (result.get("confianca") or "baixa").lower()
    valido = bool(result.get("valido"))

    if not cand_id or not _looks_like_valid_id(cand_id):
        logger.debug("[passivo] ID inválido/não encontrado em %s", lead.display_name)
        return False

    # Mismatch check — bot já tem ID diferente registrado
    if (lead.liga_account_id or "").strip() and lead.liga_account_id != str(cand_id):
        import json as _json
        from db.models import OperationProof
        from datetime import datetime as _dt
        # Registra como tentativa rejeitada — admin revisa manualmente
        rej = OperationProof(
            lead_id=lead.id,
            proof_date=_dt.utcnow().strftime("%Y-%m-%d"),
            volume_usd=saldo_real or 0.0,
            account_id_raw=str(cand_id)[:100],
            validated=False,
            raw_ai_response=_json.dumps(
                {**result, "rejected_reason": "id_mismatch"},
                ensure_ascii=False,
            ),
        )
        session.add(rej)
        logger.warning(
            "[passivo] mismatch ID lead=%s registrado=%s novo=%s — registrado pra revisão",
            lead.display_name, lead.liga_account_id, cand_id,
        )
        return True

    # Salva ID + valida no partner bot
    lead.liga_account_id = str(cand_id)[:100]
    if saldo_real is not None:
        lead.liga_balance = float(saldo_real)

    try:
        val = await validate_id_via_partner_bot(client, cand_id)
        from datetime import datetime as _dt
        lead.liga_id_partner_response = (val.get("raw") or "")[:4000]
        if val.get("status") == "validated":
            lead.liga_id_status = "validated"
            lead.liga_id_country = (val.get("country") or "")[:50]
            lead.liga_id_balance = val.get("balance")
            lead.liga_id_deposits_sum = val.get("deposits_sum")
            lead.liga_id_turnover = val.get("turnover")
            lead.liga_id_validated_at = _dt.utcnow()
            # Recalcula VIP
            try:
                from liga.automation import _maybe_flag_vip
                _maybe_flag_vip(lead)
            except Exception:
                pass
            logger.info(
                "[passivo] ✓ %s validado: id=%s país=%s saldo=%s deposits=%s",
                lead.display_name, cand_id, val.get("country"),
                val.get("balance"), val.get("deposits_sum"),
            )
        elif val.get("status") == "invalid":
            lead.liga_id_status = "invalid"
            logger.warning("[passivo] ✗ %s ID %s rejeitado pelo partner bot", lead.display_name, cand_id)
        else:
            lead.liga_id_status = "extracted"
            logger.info("[passivo] ~ %s ID %s extraído (sem validação completa)", lead.display_name, cand_id)
    except Exception:
        logger.exception("[passivo] erro validando no partner bot")
        lead.liga_id_status = "extracted"

    return True


def _detect_message_kind(msg) -> tuple[str, int | None]:
    """Identifica tipo da mensagem do Telethon. Retorna (kind, duration_seconds)."""
    if msg is None:
        return "text", None
    if getattr(msg, "voice", None) or getattr(msg, "audio", None):
        # Voice note ou audio file
        try:
            doc = msg.document or msg.audio or msg.voice
            duration = None
            if doc and hasattr(doc, "attributes"):
                for attr in doc.attributes:
                    if hasattr(attr, "duration"):
                        duration = int(attr.duration)
                        break
            return "audio", duration
        except Exception:
            return "audio", None
    if getattr(msg, "photo", None):
        return "image", None
    doc = getattr(msg, "document", None)
    if doc:
        mime = (getattr(doc, "mime_type", "") or "").lower()
        if mime.startswith("image/"):
            return "image", None
        if mime.startswith("audio/") or mime.startswith("video/ogg"):
            return "audio", None
        if mime.startswith("video/"):
            return "video", None
        if "sticker" in mime:
            return "sticker", None
        return "document", None
    if getattr(msg, "sticker", None):
        return "sticker", None
    return "text", None


def store_lead_message(session, lead_id: int, msg, direction: str = "in", classified_as: str = None) -> None:
    """Grava a DM em LeadMessage. Idempotente — não duplica se telegram_msg_id já existe."""
    if msg is None:
        return
    try:
        tg_id = getattr(msg, "id", None)
        if tg_id is not None:
            existing = (
                session.query(LeadMessage)
                .filter(LeadMessage.lead_id == lead_id)
                .filter(LeadMessage.telegram_msg_id == tg_id)
                .filter(LeadMessage.direction == direction)
                .first()
            )
            if existing:
                return  # já gravamos

        kind, duration = _detect_message_kind(msg)
        text = (msg.message or "").strip() if hasattr(msg, "message") else ""
        # Pra mídia sem texto, descreve o tipo (placeholder até Whisper transcrever)
        if kind == "audio" and not text:
            text = f"[áudio {duration or '?'}s — não transcrito]"
        elif kind == "image" and not text:
            text = "[imagem]"
        elif kind == "video" and not text:
            text = "[vídeo]"
        elif kind == "sticker" and not text:
            text = "[sticker]"
        elif kind == "document" and not text:
            text = "[documento]"

        session.add(LeadMessage(
            lead_id=lead_id,
            direction=direction,
            kind=kind,
            content=text[:5000],
            duration_sec=duration,
            telegram_msg_id=tg_id,
            classified_as=classified_as,
        ))
    except Exception:
        logger.debug("[lead_message] erro gravando", exc_info=True)


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

                # Atualiza last_dm_at + grava a msg em LeadMessage
                lead.last_dm_at = datetime.utcnow()
                store_lead_message(session, lead.id, event.message, direction="in")
                session.commit()
                lead_id_local = lead.id
                msg_text_local = (event.message.message or "").strip()
                is_image_msg = bool(getattr(event.message, "photo", None))

            # ═══ FUNIL AUTOMATIZADO (com debounce/buffer) ═══════════════════
            # Em vez de chamar dispatcher imediatamente, joga a mensagem no
            # buffer. O buffer agrupa mensagens consecutivas (debounce ~7s)
            # e só dispara o callback UMA vez quando o lead para de digitar.
            # Isso evita respostas conflitantes quando lead manda 2+ msgs juntas.
            #
            # Imagem é baixada AGORA (event.message só vive durante o handler)
            # e os bytes são passados pro buffer. Quando ele disparar, roda
            # Vision com os bytes acumulados (último).
            _image_bytes_pre = None
            if is_image_msg:
                try:
                    _image_bytes_pre = await event.message.download_media(file=bytes)
                except Exception:
                    logger.exception("[funnel] erro baixando imagem")

            try:
                from liga.funnel.dispatcher import dispatch as _funnel_dispatch, get_active_funnel
                from liga.funnel.buffer import submit as _buffer_submit

                if get_active_funnel() is not None:
                    # Callback que vai rodar quando o buffer disparar
                    async def _process_aggregated(combined_text, has_image, image_analysis, image_bytes):
                        # Roda Vision aqui (depois do debounce, com bytes da
                        # última imagem do batch agregado)
                        if has_image and image_bytes and not image_analysis:
                            try:
                                from ai.providers import analyze_account_screenshot
                                image_analysis = analyze_account_screenshot(image_bytes, lead_id=lead_id_local)
                                logger.info(
                                    "[funnel] vision (buffer): id=%s confianca=%s valido=%s",
                                    (image_analysis or {}).get("id_conta"),
                                    (image_analysis or {}).get("confianca"),
                                    (image_analysis or {}).get("valido"),
                                )
                            except Exception:
                                logger.exception("[funnel] erro Vision no buffer")

                        with SessionLocal() as _s:
                            _lead_for_funnel = _s.query(Lead).get(lead_id_local)
                        if not _lead_for_funnel:
                            return

                        _funnel_result = await _funnel_dispatch(
                            client, _lead_for_funnel, combined_text,
                            is_image=has_image, image_analysis=image_analysis,
                        )
                        if _funnel_result.get("action") == "step_executed":
                            logger.info(
                                "[funnel] lead=%s %s→%s sent=%d (debounced)",
                                _lead_for_funnel.display_name,
                                _funnel_result.get("from"), _funnel_result.get("to"),
                                _funnel_result.get("sent", 0),
                            )
                        else:
                            logger.debug("[funnel] não processou: %s", _funnel_result.get("action"))

                    # Joga no buffer (não bloqueia — só agenda)
                    await _buffer_submit(
                        lead_id=lead_id_local,
                        message_text=msg_text_local,
                        is_image=is_image_msg,
                        image_analysis=None,  # será rodado dentro do callback após debounce
                        image_bytes=_image_bytes_pre,
                        callback=_process_aggregated,
                    )
                    # Funil agora é ASYNC via buffer. Não retornamos
                    # aqui — outros handlers (agente passivo) podem rodar
                    # em paralelo se for útil.
            except Exception:
                logger.exception("[funnel] erro submetendo ao buffer (continuando fluxo normal)")

            # ═══ AGENTE ATIVO (sugestão pra queue) ════════════════════════
            try:
                from liga.agent.suggester import is_agent_active, create_suggestion_for_dm
                if is_agent_active() and msg_text_local and not is_image_msg:
                    create_suggestion_for_dm(
                        lead_id_local, msg_text_local,
                        dm_telegram_id=getattr(event.message, "id", None),
                    )
            except Exception:
                logger.debug("[agent] erro criando sugestão", exc_info=True)

            # Re-abre session pra fluxo legado
            with SessionLocal() as session:
                lead = session.query(Lead).get(lead_id_local)
                if not lead:
                    return

                # --- Roteamento Liga (estado da jornada) ---------------------
                # Em modo PASSIVO (AUTO_REPLY=0, default), os handlers da Liga
                # NÃO rodam — bot apenas cataloga dados, você responde tudo.
                # Pra ativar respostas automáticas, setar AUTO_REPLY=1 no .env.
                liga_state = getattr(lead, "liga_state", None) or "new"
                handler = LIGA_HANDLERS.get(liga_state)
                if handler is not None and _auto_reply_enabled():
                    try:
                        await handler(event, lead, session, client)
                        reply_text_for_intent = (event.message.message or "").strip()
                        deposit_match = detect_deposit_intent(reply_text_for_intent)
                        update_lead_engagement(lead, session, deposit_promise_match=deposit_match, commit=False)
                        session.commit()
                    except Exception:
                        logger.exception("[liga] erro no handler %s", liga_state)
                        session.rollback()
                elif handler is not None:
                    # Modo passivo: SEM resposta, mas atualiza categorização
                    # + extração silenciosa de ID se for screenshot
                    try:
                        # Captura passiva de screenshot (Vision + partner bot)
                        captured = await _passive_image_capture(event, lead, session, client)
                        if captured:
                            logger.debug("[passivo] screenshot processado em tempo real lead=%s", lead.display_name)

                        # Categorização heurística
                        reply_text_for_intent = (event.message.message or "").strip()
                        deposit_match = detect_deposit_intent(reply_text_for_intent)
                        update_lead_engagement(lead, session, deposit_promise_match=deposit_match, commit=False)
                        session.commit()
                    except Exception:
                        logger.debug("[passivo] erro processando", exc_info=True)
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
                    # Marca remarketing_stage como opted_out
                    try:
                        from liga.remarketing_stage import mark_opted_out
                        mark_opted_out(lead)
                    except Exception:
                        pass
                    logger.warning(
                        "[opt_out] lead=%s pediu pra parar (match: %r) — BLOCKED",
                        lead.display_name, opt_out_match,
                    )
                    # NÃO responde, não faz mais nada
                else:
                    # Qualquer resposta marca como REPLIED (status legacy)
                    if lead.status in (LeadStatus.PENDING.value, LeadStatus.CONTACTED.value):
                        lead.status = LeadStatus.REPLIED.value
                    # Avança remarketing_stage pra "replied" (preserva se já é terminal)
                    try:
                        from liga.remarketing_stage import mark_replied
                        mark_replied(lead)
                    except Exception:
                        pass
                    # is_fresh = True (lead respondeu agora)
                    lead.is_fresh = True

                # Categoriza o lead em tempo real — usa a mensagem que chegou agora
                # pra detectar promessa de depósito + recalcula tag de engajamento.
                deposit_match = detect_deposit_intent(reply_text)
                update_lead_engagement(lead, session, deposit_promise_match=deposit_match, commit=False)

                session.commit()
                logger.info("Reply de %s -> %s", lead.display_name, classification)
        except Exception:
            logger.exception("Erro processando reply")

    @client.on(events.NewMessage(outgoing=True))
    async def _on_self_dm(event):
        """Listener pra DMs que VOCÊ envia. Alimenta o agente passivo (Modo 2):
        cada resposta sua é pareada com a última pergunta do lead e registrada
        em AgentLearningExample → vault Obsidian.
        """
        try:
            if not event.is_private:
                return
            # Quem é o lead (o "outro" lado da DM)
            lead_telegram_id = getattr(event.chat_id, "user_id", None) or event.chat_id
            if not lead_telegram_id:
                return

            text = (event.message.message or "").strip()
            if not text:
                return  # ignora mídia pura

            with SessionLocal() as session:
                lead = session.query(Lead).filter_by(telegram_id=lead_telegram_id).one_or_none()
                if not lead:
                    return
                # Grava a msg out (idempotente)
                store_lead_message(session, lead.id, event.message, direction="out")
                session.commit()
                lead_id_local = lead.id

            # Modo passivo: registra par lead_msg → sua_resposta
            try:
                from liga.agent.learning import register_dm_pair
                register_dm_pair(lead_id_local, text)
            except Exception:
                logger.debug("[agent_learning] erro registrando par", exc_info=True)
        except Exception:
            logger.debug("[tracker] erro processando outgoing", exc_info=True)

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
                # Marca remarketing_stage = converted
                try:
                    from liga.remarketing_stage import mark_converted
                    mark_converted(lead)
                except Exception:
                    pass
                session.commit()
                logger.info("Lead %s entrou no grupo!", lead.display_name)
        except Exception:
            logger.exception("Erro processando entrada no grupo")

    logger.info("Listener de respostas ativo.")
