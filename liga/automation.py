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
