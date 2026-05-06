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
    AIUsage, DailyVolume, FollowUpRule, ImageCache,
    Lead, LeadStatus, OperationProof, Script, Send, SendStatus,
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
async def task_daily_digest(client) -> dict:
    """08h00 BA — resumo do dia anterior pro admin."""
    admin = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
    if not admin:
        return {"ok": False}

    yesterday = (datetime.utcnow() - timedelta(days=1)).date()
    today = datetime.utcnow().date()

    with SessionLocal() as s:
        new_leads = s.query(Lead).filter(
            Lead.created_at >= yesterday,
            Lead.created_at < today + timedelta(days=1),
        ).count()
        deposits_promised = s.query(Lead).filter(
            Lead.engagement_tag == "deposit_promised",
        ).count()
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

    for rule_id, tag, days_idle, script_id, max_per_lead, name in rules_data:
        rules_run += 1
        cutoff = datetime.utcnow() - timedelta(days=days_idle)
        with SessionLocal() as s:
            candidates = (
                s.query(Lead)
                .filter(Lead.engagement_tag == tag)
                .filter(Lead.opted_out.is_(False))
                .filter(Lead.in_private_group.is_(False))
                .filter(Lead.status.notin_([LeadStatus.BLOCKED.value, LeadStatus.EXCLUDED.value]))
                .filter((Lead.last_dm_at.is_(None)) | (Lead.last_dm_at < cutoff))
                .limit(50)  # cap por regra/run
                .all()
            )
            cand_ids = [l.id for l in candidates]

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

                _, result = await execute_send_record(send_id)
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
