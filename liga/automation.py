"""Automações periódicas do bot — backup, digest, re-validação, follow-ups, etc.

Cada função aqui é chamada pelo scheduler (liga/scheduler.py) em horários BA.
"""
from __future__ import annotations

import asyncio
import io
import json
import logging
import os
import zipfile
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Optional

try:
    from zoneinfo import ZoneInfo
    BA_TZ = ZoneInfo("America/Argentina/Buenos_Aires")
except ImportError:  # pragma: no cover
    BA_TZ = None

from sqlalchemy import func

from db import SessionLocal
from db.models import (
    AIUsage, BalanceSnapshot, DailyVolume, FollowUpRule, ImageCache,
    Lead, LeadMessage, LeadStatus, OperationProof, Script, Send, SendStatus, Setting,
)

logger = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parent.parent


# ---------------------------------------------------------------------------
# 1) Backup automático — zipa data.db + media/ e envia pro admin
# ---------------------------------------------------------------------------
async def task_daily_backup(client) -> dict:
    """Cria zip do banco + mídia e envia DM pro ADMIN_TELEGRAM_ID."""
    admin = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
    if not admin:
        logger.warning("[backup] ADMIN_TELEGRAM_ID não setado — pulando")
        return {"ok": False, "reason": "no_admin"}

    db_path = ROOT / "data.db"
    if not db_path.exists():
        logger.warning("[backup] data.db não encontrado")
        return {"ok": False, "reason": "no_db"}

    try:
        ts = datetime.utcnow().strftime("%Y-%m-%d_%H%M%S")
        out_path = ROOT / f"backup_{ts}.zip"

        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            zf.write(db_path, arcname="data.db")
            media = ROOT / "media"
            if media.exists():
                for fp in media.rglob("*"):
                    if fp.is_file():
                        zf.write(fp, arcname=str(fp.relative_to(ROOT)))
            cfg = ROOT / "config.yaml"
            if cfg.exists():
                zf.write(cfg, arcname="config.yaml")

        size_mb = out_path.stat().st_size / (1024 * 1024)
        target = int(admin) if admin.lstrip("-").isdigit() else admin
        await client.send_file(
            target, str(out_path),
            caption=(
                f"💾 *Backup automático*\n"
                f"`{ts}` · {size_mb:.1f} MB\n"
                f"data.db + media/proofs + config.yaml"
            ),
            parse_mode="markdown",
        )
        # Limpa o zip local depois de enviar (mantém só o do Telegram)
        out_path.unlink(missing_ok=True)
        logger.info("[backup] enviado ao admin (%.1f MB)", size_mb)
        return {"ok": True, "size_mb": size_mb}
    except Exception as e:
        logger.exception("[backup] falhou")
        return {"ok": False, "error": str(e)}


# ---------------------------------------------------------------------------
# 6) Daily digest — resumo de bom dia pro admin
# ---------------------------------------------------------------------------
async def task_tournament_backup(client) -> dict:
    """Backup extra a cada 6h durante torneio.

    Só roda se LIGA_TOURNAMENT_MODE=auto e datas batem, ou MODE=always.
    Reusa task_daily_backup pra fazer o trabalho real.
    """
    try:
        from .notifications import is_tournament_active
        if not is_tournament_active():
            logger.debug("[backup_torneio] fora do período do torneio — skip")
            return {"ok": False, "reason": "not_active"}
    except Exception:
        return {"ok": False, "reason": "import_error"}
    logger.info("[backup_torneio] rodando backup extra (modo torneio ativo)")
    return await task_daily_backup(client)


async def task_daily_digest(client, tournament_only: bool = False) -> dict:
    """08h00 BA — resumo do dia anterior pro admin.

    Se tournament_only=True, só roda durante a vigência do torneio
    (usado pelo segundo digest 12h durante a Liga).
    """
    admin = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
    if not admin:
        return {"ok": False}

    if tournament_only:
        try:
            from .notifications import is_tournament_active
            if not is_tournament_active():
                return {"ok": False, "reason": "not_in_tournament"}
        except Exception:
            return {"ok": False, "reason": "import_error"}

    yesterday = (datetime.utcnow() - timedelta(days=1)).date()
    today = datetime.utcnow().date()
    decay_cutoff = datetime.utcnow() - timedelta(days=4)  # leads que esfriaram nos últimos 4d

    with SessionLocal() as s:
        new_leads = s.query(Lead).filter(
            Lead.created_at >= yesterday,
            Lead.created_at < today + timedelta(days=1),
        ).count()
        deposits_promised = s.query(Lead).filter(
            Lead.engagement_tag == "deposit_promised",
        ).count()

        # Stage counts pra ações disponíveis
        eligible_r1 = s.query(Lead).filter(
            Lead.remarketing_stage == "untouched",
            Lead.is_fresh.is_(False),
            Lead.opted_out.is_(False),
            Lead.in_private_group.is_(False),
        ).count()
        eligible_r2 = s.query(Lead).filter(
            Lead.remarketing_stage == "r1_cold",
            Lead.is_fresh.is_(False),
            Lead.opted_out.is_(False),
            Lead.in_private_group.is_(False),
        ).count()
        eligible_r3 = s.query(Lead).filter(
            Lead.remarketing_stage == "r2_cold",
            Lead.is_fresh.is_(False),
            Lead.opted_out.is_(False),
            Lead.in_private_group.is_(False),
        ).count()
        in_cooldown = s.query(Lead).filter(
            Lead.remarketing_stage.in_([
                "r1_sent_cooldown", "r2_sent_cooldown", "r3_sent_cooldown",
            ])
        ).count()

        # Decay: leads que estavam em estado "quente" mas pararam de responder
        # (deposit_promised há > 3 dias sem nova msg)
        decaying = (
            s.query(Lead)
            .filter(Lead.engagement_tag == "deposit_promised")
            .filter(Lead.last_dm_at < decay_cutoff)
            .filter(Lead.opted_out.is_(False))
            .filter(Lead.in_private_group.is_(False))
            .order_by(Lead.last_dm_at.asc())
            .limit(5)
            .all()
        )

        # Perguntas sem resposta: lead mandou msg com '?' nas últimas 12h e
        # você ainda não respondeu (last LeadMessage in mais novo que out)
        from sqlalchemy import and_, or_
        questions_pending = (
            s.query(LeadMessage, Lead)
            .join(Lead, Lead.id == LeadMessage.lead_id)
            .filter(LeadMessage.direction == "in")
            .filter(LeadMessage.content.like("%?%"))
            .filter(LeadMessage.created_at >= datetime.utcnow() - timedelta(hours=24))
            .filter(Lead.opted_out.is_(False))
            .filter(Lead.in_private_group.is_(False))
            .order_by(LeadMessage.created_at.desc())
            .limit(20)
            .all()
        )
        # Filtra os que NÃO tiveram out depois (você não respondeu)
        questions_unanswered = []
        for msg, lead in questions_pending:
            last_out = (
                s.query(LeadMessage)
                .filter(LeadMessage.lead_id == lead.id)
                .filter(LeadMessage.direction == "out")
                .filter(LeadMessage.created_at > msg.created_at)
                .first()
            )
            if not last_out:
                questions_unanswered.append((lead, msg))
            if len(questions_unanswered) >= 5:
                break
        mismatches = s.query(OperationProof).filter(
            OperationProof.validated.is_(False),
            OperationProof.raw_ai_response.like("%id_mismatch%"),
        ).count()
        review_pending = s.query(OperationProof).filter(
            OperationProof.validated.is_(False),
            OperationProof.raw_ai_response.like("%low_confidence%"),
        ).count()
        ids_review = s.query(Lead).filter(
            Lead.liga_id_status.in_(["needs_review", "invalid", "extracted"]),
        ).count()
        vips = s.query(Lead).filter(Lead.is_vip_potential.is_(True)).count()
        rewarm = s.query(Lead).filter(Lead.rewarm_candidate.is_(True)).count()

        # Top do dia anterior por volume
        top_yesterday = (
            s.query(Lead, func.sum(DailyVolume.volume_usd).label("vol"))
            .join(DailyVolume, DailyVolume.lead_id == Lead.id)
            .filter(DailyVolume.date == yesterday.strftime("%Y-%m-%d"))
            .group_by(Lead.id)
            .order_by(func.sum(DailyVolume.volume_usd).desc())
            .limit(1)
            .first()
        )

    lines = [
        "☀️ *Bom dia*",
        f"_{today.strftime('%d/%m/%Y')}_",
        "",
        f"• {new_leads} novos leads ontem",
        f"• 💰 {deposits_promised} prometeram depositar",
        f"• 💎 {vips} VIPs em potencial",
        f"• 🔥 {rewarm} candidatos a re-aquecer (perderam saldo)",
        "",
        "*🎯 Pra trabalhar hoje:*",
        f"• 🆕 {eligible_r1} elegíveis pra R1 (1º contato)",
        f"• ❄️ {eligible_r2} elegíveis pra R2 (reativação)",
        f"• ❄️ {eligible_r3} elegíveis pra R3 (última chance)",
        f"• ⏳ {in_cooldown} em cooldown (aguardando)",
    ]
    if mismatches:
        lines.append(f"• ⚠ {mismatches} prints com ID divergente")
    if review_pending:
        lines.append(f"• 👁 {review_pending} prints aguardando revisão")
    if ids_review:
        lines.append(f"• 🔎 {ids_review} IDs precisam revisão manual")
    if top_yesterday and top_yesterday[1]:
        nome = top_yesterday[0].first_name or top_yesterday[0].display_name
        lines.append(f"\n🏆 *Top de ontem*: {nome} — ${float(top_yesterday[1]):,.2f}")

    # Decay alert — leads "quentes" esfriando
    if decaying:
        lines.append("\n❄️ *Esfriando* (prometeram, sumiram):")
        for l in decaying[:5]:
            handle = f"@{l.username}" if l.username else (l.first_name or f"id:{l.telegram_id}")
            days_idle = (datetime.utcnow() - l.last_dm_at).days if l.last_dm_at else "?"
            lines.append(f"  • {handle} — {days_idle}d sem responder")

    # Perguntas pendentes
    if questions_unanswered:
        lines.append("\n❓ *Perguntas aguardando sua resposta*:")
        for lead, msg in questions_unanswered:
            handle = f"@{lead.username}" if lead.username else (lead.first_name or f"id:{lead.telegram_id}")
            preview = (msg.content or "")[:60]
            hours_ago = int((datetime.utcnow() - msg.created_at).total_seconds() / 3600) if msg.created_at else 0
            lines.append(f"  • {handle} ({hours_ago}h): _{preview}_")

    text = "\n".join(lines)
    target = int(admin) if admin.lstrip("-").isdigit() else admin
    try:
        await client.send_message(target, text, parse_mode="markdown")
        logger.info("[digest] enviado pro admin")
        return {"ok": True}
    except Exception:
        logger.exception("[digest] falhou")
        return {"ok": False}


# ---------------------------------------------------------------------------
# 8) Re-validação semanal via @QuotexPartnerBot + detecção de delta
# ---------------------------------------------------------------------------
async def task_weekly_revalidation(client) -> dict:
    """Domingo 03h00 BA — re-checa todos os IDs validados via partner bot.

    Marca como rewarm_candidate quem TINHA saldo e agora tem zero (perdeu tudo).
    Esses entram no funil de re-aquecimento.

    Cap: 200 validações/semana pra não estressar o partner bot.
    """
    from userbot.leads import validate_id_via_partner_bot

    cap = int(os.getenv("MAX_REVALIDATIONS_PER_RUN", "200"))
    re_checked = 0
    rewarm_count = 0
    still_active = 0
    invalid_now = 0

    with SessionLocal() as s:
        # Prioriza quem foi validado HÁ MAIS TEMPO
        leads = (
            s.query(Lead)
            .filter(Lead.liga_id_status == "validated")
            .filter(Lead.liga_account_id.isnot(None))
            .order_by(Lead.last_revalidated_at.asc().nullsfirst() if hasattr(Lead.last_revalidated_at.asc(), "nullsfirst") else Lead.last_revalidated_at.asc())
            .limit(cap)
            .all()
        )
        lead_ids = [l.id for l in leads]

    for lead_id in lead_ids:
        try:
            with SessionLocal() as s:
                lead = s.query(Lead).get(lead_id)
                if lead is None or not lead.liga_account_id:
                    continue
                old_balance = lead.liga_id_balance or 0.0
                old_deposits = lead.liga_id_deposits_sum or 0.0

                val = await validate_id_via_partner_bot(client, lead.liga_account_id)
                lead.last_revalidated_at = datetime.utcnow()
                lead.liga_id_partner_response = (val.get("raw") or "")[:4000]

                if val.get("status") == "validated":
                    new_balance = val.get("balance") or 0.0
                    new_deposits = val.get("deposits_sum") or 0.0
                    lead.liga_id_balance = new_balance
                    lead.liga_id_deposits_sum = new_deposits
                    lead.liga_id_turnover = val.get("turnover")
                    lead.liga_id_validated_at = datetime.utcnow()
                    re_checked += 1
                    still_active += 1

                    # Salva snapshot histórico (pra ver evolução ao longo do tempo)
                    s.add(BalanceSnapshot(
                        lead_id=lead.id,
                        balance=new_balance,
                        deposits_sum=new_deposits,
                        turnover=val.get("turnover") or 0.0,
                        source="partner_bot",
                    ))

                    # Detecta perda: tinha saldo, agora zero → rewarm candidate
                    if old_balance >= 50 and new_balance < 5:
                        lead.rewarm_candidate = True
                        rewarm_count += 1
                        logger.info(
                            "[revalidation] lead=%s perdeu saldo ($%.2f → $%.2f) — rewarm",
                            lead.display_name, old_balance, new_balance,
                        )
                    elif new_balance >= 50:
                        lead.rewarm_candidate = False  # voltou a ter saldo

                    # VIP detection: deposits altos mantém flag
                    _maybe_flag_vip(lead)
                elif val.get("status") == "invalid":
                    lead.liga_id_status = "invalid"
                    invalid_now += 1

                s.commit()
            await asyncio.sleep(1.0)  # respeita o partner bot
        except Exception:
            logger.exception("[revalidation] erro lead %s", lead_id)

    result = {
        "checked": re_checked, "still_active": still_active,
        "rewarm_candidates_new": rewarm_count, "invalid": invalid_now,
    }
    logger.info("[revalidation] %s", result)
    return result


# ---------------------------------------------------------------------------
# 11.5) Validação automática de IDs pendentes
# ---------------------------------------------------------------------------
# Estado compartilhado pra controlar execução + cancelamento
_pending_validation_state = {
    "running": False,
    "should_stop": False,
    "started_at": None,
    "current_index": 0,
    "total": 0,
    "validated": 0,
    "invalid": 0,
    "errors": 0,
    "current_lead": None,
}


def is_pending_validation_running() -> bool:
    """Retorna True se task_validate_pending_ids está rodando agora."""
    return bool(_pending_validation_state.get("running"))


def stop_pending_validation() -> bool:
    """Solicita parada da validação em andamento. Próximo lead na fila é o último."""
    if not _pending_validation_state.get("running"):
        return False
    _pending_validation_state["should_stop"] = True
    logger.info("[validate_pending] STOP solicitado pelo usuário")
    return True


def get_pending_validation_status() -> dict:
    """Snapshot do estado atual pra UI exibir progresso."""
    s = _pending_validation_state
    elapsed = None
    if s.get("started_at"):
        elapsed = int((datetime.utcnow() - s["started_at"]).total_seconds())
    return {
        "running": s.get("running", False),
        "should_stop": s.get("should_stop", False),
        "current_index": s.get("current_index", 0),
        "total": s.get("total", 0),
        "validated": s.get("validated", 0),
        "invalid": s.get("invalid", 0),
        "errors": s.get("errors", 0),
        "current_lead": s.get("current_lead"),
        "elapsed_seconds": elapsed,
    }


async def task_validate_pending_ids(client, max_validations: int = 30, force_full: bool = False) -> dict:
    """A cada hora — pega leads com ID EXTRAÍDO mas NÃO VALIDADO e tenta validar.

    Cobre o gap onde:
    - Scan inicial achou ID mas o cap de validações/run foi atingido
      (lead.liga_id_status == 'extracted')
    - Validação anterior caiu em 'needs_review' (formato suspeito etc)
    - Vision capturou ID em tempo real mas validação falhou silenciosamente
    - Lead marcado 'invalid' há > 30 dias (pode ter criado conta nova)

    Backoff por lead:
    - Cada lead só é tentado 1× a cada 24h (a menos que force_full=True)
    - Evita spam ao @QuotexPartnerBot quando há muitos pendentes
    - Cron rodando a cada hora vai esvaziar a fila em ~1 dia depois resetar

    Estratégia anti-spam:
    - Cap de 30 validações por execução (configurável via env)
    - Delay de 15s + jitter aleatório (±3s) entre cada chamada
      → 30 leads × ~16s ≈ 8 minutos por execução
    - Suporta cancelamento via stop_pending_validation()
    - Não roda 2 instâncias ao mesmo tempo (max_instances=1 no scheduler)

    Prioridade: VIPs primeiro → saldo declarado alto → mais recentes.

    force_full=True: ignora o backoff de 24h e tenta TODOS pendentes.
        Use só quando o user clicar 'Validação completa agora' no painel.
    """
    import random
    from userbot.leads import validate_id_via_partner_bot, _looks_like_valid_id
    from sqlalchemy import or_, and_

    # Não roda se já tem instância ativa
    if _pending_validation_state["running"]:
        logger.warning("[validate_pending] já está rodando — abortando segunda instância")
        return {"error": "já está rodando", "running": True}

    cap = int(os.getenv("MAX_PENDING_VALIDATIONS_PER_RUN", str(max_validations)))
    delay_base = float(os.getenv("PARTNER_BOT_DELAY_SECONDS", "15.0"))
    delay_jitter = float(os.getenv("PARTNER_BOT_DELAY_JITTER", "3.0"))
    backoff_hours = int(os.getenv("PARTNER_BOT_BACKOFF_HOURS", "24"))
    invalid_retry_days = int(os.getenv("INVALID_ID_RETRY_DAYS", "30"))

    # Marca como rodando
    _pending_validation_state.update({
        "running": True,
        "should_stop": False,
        "started_at": datetime.utcnow(),
        "current_index": 0,
        "total": 0,
        "validated": 0,
        "invalid": 0,
        "errors": 0,
        "current_lead": None,
    })

    validated = invalid = error = skipped = stopped_early = 0

    try:
        with SessionLocal() as s:
            now = datetime.utcnow()
            backoff_cutoff = now - timedelta(hours=backoff_hours)
            invalid_retry_cutoff = now - timedelta(days=invalid_retry_days)

            base_query = (
                s.query(Lead)
                .filter(Lead.liga_account_id.isnot(None))
                .filter(Lead.liga_account_id != "")
                .filter(Lead.opted_out.is_(False))
            )

            # Status válidos pra revalidação:
            #  - extracted/needs_review/pending: tentativas anteriores incompletas
            #  - invalid: SÓ se foi há > 30 dias (lead pode ter criado conta nova)
            status_filter = or_(
                Lead.liga_id_status.in_(["extracted", "needs_review", "pending", None]),
                and_(
                    Lead.liga_id_status == "invalid",
                    or_(
                        Lead.last_revalidated_at.is_(None),
                        Lead.last_revalidated_at < invalid_retry_cutoff,
                    ),
                ),
            )

            base_query = base_query.filter(status_filter)

            # Backoff: se NÃO é forçado, pula leads tentados nas últimas N horas
            if not force_full:
                base_query = base_query.filter(
                    or_(
                        Lead.last_revalidated_at.is_(None),
                        Lead.last_revalidated_at < backoff_cutoff,
                    )
                )

            leads = (
                base_query
                .order_by(
                    Lead.is_vip_potential.desc().nullslast() if hasattr(Lead.is_vip_potential.desc(), "nullslast") else Lead.is_vip_potential.desc(),
                    Lead.liga_balance.desc().nullslast() if hasattr(Lead.liga_balance.desc(), "nullslast") else Lead.liga_balance.desc(),
                    Lead.last_dm_at.desc(),
                )
                .limit(cap)
                .all()
            )
            lead_ids = [(l.id, l.liga_account_id, l.display_name) for l in leads]

        _pending_validation_state["total"] = len(lead_ids)
        mode = "FORCE FULL" if force_full else f"backoff {backoff_hours}h"
        logger.info(
            "[validate_pending] %d leads na fila · %s · delay=%.0fs±%.0fs · ETA ≈ %d min",
            len(lead_ids), mode, delay_base, delay_jitter,
            int(len(lead_ids) * delay_base / 60),
        )

        for idx, (lead_id, cand_id, display_name) in enumerate(lead_ids, 1):
            # Verifica cancelamento ANTES de processar
            if _pending_validation_state["should_stop"]:
                logger.info("[validate_pending] interrompido pelo usuário em %d/%d", idx-1, len(lead_ids))
                stopped_early = len(lead_ids) - (idx - 1)
                break

            _pending_validation_state["current_index"] = idx
            _pending_validation_state["current_lead"] = display_name

            # Validação prévia do formato — evita bater no partner bot com lixo
            if not _looks_like_valid_id(cand_id):
                with SessionLocal() as s:
                    lead = s.query(Lead).get(lead_id)
                    if lead:
                        lead.liga_id_status = "needs_review"
                        s.commit()
                skipped += 1
                continue

            try:
                val = await validate_id_via_partner_bot(client, cand_id)

                with SessionLocal() as s:
                    lead = s.query(Lead).get(lead_id)
                    if not lead:
                        continue
                    lead.last_revalidated_at = datetime.utcnow()
                    lead.liga_id_partner_response = (val.get("raw") or "")[:4000]

                    if val.get("status") == "validated":
                        lead.liga_id_status = "validated"
                        lead.liga_id_country = (val.get("country") or "")[:50]
                        lead.liga_id_balance = val.get("balance")
                        lead.liga_id_deposits_sum = val.get("deposits_sum")
                        lead.liga_id_turnover = val.get("turnover")
                        lead.liga_id_validated_at = datetime.utcnow()
                        _maybe_flag_vip(lead)
                        validated += 1
                        _pending_validation_state["validated"] = validated
                        logger.info(
                            "[validate_pending] (%d/%d) ✓ %s id=%s país=%s saldo=$%.2f deps=$%.2f",
                            idx, len(lead_ids),
                            display_name, cand_id, val.get("country"),
                            val.get("balance") or 0, val.get("deposits_sum") or 0,
                        )
                    elif val.get("status") == "invalid":
                        lead.liga_id_status = "invalid"
                        invalid += 1
                        _pending_validation_state["invalid"] = invalid
                    else:
                        if lead.liga_id_status != "extracted":
                            lead.liga_id_status = "extracted"
                        error += 1
                        _pending_validation_state["errors"] = error

                    s.commit()

                # Sleep com jitter aleatório (anti-spam) — só se não é o último
                if idx < len(lead_ids) and not _pending_validation_state["should_stop"]:
                    actual_delay = delay_base + random.uniform(-delay_jitter, delay_jitter)
                    actual_delay = max(5.0, actual_delay)  # nunca menos que 5s
                    # Sleep dividido em pedaços de 0.5s pra responder cancelamento rápido
                    waited = 0.0
                    while waited < actual_delay and not _pending_validation_state["should_stop"]:
                        chunk = min(0.5, actual_delay - waited)
                        await asyncio.sleep(chunk)
                        waited += chunk
            except Exception:
                logger.exception("[validate_pending] erro lead %s", display_name)
                error += 1
                _pending_validation_state["errors"] = error

        result = {
            "checked": len(lead_ids) - stopped_early,
            "validated": validated,
            "invalid": invalid,
            "errors": error,
            "skipped_bad_format": skipped,
            "stopped_early": stopped_early,
            "total_in_queue": len(lead_ids),
        }
        logger.info("[validate_pending] resultado: %s", result)
        return result
    finally:
        # Sempre limpa estado ao terminar (inclusive em caso de erro)
        _pending_validation_state.update({
            "running": False,
            "should_stop": False,
            "current_lead": None,
        })


# ---------------------------------------------------------------------------
# 11.7) Deep scan em batch — varre conversas de TODOS sem ID
# ---------------------------------------------------------------------------
_scan_all_state = {
    "running": False,
    "should_stop": False,
    "started_at": None,
    "current_index": 0,
    "total": 0,
    "ids_found": 0,
    "validated": 0,
    "invalid": 0,
    "no_id": 0,
    "errors": 0,
    "current_lead": None,
}


def is_scan_all_running() -> bool:
    return bool(_scan_all_state.get("running"))


def stop_scan_all() -> bool:
    if not _scan_all_state.get("running"):
        return False
    _scan_all_state["should_stop"] = True
    logger.info("[scan_all] STOP solicitado pelo usuário")
    return True


def get_scan_all_status() -> dict:
    s = _scan_all_state
    elapsed = None
    if s.get("started_at"):
        elapsed = int((datetime.utcnow() - s["started_at"]).total_seconds())
    return {
        "running": s.get("running", False),
        "should_stop": s.get("should_stop", False),
        "current_index": s.get("current_index", 0),
        "total": s.get("total", 0),
        "ids_found": s.get("ids_found", 0),
        "validated": s.get("validated", 0),
        "invalid": s.get("invalid", 0),
        "no_id": s.get("no_id", 0),
        "errors": s.get("errors", 0),
        "current_lead": s.get("current_lead"),
        "elapsed_seconds": elapsed,
    }


async def task_scan_all_pending_dms(client, max_leads: int = 30) -> dict:
    """Pra cada lead SEM ID válido: lê últimas 50 DMs (texto + Vision) procurando ID.

    Mais agressivo que task_validate_pending_ids:
    - Aquele só revalida IDs JÁ EXTRAÍDOS
    - Esse procura IDs em conversas de quem NUNCA TEVE ID extraído (ou tem inválido)

    Pra cada lead encontrado:
    - Se ID válido formato → valida no @QuotexPartnerBot
    - Se inválido formato → marca needs_review
    - Se nada encontrado → marca needs_review

    Anti-spam:
    - Cap de 30 leads por execução (configurável)
    - Delay 15s + jitter ±3s entre validações no partner bot
    - Suporta cancelamento via stop_scan_all()
    """
    import random
    from userbot.leads import (
        find_recent_account_id_in_dms,
        validate_id_via_partner_bot,
        _looks_like_valid_id,
    )

    if _scan_all_state["running"]:
        return {"error": "já está rodando", "running": True}

    cap = int(os.getenv("MAX_SCAN_ALL_PER_RUN", str(max_leads)))
    delay_base = float(os.getenv("PARTNER_BOT_DELAY_SECONDS", "15.0"))
    delay_jitter = float(os.getenv("PARTNER_BOT_DELAY_JITTER", "3.0"))

    _scan_all_state.update({
        "running": True,
        "should_stop": False,
        "started_at": datetime.utcnow(),
        "current_index": 0,
        "total": 0,
        "ids_found": 0,
        "validated": 0,
        "invalid": 0,
        "no_id": 0,
        "errors": 0,
        "current_lead": None,
    })

    ids_found = validated = invalid = no_id = error = stopped_early = 0

    try:
        with SessionLocal() as s:
            # Leads sem ID OU com status problemático, prioridade VIPs/saldo
            leads = (
                s.query(Lead)
                .filter(
                    (Lead.liga_account_id.is_(None)) |
                    (Lead.liga_account_id == "") |
                    (Lead.liga_id_status.in_(["needs_review", "invalid"]))
                )
                .filter(Lead.opted_out.is_(False))
                .filter(Lead.in_private_group.is_(False))
                .filter(Lead.last_dm_at.isnot(None))  # tem que ter conversado
                .order_by(
                    Lead.is_vip_potential.desc().nullslast() if hasattr(Lead.is_vip_potential.desc(), "nullslast") else Lead.is_vip_potential.desc(),
                    Lead.liga_balance.desc().nullslast() if hasattr(Lead.liga_balance.desc(), "nullslast") else Lead.liga_balance.desc(),
                    Lead.last_dm_at.desc(),
                )
                .limit(cap)
                .all()
            )
            lead_data = [(l.id, l.telegram_id, l.display_name) for l in leads]

        _scan_all_state["total"] = len(lead_data)
        logger.info(
            "[scan_all] %d leads na fila · cap=%d · delay=%.0fs±%.0fs · ETA ≈ %d min",
            len(lead_data), cap, delay_base, delay_jitter,
            int(len(lead_data) * (delay_base + 5) / 60),  # +5s pra scan + Vision
        )

        for idx, (lead_id, telegram_id, display_name) in enumerate(lead_data, 1):
            if _scan_all_state["should_stop"]:
                stopped_early = len(lead_data) - (idx - 1)
                logger.info("[scan_all] interrompido em %d/%d", idx-1, len(lead_data))
                break

            _scan_all_state["current_index"] = idx
            _scan_all_state["current_lead"] = display_name

            try:
                # Deep scan — pega TODOS candidatos (texto + imagem)
                cand = await find_recent_account_id_in_dms(
                    client, telegram_id,
                    max_messages=50,
                    scan_images=True,
                    max_images=2,
                    return_all=True,  # ← cascata: tenta texto, se falhar tenta imagem
                )
                candidates_list = cand.get("candidates") or []

                if not candidates_list:
                    # Nada encontrado
                    with SessionLocal() as s:
                        lead = s.query(Lead).get(lead_id)
                        if lead:
                            lead.last_revalidated_at = datetime.utcnow()
                            if lead.liga_id_status not in ("validated", "invalid"):
                                lead.liga_id_status = "needs_review"
                            s.commit()
                    no_id += 1
                    _scan_all_state["no_id"] = no_id
                    continue

                ids_found += 1
                _scan_all_state["ids_found"] = ids_found

                # Tenta validar cada candidato até achar 1 validated
                # (salva o último resultado pra fallback)
                last_val = None
                last_cand_id = None
                last_source = None
                final_status = None

                for c in candidates_list:
                    test_id = c["id"]
                    test_source = c.get("source", "?")
                    val = await validate_id_via_partner_bot(client, test_id)
                    last_val = val
                    last_cand_id = test_id
                    last_source = test_source

                    if val.get("status") == "validated":
                        final_status = "validated"
                        break
                    # Se invalid e tem mais candidatos, tenta o próximo após delay curto
                    if val.get("status") == "invalid" and len(candidates_list) > 1:
                        await asyncio.sleep(2.0)  # delay curto entre múltiplas tentativas

                # Aplica o melhor resultado obtido
                with SessionLocal() as s:
                    lead = s.query(Lead).get(lead_id)
                    if not lead:
                        continue
                    lead.liga_account_id = last_cand_id
                    lead.last_revalidated_at = datetime.utcnow()
                    lead.liga_id_partner_response = (last_val.get("raw") or "")[:4000] if last_val else ""

                    if final_status == "validated":
                        lead.liga_id_status = "validated"
                        lead.liga_id_country = (last_val.get("country") or "")[:50]
                        lead.liga_id_balance = last_val.get("balance")
                        lead.liga_id_deposits_sum = last_val.get("deposits_sum")
                        lead.liga_id_turnover = last_val.get("turnover")
                        lead.liga_id_validated_at = datetime.utcnow()
                        _maybe_flag_vip(lead)
                        validated += 1
                        _scan_all_state["validated"] = validated
                        logger.info(
                            "[scan_all] (%d/%d) ✓ %s id=%s país=%s saldo=$%.2f (%d candidato(s) testado(s), match via %s)",
                            idx, len(lead_data), display_name, last_cand_id,
                            last_val.get("country"), last_val.get("balance") or 0,
                            len(candidates_list), last_source,
                        )
                    elif last_val and last_val.get("status") == "invalid":
                        lead.liga_id_status = "invalid"
                        invalid += 1
                        _scan_all_state["invalid"] = invalid
                    else:
                        lead.liga_id_status = "extracted"

                    s.commit()

                # Sleep com jitter — só se não é o último e não parou
                if idx < len(lead_data) and not _scan_all_state["should_stop"]:
                    actual_delay = max(5.0, delay_base + random.uniform(-delay_jitter, delay_jitter))
                    waited = 0.0
                    while waited < actual_delay and not _scan_all_state["should_stop"]:
                        chunk = min(0.5, actual_delay - waited)
                        await asyncio.sleep(chunk)
                        waited += chunk

            except Exception:
                logger.exception("[scan_all] erro lead %s", display_name)
                error += 1
                _scan_all_state["errors"] = error

        result = {
            "checked": len(lead_data) - stopped_early,
            "ids_found": ids_found,
            "validated": validated,
            "invalid": invalid,
            "no_id": no_id,
            "errors": error,
            "stopped_early": stopped_early,
            "total_in_queue": len(lead_data),
        }
        logger.info("[scan_all] resultado: %s", result)
        return result
    finally:
        _scan_all_state.update({
            "running": False,
            "should_stop": False,
            "current_lead": None,
        })


# ---------------------------------------------------------------------------
# 11.8) Rescue de sends presos em queue
# ---------------------------------------------------------------------------
async def task_rescue_stuck_sends(client=None) -> dict:
    """A cada 5 min — varre Send com status='queued' há > 10 minutos e tenta processar.

    Cobre cenários onde sends ficam presos:
    - Bot reiniciou no meio de uma campanha (APScheduler em memória perde jobs)
    - Erro de rede que impediu commit
    - Campanha completed mas alguns sends ficaram pra trás
    """
    from db.models import Send, Campaign, SendStatus, CampaignStatus
    from userbot.sender import execute_send_record

    cutoff = datetime.utcnow() - timedelta(minutes=10)
    cap = int(os.getenv("RESCUE_STUCK_SENDS_CAP", "50"))

    with SessionLocal() as s:
        stuck = (
            s.query(Send)
            .join(Campaign, Send.campaign_id == Campaign.id)
            .filter(Send.status == SendStatus.QUEUED.value)
            .filter(Send.queued_at < cutoff)
            .filter(Campaign.status.notin_([
                CampaignStatus.CANCELLED.value,
                CampaignStatus.PAUSED.value,
            ]))
            .order_by(Send.queued_at.asc())
            .limit(cap)
            .all()
        )
        send_ids = [sd.id for sd in stuck]

    if not send_ids:
        return {"checked": 0, "processed": 0, "queued_remaining": 0}

    logger.info("[rescue] %d sends presos em queue há > 10min — processando...", len(send_ids))

    processed = sent = failed = skipped = 0
    for sid in send_ids:
        try:
            res = await execute_send_record(sid)
            processed += 1
            if res.success:
                sent += 1
            elif res.skipped_reason:
                skipped += 1
            else:
                failed += 1
        except Exception:
            logger.exception("[rescue] erro processando send %s", sid)
            failed += 1

    # Conta quantos AINDA estão em queue depois
    with SessionLocal() as s:
        remaining = (
            s.query(Send)
            .filter(Send.status == SendStatus.QUEUED.value)
            .filter(Send.queued_at < cutoff)
            .count()
        )

    result = {
        "checked": len(send_ids),
        "processed": processed,
        "sent": sent,
        "skipped": skipped,
        "failed": failed,
        "queued_remaining": remaining,
    }
    logger.info("[rescue] resultado: %s", result)
    return result


# ---------------------------------------------------------------------------
# 12) VIP potential detection
# ---------------------------------------------------------------------------
VIP_DEPOSIT_THRESHOLD = float(os.getenv("VIP_DEPOSIT_THRESHOLD", "500"))   # depósitos >= $500
VIP_TURNOVER_THRESHOLD = float(os.getenv("VIP_TURNOVER_THRESHOLD", "5000")) # turnover >= $5k
VIP_BALANCE_THRESHOLD = float(os.getenv("VIP_BALANCE_THRESHOLD", "300"))    # saldo atual >= $300


def _maybe_flag_vip(lead: Lead) -> bool:
    """Calcula e seta is_vip_potential com base em deposits/turnover/balance."""
    deps = lead.liga_id_deposits_sum or 0.0
    turn = lead.liga_id_turnover or 0.0
    bal = (lead.liga_id_balance or 0.0)
    is_vip = (
        deps >= VIP_DEPOSIT_THRESHOLD
        or turn >= VIP_TURNOVER_THRESHOLD
        or bal >= VIP_BALANCE_THRESHOLD
    )
    if is_vip != bool(lead.is_vip_potential):
        lead.is_vip_potential = is_vip
        if is_vip:
            logger.info(
                "[vip] lead=%s flaggado VIP (deps=$%.0f turn=$%.0f bal=$%.0f)",
                lead.display_name, deps, turn, bal,
            )
        return True
    return False


async def task_recalculate_vips() -> dict:
    """Recalcula is_vip_potential pra todos os leads. Idempotente."""
    flagged = unflagged = 0
    with SessionLocal() as s:
        leads = s.query(Lead).all()
        for lead in leads:
            was = bool(lead.is_vip_potential)
            _maybe_flag_vip(lead)
            now = bool(lead.is_vip_potential)
            if now and not was: flagged += 1
            if was and not now: unflagged += 1
        s.commit()
    logger.info("[vip] recalc: +%d, -%d", flagged, unflagged)
    return {"newly_flagged": flagged, "unflagged": unflagged}


# ---------------------------------------------------------------------------
# 7) Follow-up automático por engagement_tag
# ---------------------------------------------------------------------------
async def task_run_follow_ups(client) -> dict:
    """10h00 BA — itera FollowUpRule e dispara scripts pra leads que matcham.

    Critérios:
    - Lead com engagement_tag == regra.target_engagement_tag
    - Lead recebeu < regra.max_sends_per_lead disparos dessa regra
    - Última atividade do lead há ≥ regra.min_days_idle dias
    - Lead NÃO opted_out, NÃO BLOCKED, NÃO EXCLUDED
    """
    from userbot.sender import execute_send_record
    from db.models import Campaign, CampaignStatus

    sent_total = 0
    rules_run = 0

    with SessionLocal() as s:
        active_rules = (
            s.query(FollowUpRule)
            .filter(FollowUpRule.is_active.is_(True))
            .filter(FollowUpRule.script_id.isnot(None))
            .all()
        )
        rules_data = [
            (r.id, r.target_engagement_tag, r.min_days_idle, r.script_id, r.max_sends_per_lead, r.name)
            for r in active_rules
        ]

    # Lazy import pra evitar circular
    from .remarketing_stage import eligible_leads_for_dispatch

    for rule_id, tag, days_idle, script_id, max_per_lead, name in rules_data:
        rules_run += 1
        cutoff = datetime.utcnow() - timedelta(days=days_idle)
        with SessionLocal() as s:
            from db.models import Script
            script_obj = s.query(Script).get(script_id)
            if not script_obj:
                logger.warning("[follow_up] rule %s: script %s não encontrado", rule_id, script_id)
                continue

            # Usa helper centralizado pra aplicar TODOS os filtros
            # (opted_out, in_private_group, status, fresh, cooldowns, VIPs, excluded, etc)
            # mais o filtro específico da rule (engagement_tag).
            eligible, _stats = eligible_leads_for_dispatch(
                s, script_obj,
                target_engagement_override=tag,
                exclude_vips=True,  # follow-ups automáticos NUNCA tocam VIPs
                max_leads=50,        # cap por regra/run
            )
            # Filtro adicional da regra: last_dm_at < cutoff (idle days)
            eligible = [
                l for l in eligible
                if l.last_dm_at is None or l.last_dm_at < cutoff
            ]
            cand_ids = [l.id for l in eligible]

        sent_this_rule = 0
        for lead_id in cand_ids:
            try:
                with SessionLocal() as s:
                    # Conta quantos sends dessa regra/script o lead já recebeu
                    existing = (
                        s.query(Send)
                        .filter(Send.lead_id == lead_id)
                        .filter(Send.script_id == script_id)
                        .count()
                    )
                    if existing >= max_per_lead:
                        continue

                    # Cria/reusa campanha "auto" pra essa regra
                    camp = (
                        s.query(Campaign)
                        .filter(Campaign.name == f"auto:{name}")
                        .filter(Campaign.status == CampaignStatus.RUNNING.value)
                        .first()
                    )
                    if not camp:
                        camp = Campaign(
                            script_id=script_id,
                            name=f"auto:{name}",
                            status=CampaignStatus.RUNNING.value,
                            started_at=datetime.utcnow(),
                            notes=f"Auto follow-up rule #{rule_id}",
                            target_engagement_tag=tag,
                            exclude_vips=True,
                        )
                        s.add(camp)
                        s.commit()
                        s.refresh(camp)

                    send = Send(
                        campaign_id=camp.id,
                        lead_id=lead_id,
                        script_id=script_id,
                        status=SendStatus.QUEUED.value,
                    )
                    s.add(send)
                    s.commit()
                    s.refresh(send)
                    send_id = send.id

                # FIX: execute_send_record retorna SendResult (NÃO tupla!)
                # Antes: '_, result = await ...' dava TypeError silenciado pelo
                # except Exception abaixo. Sends iam mas estatísticas zeradas.
                result = await execute_send_record(send_id)
                if result and result.success:
                    sent_this_rule += 1
                    sent_total += 1
                await asyncio.sleep(2.0)  # respeita rate limit
            except Exception:
                logger.exception("[follow_up] erro lead %s rule %s", lead_id, rule_id)

        # Atualiza estatísticas da regra
        with SessionLocal() as s:
            r = s.query(FollowUpRule).get(rule_id)
            if r:
                r.last_run_at = datetime.utcnow()
                r.sent_count = (r.sent_count or 0) + sent_this_rule
                s.commit()

    logger.info("[follow_up] %d regras processadas, %d envios", rules_run, sent_total)
    return {"rules_run": rules_run, "sent_total": sent_total}


# ---------------------------------------------------------------------------
# 13) Account warming meter
# ---------------------------------------------------------------------------
async def task_check_private_group_members(client) -> dict:
    """Cron de 10 em 10 min: pega snapshot dos membros atuais do grupo privado
    e detecta entrantes novos que o listener `_on_chat_action` pode ter perdido.

    Pra cada novo membro:
    - Marca `in_private_group = True`
    - Marca último Send como conversion (bump métricas do script/variant)
    - Atualiza status pra REPLIED se ainda era PENDING/CONTACTED
    """
    from userbot.leads import get_private_group_member_ids
    from db.models import Script, ScriptVariant

    try:
        members = await get_private_group_member_ids()
    except Exception:
        logger.exception("[group_check] erro listando membros")
        return {"error": "falha listando grupo"}

    if not members:
        return {"ok": True, "new": 0, "total_members": 0}

    new_conversions = 0
    new_excluded = 0
    with SessionLocal() as session:
        for tg_id in members:
            lead = session.query(Lead).filter_by(telegram_id=tg_id).one_or_none()
            if not lead:
                # Lead que nem está no nosso DB ainda — cria como EXCLUDED
                session.add(Lead(
                    telegram_id=tg_id,
                    in_private_group=True,
                    status=LeadStatus.EXCLUDED.value,
                    source="private_group_member",
                ))
                new_excluded += 1
                continue

            # Lead já existe: detecta mudança
            if lead.in_private_group:
                continue  # já estava no grupo

            lead.in_private_group = True
            new_conversions += 1
            logger.info(
                "[group_check] 🎉 novo membro detectado: %s (entrou no grupo privado)",
                lead.display_name,
            )

            # Marca último Send como conversion
            last_send = (
                session.query(Send)
                .filter(Send.lead_id == lead.id)
                .filter(Send.status == SendStatus.SENT.value)
                .order_by(Send.sent_at.desc())
                .first()
            )
            if last_send and not last_send.replied:
                last_send.reply_classification = "conversion"
                if last_send.script_id:
                    script = session.query(Script).get(last_send.script_id)
                    if script:
                        script.conversions_count = (script.conversions_count or 0) + 1
                if last_send.variant_id:
                    variant = session.query(ScriptVariant).get(last_send.variant_id)
                    if variant:
                        variant.conversions_count = (variant.conversions_count or 0) + 1

            # Atualiza status
            if lead.status in (LeadStatus.PENDING.value, LeadStatus.CONTACTED.value):
                lead.status = LeadStatus.REPLIED.value

        session.commit()

    if new_conversions or new_excluded:
        logger.info(
            "[group_check] %d conversões novas, %d novos no grupo (sem registro prévio)",
            new_conversions, new_excluded,
        )
    return {
        "ok": True,
        "new_conversions": new_conversions,
        "new_excluded": new_excluded,
        "total_members": len(members),
    }


def get_account_health() -> dict:
    """Snapshot da saúde do userbot baseado em métricas dos últimos 7d."""
    cutoff = datetime.utcnow() - timedelta(days=7)
    with SessionLocal() as s:
        sent = (
            s.query(func.count(Send.id))
            .filter(Send.sent_at >= cutoff)
            .filter(Send.status == SendStatus.SENT.value)
            .scalar() or 0
        )
        failed = (
            s.query(func.count(Send.id))
            .filter(Send.queued_at >= cutoff)
            .filter(Send.status == SendStatus.FAILED.value)
            .scalar() or 0
        )
        replied = (
            s.query(func.count(Send.id))
            .filter(Send.sent_at >= cutoff)
            .filter(Send.replied.is_(True))
            .scalar() or 0
        )
        blocked_recently = (
            s.query(func.count(Lead.id))
            .filter(Lead.status == LeadStatus.BLOCKED.value)
            .filter(Lead.updated_at >= cutoff)
            .scalar() or 0
        )

    reply_rate = (replied / sent) if sent else 0.0
    error_rate = (failed / max(sent + failed, 1))

    # Determina health: red se error_rate > 20% ou blocked > 10
    if error_rate > 0.20 or blocked_recently > 10:
        health = "red"
        msg = "⚠ Possível shadow ban ou conta limitada"
    elif error_rate > 0.10 or reply_rate < 0.05:
        health = "yellow"
        msg = "⚠ Métricas degradadas — observar"
    else:
        health = "green"
        msg = "✓ Saudável"

    return {
        "health": health, "message": msg,
        "sent_7d": sent, "failed_7d": failed, "replied_7d": replied,
        "blocked_recent": blocked_recently,
        "reply_rate": round(reply_rate * 100, 1),
        "error_rate": round(error_rate * 100, 1),
    }


# ---------------------------------------------------------------------------
# Scan incremental de DMs — roda a cada 5 minutos
# ---------------------------------------------------------------------------
INCREMENTAL_LAST_CHECK_KEY = "last_dm_incremental_check_at"


def _get_last_incremental_check() -> datetime:
    """Lê last check do Setting. Default: 6 minutos atrás (cobre 1 ciclo de 5min)."""
    with SessionLocal() as s:
        row = s.query(Setting).filter_by(key=INCREMENTAL_LAST_CHECK_KEY).first()
        if row and row.value:
            try:
                return datetime.fromisoformat(row.value)
            except Exception:
                pass
    return datetime.utcnow() - timedelta(minutes=6)


def _set_last_incremental_check(when: datetime) -> None:
    with SessionLocal() as s:
        row = s.query(Setting).filter_by(key=INCREMENTAL_LAST_CHECK_KEY).first()
        if row:
            row.value = when.isoformat()
        else:
            s.add(Setting(key=INCREMENTAL_LAST_CHECK_KEY, value=when.isoformat()))
        s.commit()


async def task_incremental_dm_scan(client, max_leads: int = 30) -> dict:
    """A cada 5 min: scan incremental de DMs novas desde o último check.

    Comportamento:
    - Se nenhuma DM nova chegou desde o último check: NADA é feito (sem complemento)
    - Pra cada lead com nova DM: extrai ID (se ainda não tem) + valida no partner bot
    - Skipa leads validados / inválidos / no grupo privado / opted_out / blocked
    - Cap: max_leads por scan (default 30) pra ficar leve

    Atualiza Setting 'last_dm_incremental_check_at' ao terminar.
    """
    from userbot.leads import (
        find_recent_account_id_in_dms,
        validate_id_via_partner_bot,
        _looks_like_valid_id,
    )

    last_check = _get_last_incremental_check()
    new_check = datetime.utcnow()

    leads_seen = 0
    leads_with_new_dm = 0
    ids_extracted = 0
    ids_validated = 0
    ids_invalid = 0
    leads_skipped = 0

    try:
        async for dialog in client.iter_dialogs(limit=200):
            if not dialog.is_user:
                continue
            entity = dialog.entity
            if entity is None or getattr(entity, "bot", False) or getattr(entity, "is_self", False):
                continue

            # Compara dialog.date (aware) com last_check (assume UTC)
            d_date = dialog.date
            if d_date is None:
                continue
            try:
                d_naive = d_date.replace(tzinfo=None) if d_date.tzinfo else d_date
            except Exception:
                d_naive = d_date

            # Sem mensagens novas → break (próximos dialogs são ainda mais antigos)
            if d_naive < last_check:
                break

            leads_with_new_dm += 1
            if leads_seen >= max_leads:
                continue

            # Carrega o lead
            with SessionLocal() as s:
                lead = s.query(Lead).filter_by(telegram_id=entity.id).first()
                if not lead:
                    continue
                # Skipa quem não precisa
                if lead.in_private_group:
                    leads_skipped += 1
                    continue
                if lead.status in (LeadStatus.BLOCKED.value, LeadStatus.EXCLUDED.value):
                    leads_skipped += 1
                    continue
                if getattr(lead, "opted_out", False):
                    leads_skipped += 1
                    continue
                # Já tem ID definitivamente resolvido → skipa
                if lead.liga_id_status in ("validated", "invalid"):
                    leads_skipped += 1
                    continue

                lead_id = lead.id
                lead_display = lead.display_name
                already_has = (lead.liga_account_id or "").strip()

            # Tenta extrair ID nas últimas 15 msgs (texto + 1 imagem se necessário)
            try:
                cand = await find_recent_account_id_in_dms(
                    client, entity.id,
                    max_messages=15,
                    scan_images=True,
                    max_images=1,
                )
            except Exception:
                logger.debug("[incremental] erro extraindo lead=%s", lead_display, exc_info=True)
                continue

            cand_id = (cand.get("id") or "")[:100]
            if not cand_id:
                # Nenhum ID encontrado nessa janela — segue
                leads_seen += 1
                continue

            if not _looks_like_valid_id(cand_id):
                # Candidato inválido — manda pra revisão manual e segue
                with SessionLocal() as s:
                    lead = s.query(Lead).get(lead_id)
                    if lead:
                        lead.liga_id_status = "needs_review"
                        lead.liga_id_partner_response = (
                            f"[incremental] candidato '{cand_id}' fora de 7-9 dígitos"
                        )
                        s.commit()
                leads_seen += 1
                continue

            # Se já tem o mesmo ID, só re-valida se nunca validou
            if already_has and already_has == cand_id and lead.liga_id_status == "validated":
                leads_seen += 1
                continue

            ids_extracted += 1
            # Valida via @QuotexPartnerBot
            try:
                val = await validate_id_via_partner_bot(client, cand_id)
            except Exception:
                logger.debug("[incremental] erro validando lead=%s", lead_display, exc_info=True)
                val = {"status": "error", "raw": ""}

            with SessionLocal() as s:
                lead = s.query(Lead).get(lead_id)
                if not lead:
                    continue
                lead.liga_account_id = cand_id
                lead.liga_id_partner_response = (val.get("raw") or "")[:4000]

                if val.get("status") == "validated":
                    lead.liga_id_status = "validated"
                    lead.liga_id_country = (val.get("country") or "")[:50]
                    lead.liga_id_balance = val.get("balance")
                    lead.liga_id_deposits_sum = val.get("deposits_sum")
                    lead.liga_id_turnover = val.get("turnover")
                    lead.liga_id_validated_at = datetime.utcnow()
                    lead.last_revalidated_at = datetime.utcnow()
                    _maybe_flag_vip(lead)
                    ids_validated += 1
                    logger.info(
                        "[incremental] ✓ lead=%s id=%s país=%s",
                        lead_display, cand_id, val.get("country"),
                    )
                elif val.get("status") == "invalid":
                    lead.liga_id_status = "invalid"
                    ids_invalid += 1
                else:
                    lead.liga_id_status = "extracted"
                s.commit()

            leads_seen += 1
            await asyncio.sleep(0.6)  # respeita partner bot
    except Exception:
        logger.exception("[incremental] erro no loop")
    finally:
        _set_last_incremental_check(new_check)

    # Se nada aconteceu, só sai silenciosamente (sem ruído nos logs)
    if leads_with_new_dm == 0:
        return {"new_dms": 0, "skipped": True}

    result = {
        "new_dms": leads_with_new_dm,
        "leads_processed": leads_seen,
        "leads_skipped": leads_skipped,
        "ids_extracted": ids_extracted,
        "ids_validated": ids_validated,
        "ids_invalid": ids_invalid,
        "last_check": new_check.isoformat(),
    }
    if ids_extracted > 0 or ids_validated > 0:
        logger.info("[incremental] %s", result)
    return result
