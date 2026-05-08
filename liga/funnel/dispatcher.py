"""Dispatcher do funil — recebe DM, classifica, executa step da state machine.

Chamado por userbot/tracker.py quando AUTO_REPLY_FUNNEL=1 e há funnel ativo.
Idempotente: se lead em estado X recebe DM mas não tem step matching, retorna
sem ação (escala pra humano respondendo manualmente).

Anti-detecção:
- Typing action durante delays
- Delays aleatórios entre msgs
- Janela horária (default 08-23h BA)
- Cap diário (default 150 respostas/dia)
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import random
from datetime import datetime, timedelta
from typing import Optional

from db import SessionLocal
from db.models import (
    Funnel, FunnelStep, Lead, ScriptVariant, ScriptMedia,
    Setting, LeadMessage,
)

logger = logging.getLogger(__name__)


def get_active_funnel():
    """Retorna o funnel ativo (is_active=True), ou None."""
    with SessionLocal() as s:
        return s.query(Funnel).filter(Funnel.is_active.is_(True)).first()


def _parse_config(funnel: Funnel) -> dict:
    """Parseia config_json com defaults razoáveis."""
    defaults = {
        "delay_min": 8,
        "delay_max": 20,
        "delay_between_min": 1,
        "delay_between_max": 5,
        "active_window_start": 8,   # hora BA
        "active_window_end": 23,
        "daily_cap": 150,
        "min_confidence": 0.7,
        "min_deposit_usd": 20,
        "group_link": os.getenv("PRIVATE_GROUP_INVITE_LINK", ""),
    }
    if not funnel.config_json:
        return defaults
    try:
        loaded = json.loads(funnel.config_json)
        defaults.update(loaded)
    except Exception:
        logger.warning("[funnel] config_json inválido, usando defaults")
    return defaults


def _is_in_active_window(config: dict) -> bool:
    """Checa se hora atual em BA está na janela ativa."""
    try:
        from zoneinfo import ZoneInfo
        ba_tz = ZoneInfo("America/Argentina/Buenos_Aires")
        now_h = datetime.now(ba_tz).hour
        return config["active_window_start"] <= now_h < config["active_window_end"]
    except Exception:
        return True  # se falhar, assume sempre ativo


def _check_daily_cap(config: dict) -> tuple[bool, int]:
    """Retorna (within_cap, current_count)."""
    cap = config.get("daily_cap", 150)
    try:
        with SessionLocal() as s:
            today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
            count_row = s.query(Setting).filter_by(key="funnel_daily_count").one_or_none()
            count_date_row = s.query(Setting).filter_by(key="funnel_daily_count_date").one_or_none()
            today_str = today_start.strftime("%Y-%m-%d")
            if count_date_row and count_date_row.value == today_str and count_row:
                current = int(count_row.value or "0")
            else:
                current = 0
        return current < cap, current
    except Exception:
        return True, 0


def _increment_daily_count():
    try:
        with SessionLocal() as s:
            today_str = datetime.utcnow().strftime("%Y-%m-%d")
            count_row = s.query(Setting).filter_by(key="funnel_daily_count").one_or_none()
            count_date_row = s.query(Setting).filter_by(key="funnel_daily_count_date").one_or_none()

            if count_date_row and count_date_row.value == today_str:
                if count_row:
                    count_row.value = str(int(count_row.value or "0") + 1)
                else:
                    s.add(Setting(key="funnel_daily_count", value="1"))
            else:
                if count_row:
                    count_row.value = "1"
                else:
                    s.add(Setting(key="funnel_daily_count", value="1"))
                if count_date_row:
                    count_date_row.value = today_str
                else:
                    s.add(Setting(key="funnel_daily_count_date", value=today_str))
            s.commit()
    except Exception:
        logger.exception("[funnel] erro incrementando daily count")


def find_step(funnel_id: int, source_state: str, intent: str) -> Optional[FunnelStep]:
    """Busca FunnelStep matching pra (estado, intent)."""
    with SessionLocal() as s:
        return (
            s.query(FunnelStep)
            .filter(FunnelStep.funnel_id == funnel_id)
            .filter(FunnelStep.source_state == source_state)
            .filter(FunnelStep.trigger_intent == intent)
            .order_by(FunnelStep.order_index)
            .first()
        )


async def _validate_lead_id(client, lead: Lead, message_text: str = "", is_image: bool = False) -> tuple[bool, str, Optional[str]]:
    """Valida ID do lead via @QuotexPartnerBot.

    Tenta extrair ID:
    - Se is_image: roda Vision na imagem do evento (não temos aqui — usa lead.liga_account_id)
    - Se text: regex de 7-9 dígitos no message_text

    Returns (is_valid, raw_response, extracted_id).
    """
    import re as _re
    from userbot.leads import _looks_like_valid_id, validate_id_via_partner_bot

    extracted_id = None

    # Tenta extrair do texto da mensagem atual
    if message_text:
        # Regex: número solto de 7-9 dígitos
        matches = _re.findall(r"\b(\d{7,9})\b", message_text)
        for cand in matches:
            if _looks_like_valid_id(cand):
                extracted_id = cand
                break

    # Fallback: usa o que tá no lead
    if not extracted_id and lead.liga_account_id:
        extracted_id = lead.liga_account_id

    if not extracted_id:
        return False, "Nenhum ID extraído da mensagem", None

    try:
        val = await validate_id_via_partner_bot(client, extracted_id)
        is_valid = val.get("status") == "validated"
        raw = (val.get("raw") or "")[:500]
        # Salva no lead se valid
        if is_valid:
            with SessionLocal() as s:
                ld = s.query(Lead).get(lead.id)
                if ld:
                    ld.liga_account_id = extracted_id
                    ld.liga_id_status = "validated"
                    ld.liga_id_country = (val.get("country") or "")[:50]
                    ld.liga_id_balance = val.get("balance")
                    ld.liga_id_deposits_sum = val.get("deposits_sum")
                    ld.liga_id_validated_at = datetime.utcnow()
                    s.commit()
        return is_valid, raw, extracted_id
    except Exception as e:
        logger.exception("[funnel] erro validate_id")
        return False, f"erro: {e}", extracted_id


async def _validate_lead_deposit(client, lead: Lead, min_usd: float = 20.0) -> tuple[bool, float, str]:
    """Valida saldo/depósito do lead via @QuotexPartnerBot.

    Re-consulta o partner bot pra pegar saldo ATUAL (não do cache do banco).
    Returns (is_ok, current_balance, raw_response).
    """
    from userbot.leads import validate_id_via_partner_bot

    if not lead.liga_account_id:
        return False, 0.0, "Lead sem liga_account_id"

    try:
        val = await validate_id_via_partner_bot(client, lead.liga_account_id)
        balance = float(val.get("balance") or 0.0)
        deposits = float(val.get("deposits_sum") or 0.0)
        # Considera ok se: saldo atual >= min OU deposits_sum >= min
        is_ok = balance >= min_usd or deposits >= min_usd

        # Atualiza saldo no banco
        with SessionLocal() as s:
            ld = s.query(Lead).get(lead.id)
            if ld:
                ld.liga_id_balance = balance
                ld.liga_id_deposits_sum = deposits
                ld.last_revalidated_at = datetime.utcnow()
                s.commit()

        return is_ok, balance, (val.get("raw") or "")[:500]
    except Exception as e:
        logger.exception("[funnel] erro validate_deposit")
        return False, 0.0, f"erro: {e}"


async def execute_step(
    client,
    lead: Lead,
    step: FunnelStep,
    config: dict,
    dry_run: bool = False,
    message_text: str = "",
    is_image: bool = False,
) -> dict:
    """Executa um step: envia scripts/mídia + atualiza estado do lead.

    Antes de enviar, executa extra_action (validate_id/validate_deposit).
    Se validação falhar, NOTIFICA ADMIN e NÃO avança lead.

    Retorna dict com resultado: {"sent": int, "errors": int, "actions": [...]}
    """
    actions = []

    if dry_run:
        logger.info(
            "[funnel DRY] lead=%s step=%s→%s would send: scripts=%s media=%s extra=%s",
            lead.display_name, step.source_state, step.target_state,
            step.script_ids_json, step.media_ids_json, step.extra_action,
        )
        return {"sent": 0, "errors": 0, "actions": ["dry_run"], "dry_run": True}

    # ═══ EXTRA_ACTION antes de mandar mensagens ═══════════════════════════
    extra_action = (step.extra_action or "").strip()

    # ─── BYPASS: se lead for o test_mode_username, MOCKA sucesso de validação ─
    # Permite testar fluxo completo sem precisar de conta Quotex real, ID
    # válido ou depósito real. O bot finge que validou OK e avança o estado
    # normalmente. Em produção (lead != test user), validação é real.
    test_user_cfg = (config.get("test_mode_username") or "").strip().lstrip("@").lower()
    lead_username = (lead.username or "").lstrip("@").lower()
    is_test_lead = bool(test_user_cfg and lead_username == test_user_cfg)

    if extra_action == "validate_id" and is_test_lead:
        logger.info(
            "[funnel] lead=%s é TEST_USERNAME — pulando validação real de ID (mock OK)",
            lead.display_name,
        )
        actions.append("validate_id_mocked:test_mode")
        # Salva ID fake pro lead pra rastreabilidade
        try:
            with SessionLocal() as s:
                _l = s.query(Lead).get(lead.id)
                if _l and not _l.quotex_id:
                    _l.quotex_id = "TEST_MOCK_99999999"
                    s.commit()
        except Exception:
            pass
    elif extra_action == "validate_deposit" and is_test_lead:
        logger.info(
            "[funnel] lead=%s é TEST_USERNAME — pulando validação real de DEPÓSITO (mock OK)",
            lead.display_name,
        )
        actions.append("validate_deposit_mocked:test_mode")
    elif extra_action == "validate_id":
        is_valid, raw, extracted = await _validate_lead_id(
            client, lead, message_text=message_text, is_image=is_image,
        )
        if not is_valid:
            try:
                from liga.notifications import notify_id_invalid
                await notify_id_invalid(client, lead, extracted or "(não extraído)", raw)
            except Exception:
                logger.exception("[funnel] erro notificando id_invalid")
            logger.info(
                "[funnel] lead=%s ID INVÁLIDO (%s) — admin notificado, lead NÃO avança",
                lead.display_name, extracted,
            )
            return {
                "sent": 0, "errors": 0,
                "actions": ["validate_id_failed", "admin_notified"],
                "blocked": True, "did_not_advance": True,
                "extracted_id": extracted, "partner_raw": raw,
            }
        actions.append(f"validate_id_ok:{extracted}")

    elif extra_action == "validate_deposit":
        min_usd = float(config.get("min_deposit_usd", 20.0))
        is_ok, balance, raw = await _validate_lead_deposit(client, lead, min_usd=min_usd)
        if not is_ok:
            try:
                from liga.notifications import notify_deposit_unconfirmed
                await notify_deposit_unconfirmed(client, lead, current_balance=balance, min_required=min_usd)
            except Exception:
                logger.exception("[funnel] erro notificando deposit_unconfirmed")
            logger.info(
                "[funnel] lead=%s DEPÓSITO NÃO CONFIRMADO (saldo=$%.2f < %s) — admin notificado",
                lead.display_name, balance, min_usd,
            )
            return {
                "sent": 0, "errors": 0,
                "actions": ["validate_deposit_failed", "admin_notified"],
                "blocked": True, "did_not_advance": True,
                "current_balance": balance, "min_required": min_usd,
            }
        actions.append(f"validate_deposit_ok:${balance:.2f}")

    # Delay inicial (com typing)
    initial_delay = random.uniform(step.delay_min or 8, step.delay_max or 20)
    try:
        async with client.action(lead.telegram_id, "typing"):
            await asyncio.sleep(initial_delay)
    except Exception:
        await asyncio.sleep(initial_delay)
    actions.append(f"initial_delay:{initial_delay:.1f}s")

    # Carrega scripts e mídia em ordem
    script_ids = []
    media_ids = []
    try:
        if step.script_ids_json:
            script_ids = json.loads(step.script_ids_json) or []
    except Exception:
        pass
    try:
        if step.media_ids_json:
            media_ids = json.loads(step.media_ids_json) or []
    except Exception:
        pass

    sent = 0
    errors = 0

    async def _send_media_block():
        """Envia toda a lista de mídia."""
        nonlocal sent, errors
        for mid in media_ids:
            try:
                with SessionLocal() as s:
                    media = s.query(ScriptMedia).get(mid)
                    if not media:
                        continue
                    from pathlib import Path as _P
                    media_dir = _P(__file__).resolve().parent.parent.parent / "media"
                    media_path = media_dir / media.filename
                    if not media_path.exists():
                        logger.warning("[funnel] mídia %s não existe: %s", mid, media_path)
                        errors += 1
                        continue
                    video_note = bool(getattr(media, "video_note", False))

                await client.send_file(
                    lead.telegram_id, str(media_path),
                    video_note=video_note,
                    caption=(media.caption or None) if not video_note else None,
                )
                sent += 1
                actions.append(f"media:{mid}")
                await asyncio.sleep(random.uniform(
                    step.delay_between_min or 1, step.delay_between_max or 5,
                ))
            except Exception:
                logger.exception("[funnel] erro enviando mídia %s", mid)
                errors += 1

    media_position = (step.media_position or "before").lower()

    # Se 'replace' e tem mídia: ignora scripts, manda só mídia
    if media_position == "replace" and media_ids:
        await _send_media_block()
        actions.append("media_position:replace")
        # Atualiza estado e retorna
        try:
            with SessionLocal() as s:
                ld = s.query(Lead).get(lead.id)
                if ld:
                    ld.liga_state = step.target_state
                    ld.last_bot_action = f"funnel_step_{step.id}:{step.source_state}->{step.target_state}"
                    s.commit()
            actions.append(f"state_change:{step.source_state}->{step.target_state}")
        except Exception:
            logger.exception("[funnel] erro atualizando estado do lead")
        if sent > 0:
            _increment_daily_count()
        return {"sent": sent, "errors": errors, "actions": actions, "dry_run": False}

    # Mídia BEFORE — envia antes dos textos
    if media_position == "before" and media_ids:
        await _send_media_block()

    # ═══ Helper: parser de link de mensagem do Telegram ═══════════════════
    # Detecta se um bloco é APENAS um link de mensagem do Telegram tipo:
    #   https://t.me/c/2284832749/42      (grupo/canal privado)
    #   https://t.me/usergroup/42         (grupo/canal público)
    # Se sim, em vez de enviar como texto, faz FORWARD da msg original com
    # drop_author=True (sem mostrar "Forwarded from"). Mantém preview rico,
    # link disfarçado, mídia, tudo. Bot vira "copiador" da mensagem.
    import re as _re
    # Aceita 4 formatos:
    #   https://t.me/c/<priv_id>/<msg_id>             (grupo/canal privado)
    #   https://t.me/c/<priv_id>/<topic_id>/<msg_id>  (grupo privado com topics/fórum)
    #   https://t.me/<username>/<msg_id>              (público)
    #   https://t.me/<username>/<topic_id>/<msg_id>   (público com topics)
    # O topic_id é descartado — Telethon forward_messages só precisa do msg_id final.
    _TG_MSG_LINK_RE = _re.compile(
        r"^https?://t\.me/"
        r"(?:c/(\d+)|([a-zA-Z][\w]{3,31}))"          # private_id OU username
        r"(?:/\d+)?"                                  # topic_id opcional (descartado)
        r"/(\d+)"                                     # msg_id (final)
        r"/?\s*$",
        _re.IGNORECASE,
    )

    def _parse_tg_message_link(text: str):
        """Retorna (chat_ref, msg_id) se o texto é APENAS um link de msg.
        chat_ref é int (grupos privados, com prefixo -100) OU str (username).
        Retorna None se não é link.
        """
        cleaned = (text or "").strip()
        m = _TG_MSG_LINK_RE.match(cleaned)
        if not m:
            if "t.me/" in cleaned and len(cleaned) < 200:
                logger.debug(
                    "[funnel] bloco parecia link t.me mas regex não matchou: %r",
                    cleaned,
                )
            return None
        private_id, public_user, msg_id_s = m.groups()
        try:
            msg_id = int(msg_id_s)
        except (TypeError, ValueError):
            return None
        if private_id:
            try:
                chat_ref = int(f"-100{private_id}")
            except (TypeError, ValueError):
                return None
        else:
            chat_ref = public_user
        logger.info(
            "[funnel] link de msg detectado: chat=%s msg=%s (origem=%r)",
            chat_ref, msg_id, cleaned,
        )
        return chat_ref, msg_id

    async def _send_block(block_text: str) -> bool:
        """Envia 1 bloco — usa FORWARD se for link de msg, senão send_message.
        Retorna True se enviou, False se erro irrecuperável.
        """
        link = _parse_tg_message_link(block_text)
        if link:
            chat_ref, msg_id = link
            try:
                await client.forward_messages(
                    entity=lead.telegram_id,
                    messages=msg_id,
                    from_peer=chat_ref,
                    drop_author=True,  # ← sem "Forwarded from"
                )
                logger.info(
                    "[funnel] FORWARD msg %s/%s pro lead=%s (drop_author)",
                    chat_ref, msg_id, lead.display_name,
                )
                return True
            except Exception as e:
                logger.warning(
                    "[funnel] forward falhou (chat=%s msg=%s): %s — caindo pra send_message texto",
                    chat_ref, msg_id, str(e)[:200],
                )
                # Fallback: manda o link como texto cru (lead vai ver o link)
        # Envio normal (texto com markdown + fallback)
        try:
            await client.send_message(lead.telegram_id, block_text, parse_mode="md", link_preview=True)
            return True
        except Exception as md_err:
            logger.warning(
                "[funnel] markdown falhou — fallback texto puro: %s",
                str(md_err)[:100],
            )
            try:
                await client.send_message(lead.telegram_id, block_text)
                return True
            except Exception:
                logger.exception("[funnel] envio falhou totalmente")
                return False

    # Envia scripts (texto) — cada script é splitado por linha em branco
    # em mensagens separadas (parece mais humano)
    for sid in script_ids:
        try:
            with SessionLocal() as s:
                variant = s.query(ScriptVariant).get(sid)
                if not variant or not variant.text_es:
                    continue
                full_text = variant.text_es

            # Substitui [nombre] pelo primeiro nome
            first_name = (lead.first_name or "").split()[0] if lead.first_name else ""
            if first_name:
                full_text = full_text.replace("[nombre]", first_name)
            else:
                full_text = full_text.replace("[nombre]", "").strip()

            # Split por linha em branco — cada bloco vira mensagem separada
            blocks = [b.strip() for b in _re.split(r"\n\s*\n+", full_text) if b.strip()]
            if not blocks:
                continue

            for block_idx, block in enumerate(blocks):
                # Detecta se é forward antes de mostrar typing — pra link
                # de mensagem mostra menos typing (parece mais natural)
                is_forward = _parse_tg_message_link(block) is not None

                # Typing antes de cada bloco
                try:
                    async with client.action(lead.telegram_id, "typing"):
                        await asyncio.sleep(random.uniform(
                            0.5 if is_forward else 1.5,
                            1.5 if is_forward else 3.5,
                        ))
                except Exception:
                    pass

                # Envia (helper decide entre forward ou texto)
                ok = await _send_block(block)
                if not ok:
                    errors += 1
                    continue

                # Registra como LeadMessage out
                try:
                    with SessionLocal() as s:
                        s.add(LeadMessage(
                            lead_id=lead.id, direction="out",
                            kind="forward" if is_forward else "text",
                            content=block[:5000],
                            classified_as=f"funnel_step_{step.id}",
                        ))
                        s.commit()
                except Exception:
                    pass

                # Delay entre blocos (não depois do último)
                if block_idx < len(blocks) - 1:
                    await asyncio.sleep(random.uniform(
                        step.delay_between_min or 1, step.delay_between_max or 5,
                    ))

            sent += len(blocks)
            actions.append(f"script:{sid}({len(blocks)} blocks)")

            # Delay maior entre scripts diferentes
            if sid != script_ids[-1]:
                await asyncio.sleep(random.uniform(
                    step.delay_between_min or 1, step.delay_between_max or 5,
                ))
        except Exception:
            logger.exception("[funnel] erro enviando script %s", sid)
            errors += 1

    # Mídia AFTER — envia depois dos textos
    if media_position == "after" and media_ids:
        await _send_media_block()

    # Atualiza estado do lead
    try:
        with SessionLocal() as s:
            ld = s.query(Lead).get(lead.id)
            if ld:
                ld.liga_state = step.target_state
                ld.last_bot_action = f"funnel_step_{step.id}:{step.source_state}->{step.target_state}"
                s.commit()
        actions.append(f"state_change:{step.source_state}->{step.target_state}")
    except Exception:
        logger.exception("[funnel] erro atualizando estado do lead")

    if sent > 0:
        _increment_daily_count()

    return {"sent": sent, "errors": errors, "actions": actions, "dry_run": False}


async def dispatch(
    client, lead: Lead, message_text: str,
    is_image: bool = False,
    force_funnel_id: Optional[int] = None,
    force_send: bool = False,
) -> dict:
    """Entry point: chamado quando DM nova chega.

    Args:
        force_funnel_id: ignora is_active e usa esse funil específico (pra TESTE)
        force_send: ignora dry_run e envia de verdade (pra TESTE)

    Retorna dict com decisão tomada e ação executada.
    """
    if force_funnel_id is not None:
        with SessionLocal() as s:
            funnel = s.query(Funnel).get(force_funnel_id)
        if not funnel:
            return {"action": "funnel_not_found", "id": force_funnel_id}
    else:
        funnel = get_active_funnel()
        if not funnel:
            return {"action": "no_active_funnel"}

    if getattr(lead, "opted_out", False):
        return {"action": "skipped", "reason": "opted_out"}

    config = _parse_config(funnel)

    # ═══ MODO TESTE REAL ═══════════════════════════════════════════════════
    # Se test_mode_username está setado no config, SÓ processa DMs desse user.
    # Permite ativar o funil pra valer mas restrito a 1 conta (ex: @braozin)
    # pra testar fluxo completo em tempo real sem afetar leads reais.
    test_user = (config.get("test_mode_username") or "").strip().lstrip("@").lower()
    if test_user and not force_send:  # force_send pula esse filtro (botão Testar)
        lead_username = (lead.username or "").lstrip("@").lower()
        if lead_username != test_user:
            logger.debug(
                "[funnel] modo teste ativo (only=@%s) — ignorando lead @%s",
                test_user, lead_username,
            )
            return {
                "action": "skipped_test_mode",
                "reason": f"funil em modo teste, só responde @{test_user}",
                "test_mode_username": test_user,
                "lead_username": lead_username,
            }

    # Janela horária
    if not _is_in_active_window(config):
        return {"action": "outside_window", "reason": "fora da janela ativa"}

    # Cap diário
    within_cap, current = _check_daily_cap(config)
    if not within_cap:
        return {"action": "daily_cap_hit", "current": current}

    # Histórico recente pra context
    history = []
    try:
        with SessionLocal() as s:
            recent = (
                s.query(LeadMessage)
                .filter(LeadMessage.lead_id == lead.id)
                .order_by(LeadMessage.created_at.desc())
                .limit(5).all()
            )
            history = [
                {"direction": m.direction, "content": m.content or ""}
                for m in reversed(recent)
            ]
    except Exception:
        pass

    # Classifica intent
    from .classifier import classify_intent
    state = lead.liga_state or "new"
    cls = classify_intent(message_text or "", state, history, is_image=is_image)
    intent = cls["intent"]
    confidence = cls.get("confidence", 0.0)

    if intent == "off_topic" or confidence < config["min_confidence"]:
        logger.info(
            "[funnel] lead=%s msg=%r → SKIP (intent=%s conf=%.2f rejected=%s)",
            lead.display_name, (message_text or "")[:60], intent, confidence,
            cls.get("rejected_intent"),
        )
        return {
            "action": "escalated", "reason": "off_topic_or_low_confidence",
            "intent": intent, "confidence": confidence,
            "rejected_intent": cls.get("rejected_intent"),
        }

    # ═══ DEFESA: lead com histórico longo NÃO É primeiro contato ═══
    # Mesmo se o classifier disse 'quer_entrar_vip', se o lead já trocou
    # várias mensagens com você, NÃO é um primeiro contato real — provavelmente
    # tá perguntando algo já no contexto. Funil de aquisição NÃO deve disparar.
    #
    # EXCEÇÕES (defesa NÃO aplica):
    # - force_send=True (botão "Testar 1 etapa" do painel)
    # - lead = test_mode_username ativo (modo teste real — queremos testar mesmo
    #   com histórico, senão impossível testar com sua conta secundária real)
    _skip_first_contact_defense = (
        force_send
        or (test_user and (lead.username or "").lstrip("@").lower() == test_user)
    )
    if intent == "quer_entrar_vip" and state == "new" and not _skip_first_contact_defense:
        try:
            with SessionLocal() as _s:
                # Conta msgs `out` (suas) anteriores — se tem 2+, lead já tá em conversa
                from sqlalchemy import func as _func
                out_count = (
                    _s.query(_func.count(LeadMessage.id))
                    .filter(LeadMessage.lead_id == lead.id)
                    .filter(LeadMessage.direction == "out")
                    .scalar() or 0
                )
                # Também checa mensagens IN totais (não só recentes)
                total_in = (
                    _s.query(_func.count(LeadMessage.id))
                    .filter(LeadMessage.lead_id == lead.id)
                    .filter(LeadMessage.direction == "in")
                    .scalar() or 0
                )
            if out_count >= 2 or total_in >= 5:
                logger.info(
                    "[funnel] lead=%s INTENT=quer_entrar_vip mas tem histórico (out=%d, in=%d) — NÃO É primeiro contato, SKIP",
                    lead.display_name, out_count, total_in,
                )
                return {
                    "action": "skipped_not_first_contact",
                    "reason": f"lead já tem {out_count} respostas suas e {total_in} msgs — não é primeiro contato",
                    "intent": intent,
                    "confidence": confidence,
                    "out_msgs": out_count,
                    "in_msgs": total_in,
                }
        except Exception:
            logger.debug("[funnel] erro checando histórico", exc_info=True)
    elif _skip_first_contact_defense and intent == "quer_entrar_vip" and state == "new":
        logger.info(
            "[funnel] lead=%s defesa de primeiro contato BYPASSADA (force_send=%s, test_mode=%s)",
            lead.display_name, force_send, bool(test_user),
        )

    # Busca step
    step = find_step(funnel.id, state, intent)
    if not step:
        logger.info(
            "[funnel] lead=%s estado=%s intent=%s — SEM STEP MATCHING (escalado pra humano)",
            lead.display_name, state, intent,
        )
        return {
            "action": "no_step_match", "state": state, "intent": intent,
            "confidence": confidence,
        }

    # Executa step (force_send ignora dry_run pra teste)
    effective_dry_run = bool(funnel.is_dry_run) and not force_send
    result = await execute_step(
        client, lead, step, config,
        dry_run=effective_dry_run,
        message_text=message_text,
        is_image=is_image,
    )
    return {
        "action": "step_executed",
        "step_id": step.id,
        "from": step.source_state,
        "to": step.target_state,
        "intent": intent,
        "confidence": confidence,
        **result,
    }
