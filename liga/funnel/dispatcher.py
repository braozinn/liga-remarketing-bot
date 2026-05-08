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


async def execute_step(
    client,
    lead: Lead,
    step: FunnelStep,
    config: dict,
    dry_run: bool = False,
) -> dict:
    """Executa um step: envia scripts/mídia + atualiza estado do lead.

    Retorna dict com resultado: {"sent": int, "errors": int, "actions": [...]}
    """
    actions = []

    if dry_run:
        logger.info(
            "[funnel DRY] lead=%s step=%s→%s would send: scripts=%s media=%s",
            lead.display_name, step.source_state, step.target_state,
            step.script_ids_json, step.media_ids_json,
        )
        return {"sent": 0, "errors": 0, "actions": ["dry_run"], "dry_run": True}

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

    # Envia mídia primeiro (bolinhas etc)
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

    # Envia scripts (texto)
    for sid in script_ids:
        try:
            with SessionLocal() as s:
                variant = s.query(ScriptVariant).get(sid)
                if not variant or not variant.text_es:
                    continue
                text = variant.text_es

            # Substitui [nombre] pelo primeiro nome
            first_name = (lead.first_name or "").split()[0] if lead.first_name else ""
            if first_name:
                text = text.replace("[nombre]", first_name)
            else:
                text = text.replace("[nombre]", "").strip()

            try:
                async with client.action(lead.telegram_id, "typing"):
                    await asyncio.sleep(random.uniform(1.5, 3.5))
            except Exception:
                pass

            await client.send_message(lead.telegram_id, text)
            sent += 1
            actions.append(f"script:{sid}")

            # Registra como LeadMessage out
            try:
                with SessionLocal() as s:
                    s.add(LeadMessage(
                        lead_id=lead.id, direction="out", kind="text",
                        content=text[:5000], classified_as=f"funnel_step_{step.id}",
                    ))
                    s.commit()
            except Exception:
                pass

            await asyncio.sleep(random.uniform(
                step.delay_between_min or 1, step.delay_between_max or 5,
            ))
        except Exception:
            logger.exception("[funnel] erro enviando script %s", sid)
            errors += 1

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


async def dispatch(client, lead: Lead, message_text: str, is_image: bool = False) -> dict:
    """Entry point: chamado quando DM nova chega.

    Retorna dict com decisão tomada e ação executada.
    """
    funnel = get_active_funnel()
    if not funnel:
        return {"action": "no_active_funnel"}

    if getattr(lead, "opted_out", False):
        return {"action": "skipped", "reason": "opted_out"}

    config = _parse_config(funnel)

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
        return {
            "action": "escalated", "reason": "off_topic_or_low_confidence",
            "intent": intent, "confidence": confidence,
        }

    # Busca step
    step = find_step(funnel.id, state, intent)
    if not step:
        return {
            "action": "no_step_match", "state": state, "intent": intent,
            "confidence": confidence,
        }

    # Executa step
    result = await execute_step(client, lead, step, config, dry_run=funnel.is_dry_run)
    return {
        "action": "step_executed",
        "step_id": step.id,
        "from": step.source_state,
        "to": step.target_state,
        "intent": intent,
        "confidence": confidence,
        **result,
    }
