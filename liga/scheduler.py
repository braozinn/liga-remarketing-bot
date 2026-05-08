"""Tarefas agendadas da Liga (APScheduler).

Inclui:
- Lembrete diário 21h (BA): pinga ativos sem comprovante.
- Reset diário 00h01 (BA): zera proof_sent_today.
- Checkpoints (3) e corte final: elimina/at_risk quem está abaixo de $100.
- Relatório semanal (segunda 09h00 BA) → ADMIN_TELEGRAM_ID.
- Ranking diário (22h00 BA) → LIGA_GROUP.
"""
from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from typing import Optional

try:
    from zoneinfo import ZoneInfo
except ImportError:  # pragma: no cover
    ZoneInfo = None  # type: ignore

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.date import DateTrigger
from sqlalchemy import func

from db import SessionLocal
from db.models import DailyVolume, Lead, LigaState

logger = logging.getLogger(__name__)

BA_TZ_NAME = "America/Argentina/Buenos_Aires"
BA_TZ = ZoneInfo(BA_TZ_NAME) if ZoneInfo else None

# Estado global do scheduler
_scheduler: Optional[AsyncIOScheduler] = None
_client = None  # TelegramClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
async def _send_dm(telegram_id: int, text: str) -> bool:
    """Envia DM via Telethon. Retorna True se OK."""
    if _client is None:
        logger.warning("[liga] scheduler sem client Telethon; skip DM")
        return False
    try:
        await _client.send_message(telegram_id, text, parse_mode="markdown")
        return True
    except Exception:
        logger.exception("[liga] falha ao enviar DM para %s", telegram_id)
        return False


async def _send_to_target(target: str, text: str) -> bool:
    """Envia mensagem para um @username, ID numérico ou texto-livre."""
    if _client is None or not target:
        return False
    try:
        target = target.strip()
        if target.lstrip("-").isdigit():
            entity = await _client.get_entity(int(target))
        else:
            entity = await _client.get_entity(target)
        await _client.send_message(entity, text, parse_mode="markdown")
        return True
    except Exception:
        logger.exception("[liga] falha ao enviar para %s", target)
        return False


def _progress_bar(current: float, target: float, width: int = 20) -> str:
    if target <= 0:
        return "░" * width
    pct = max(0.0, min(1.0, current / target))
    filled = int(round(pct * width))
    return "▓" * filled + "░" * (width - filled)


# ---------------------------------------------------------------------------
# Tarefas
# ---------------------------------------------------------------------------
async def task_daily_reminder() -> None:
    """21h00 (BA) — pinga ativos sem comprovante de hoje."""
    sent = 0
    with SessionLocal() as session:
        ativos = (
            session.query(Lead)
            .filter(Lead.liga_state == LigaState.ACTIVE.value)
            .filter(Lead.proof_sent_today.is_(False))
            .all()
        )
        for lead in ativos:
            nome = lead.first_name or lead.display_name
            ok = await _send_dm(
                lead.telegram_id,
                f"📸 Eh *{nome}*! Todavía no me llegó tu screenshot de hoy. "
                "La ventana cierra a medianoche (hora Buenos Aires). Mandámelo cuando puedas!"
            )
            if ok:
                sent += 1
    logger.info("[liga] lembrete diário enviado para %d leads ativos", sent)


async def task_daily_reset() -> None:
    """00h01 (BA) — vira o dia.

    Antes de zerar `proof_sent_today`, quem está com `proof_sent_today=False`
    PERDEU ontem (a janela fechou sem prova) → reseta `streak_days=0`.
    Isso é o que move o lead pra tag 'slipping' no remarketing.
    """
    streak_reset = 0
    with SessionLocal() as session:
        # 1) Quem não enviou ontem → perde a sequência
        missed = (
            session.query(Lead)
            .filter(Lead.liga_state.in_([
                LigaState.ACTIVE.value,
                LigaState.AT_RISK.value,
            ]))
            .filter(Lead.proof_sent_today.is_(False))
            .filter(Lead.streak_days > 0)
            .all()
        )
        for lead in missed:
            logger.info(
                "[liga] streak zerado lead=%s (perdeu ontem, streak_was=%d)",
                lead.display_name, lead.streak_days or 0,
            )
            lead.streak_days = 0
            streak_reset += 1

        # 2) Vira o dia: zera proof_sent_today pra todos os ativos/em-risco
        reset_count = (
            session.query(Lead)
            .filter(Lead.liga_state.in_([
                LigaState.ACTIVE.value,
                LigaState.AT_RISK.value,
            ]))
            .update({Lead.proof_sent_today: False}, synchronize_session=False)
        )
        session.commit()
    logger.info(
        "[liga] reset diário: %d leads novo-dia, %d perderam streak",
        reset_count, streak_reset,
    )


async def task_checkpoint(checkpoint_num: int, is_final: bool = False) -> None:
    """Executa checkpoint da Liga.

    - is_final=False: leads com banca < $100 viram at_risk.
      Quem JÁ estava at_risk no checkpoint anterior é eliminado.
    - is_final=True: todos com banca < $100 são eliminados.
    """
    eliminated = 0
    at_risk = 0
    with SessionLocal() as session:
        candidatos = (
            session.query(Lead)
            .filter(Lead.liga_state.in_([
                LigaState.ACTIVE.value,
                LigaState.AT_RISK.value,
            ]))
            .filter((Lead.liga_balance == None) | (Lead.liga_balance < 100))  # noqa: E711
            .all()
        )
        for lead in candidatos:
            nome = lead.first_name or lead.display_name
            bal = lead.liga_balance or 0.0
            if is_final:
                lead.liga_state = LigaState.ELIMINATED.value
                lead.last_bot_action = f"checkpoint_{checkpoint_num}_final_eliminated"
                eliminated += 1
                await _send_dm(
                    lead.telegram_id,
                    "Lamentablemente quedaste afuera del ranking de la Liga por estar "
                    "abajo de $100 en el checkpoint de hoy. Gracias por participar! "
                    "Te esperamos en la próxima 🤝"
                )
                continue

            if lead.liga_state == LigaState.AT_RISK.value:
                # Já estava em risco no checkpoint anterior → eliminar
                lead.liga_state = LigaState.ELIMINATED.value
                lead.last_bot_action = f"checkpoint_{checkpoint_num}_eliminated"
                eliminated += 1
                await _send_dm(
                    lead.telegram_id,
                    "Lamentablemente quedaste afuera del ranking de la Liga por estar "
                    "abajo de $100 en el checkpoint de hoy. Gracias por participar! "
                    "Te esperamos en la próxima 🤝"
                )
            else:
                lead.liga_state = LigaState.AT_RISK.value
                lead.last_bot_action = f"checkpoint_{checkpoint_num}_at_risk"
                at_risk += 1
                await _send_dm(
                    lead.telegram_id,
                    f"⚠️ Atención *{nome}*! Tu banca está en *${bal:.2f}*, "
                    "abajo de los $100 mínimos. Tenés hasta el próximo checkpoint para recuperar. "
                    "Si no, te sacamos del ranking."
                )
        session.commit()
    logger.info(
        "[liga] checkpoint %d (final=%s): at_risk=%d eliminated=%d",
        checkpoint_num, is_final, at_risk, eliminated,
    )


async def task_weekly_report() -> None:
    """Segunda 09h00 (BA) — relatório semanal para ADMIN_TELEGRAM_ID."""
    admin = os.getenv("ADMIN_TELEGRAM_ID", "").strip()
    if not admin:
        logger.info("[liga] ADMIN_TELEGRAM_ID não configurado; pulando relatório")
        return

    with SessionLocal() as session:
        ativos = (
            session.query(func.count(Lead.id))
            .filter(Lead.liga_state == LigaState.ACTIVE.value)
            .scalar() or 0
        )
        em_risco = (
            session.query(func.count(Lead.id))
            .filter(Lead.liga_state == LigaState.AT_RISK.value)
            .scalar() or 0
        )
        volume_total = (
            session.query(func.sum(DailyVolume.volume_usd)).scalar() or 0.0
        )
        top_rows = (
            session.query(
                Lead.id, Lead.first_name, Lead.username,
                func.coalesce(func.sum(DailyVolume.volume_usd), 0.0).label("vol"),
            )
            .outerjoin(DailyVolume, DailyVolume.lead_id == Lead.id)
            .filter(Lead.liga_state.in_([
                LigaState.ACTIVE.value,
                LigaState.AT_RISK.value,
                LigaState.FINALIST.value,
            ]))
            .group_by(Lead.id)
            .order_by(func.coalesce(func.sum(DailyVolume.volume_usd), 0.0).desc())
            .limit(5)
            .all()
        )

    lines = [
        "📊 *Relatório Semanal — Liga*",
        "",
        f"• Ativos: *{ativos}*",
        f"• Em risco: *{em_risco}*",
        f"• Volume total acumulado: *${volume_total:,.2f}*",
        "",
        "🏅 *Top 5 por volume*:",
    ]
    if not top_rows:
        lines.append("_Sem dados ainda._")
    else:
        for i, row in enumerate(top_rows, 1):
            nome = row.first_name or (f"@{row.username}" if row.username else f"id:{row.id}")
            lines.append(f"{i}. {nome} — ${float(row.vol or 0):,.2f}")

    await _send_to_target(admin, "\n".join(lines))
    logger.info("[liga] relatório semanal enviado para %s", admin)


async def task_daily_ranking() -> None:
    """22h00 (BA) — ranking diário para LIGA_GROUP."""
    target = os.getenv("LIGA_GROUP", "").strip()
    if not target:
        logger.info("[liga] LIGA_GROUP não configurado; pulando ranking")
        return

    with SessionLocal() as session:
        rows = (
            session.query(
                Lead.id, Lead.first_name, Lead.username,
                func.coalesce(func.sum(DailyVolume.volume_usd), 0.0).label("vol"),
            )
            .outerjoin(DailyVolume, DailyVolume.lead_id == Lead.id)
            .filter(Lead.liga_state.in_([
                LigaState.ACTIVE.value,
                LigaState.AT_RISK.value,
                LigaState.FINALIST.value,
            ]))
            .group_by(Lead.id)
            .order_by(func.coalesce(func.sum(DailyVolume.volume_usd), 0.0).desc())
            .limit(10)
            .all()
        )
        total = (
            session.query(func.sum(DailyVolume.volume_usd)).scalar() or 0.0
        )

    medals = ["🥇", "🥈", "🥉"] + [f"#{i}" for i in range(4, 11)]
    lines = ["🏆 *Ranking del día — Liga*", ""]
    if not rows:
        lines.append("_Todavía no hay operaciones registradas._")
    else:
        for i, row in enumerate(rows):
            nome = row.first_name or (f"@{row.username}" if row.username else f"id:{row.id}")
            lines.append(f"{medals[i]} {nome} — ${float(row.vol or 0):,.2f}")

    target_million = 1_000_000.0
    bar = _progress_bar(total, target_million)
    pct = (total / target_million * 100) if target_million else 0.0
    lines += [
        "",
        "🎯 *Meta $1M*",
        f"{bar} {pct:.1f}%",
        f"Acumulado: *${total:,.2f}*",
    ]
    await _send_to_target(target, "\n".join(lines))
    logger.info("[liga] ranking diário enviado para %s", target)


# ---------------------------------------------------------------------------
# Inicialização
# ---------------------------------------------------------------------------
def start_liga_scheduler(client) -> AsyncIOScheduler:
    """Inicia o AsyncIOScheduler com todas as tarefas da Liga.

    Recebe o TelegramClient já conectado. Idempotente: se já houver scheduler,
    devolve o existente.
    """
    global _scheduler, _client
    _client = client

    if _scheduler is not None and _scheduler.running:
        logger.info("[liga] scheduler já em execução")
        return _scheduler

    sched = AsyncIOScheduler(timezone=BA_TZ_NAME)

    # Lembrete diário 21h00 (BA)
    sched.add_job(
        task_daily_reminder,
        CronTrigger(hour=21, minute=0, timezone=BA_TZ_NAME),
        id="liga_daily_reminder",
        replace_existing=True,
    )

    # Reset diário 00h01 (BA)
    sched.add_job(
        task_daily_reset,
        CronTrigger(hour=0, minute=1, timezone=BA_TZ_NAME),
        id="liga_daily_reset",
        replace_existing=True,
    )

    # Checkpoints — datas calculadas de LIGA_START_DATE / LIGA_END_DATE
    # CP1=start+6d, CP2=start+13d, CP3=start+20d (08h00 BA)
    # Final=end (23h55 BA)
    start_str = os.getenv("LIGA_START_DATE", "").strip()
    end_str = os.getenv("LIGA_END_DATE", "").strip()

    def _parse_or_none(s: str) -> Optional[datetime]:
        if not s:
            return None
        try:
            d = datetime.strptime(s, "%Y-%m-%d")
            if BA_TZ:
                d = d.replace(tzinfo=BA_TZ)
            return d
        except Exception:
            logger.warning("[liga] data inválida em .env: %r (use YYYY-MM-DD)", s)
            return None

    start_dt = _parse_or_none(start_str)
    end_dt = _parse_or_none(end_str)

    checkpoints = []
    if start_dt:
        checkpoints += [
            ("liga_checkpoint_1", start_dt + timedelta(days=6),  {"checkpoint_num": 1, "is_final": False}),
            ("liga_checkpoint_2", start_dt + timedelta(days=13), {"checkpoint_num": 2, "is_final": False}),
            ("liga_checkpoint_3", start_dt + timedelta(days=20), {"checkpoint_num": 3, "is_final": False}),
        ]
        # CP1-3 às 08h00
        checkpoints = [
            (cid, dt.replace(hour=8, minute=0, second=0), kw)
            for cid, dt, kw in checkpoints
        ]
    if end_dt:
        cpf_dt = end_dt.replace(hour=23, minute=55, second=0)
        checkpoints.append(("liga_checkpoint_final", cpf_dt, {"checkpoint_num": 4, "is_final": True}))

    now_ba = datetime.now(BA_TZ) if BA_TZ else datetime.now()
    for cid, when, kwargs in checkpoints:
        if when <= now_ba:
            logger.warning(
                "[liga] %s já passou (%s) — pulando. Atualize LIGA_START_DATE/LIGA_END_DATE no .env.",
                cid, when.strftime("%Y-%m-%d %H:%M %z"),
            )
            continue
        sched.add_job(
            task_checkpoint,
            DateTrigger(run_date=when),
            id=cid, replace_existing=True,
            kwargs=kwargs,
            misfire_grace_time=3600,  # 1h de tolerância pra reinícios
            coalesce=True,             # se atrasar, roda só 1x
        )
        logger.info("[liga] %s agendado para %s", cid, when.strftime("%Y-%m-%d %H:%M %z"))

    # Relatório semanal — segunda 09h00 BA (Mon=0)
    sched.add_job(
        task_weekly_report,
        CronTrigger(day_of_week="mon", hour=9, minute=0, timezone=BA_TZ_NAME),
        id="liga_weekly_report",
        replace_existing=True,
    )

    # Ranking diário 22h00 BA
    sched.add_job(
        task_daily_ranking,
        CronTrigger(hour=22, minute=0, timezone=BA_TZ_NAME),
        id="liga_daily_ranking",
        replace_existing=True,
    )

    # ----- Automações novas (do roadmap) -----------------------------------
    from .automation import (
        task_daily_backup,
        task_daily_digest,
        task_weekly_revalidation,
        task_recalculate_vips,
        task_run_follow_ups,
        task_incremental_dm_scan,
    )

    # Scan incremental — a cada 5 minutos, varre DMs novas pra extrair IDs
    sched.add_job(
        task_incremental_dm_scan,
        CronTrigger(minute="*/5", timezone=BA_TZ_NAME),
        id="auto_incremental_dm_scan",
        replace_existing=True,
        misfire_grace_time=120,
        coalesce=True,
        max_instances=1,  # nunca rodar 2 ao mesmo tempo
        kwargs={"client": _client},
    )

    # Export Obsidian — diário 23h00 BA (depois do digest interno)
    from .obsidian_export import export_all_leads, export_daily_insight
    def _obsidian_nightly():
        export_all_leads(only_active=False)
        export_daily_insight()
    sched.add_job(
        _obsidian_nightly,
        CronTrigger(hour=23, minute=0, timezone=BA_TZ_NAME),
        id="auto_obsidian_export",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )

    # Análise contextual semanal via Haiku — domingo 04h00 BA
    from .contextual_analysis import task_weekly_contextual_analysis
    sched.add_job(
        task_weekly_contextual_analysis,
        CronTrigger(day_of_week="sun", hour=4, minute=0, timezone=BA_TZ_NAME),
        id="auto_contextual_analysis",
        replace_existing=True,
        misfire_grace_time=7200,
        coalesce=True,
    )

    # Análise mensal de razões de não-deposit (dia 1 às 06h00 BA)
    from .no_deposit_analysis import task_analyze_no_deposit_reasons
    sched.add_job(
        task_analyze_no_deposit_reasons,
        CronTrigger(day=1, hour=6, minute=0, timezone=BA_TZ_NAME),
        id="auto_no_deposit_analysis",
        replace_existing=True,
        misfire_grace_time=86400,
        coalesce=True,
    )

    # Cooldown advance — a cada hora
    # Avança stages (R1_cooldown→R1_cold, etc), recalcula is_fresh, descarta R3 antigos
    from .remarketing_stage import task_cooldown_advance
    sched.add_job(
        task_cooldown_advance,
        CronTrigger(minute=15, timezone=BA_TZ_NAME),  # roda no min 15 de cada hora
        id="auto_cooldown_advance",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
    )

    # Check membros do grupo privado — a cada 10 minutos
    # (detecta quem entrou via link, caso o listener ChatAction tenha perdido)
    from .automation import task_check_private_group_members
    sched.add_job(
        task_check_private_group_members,
        CronTrigger(minute="*/10", timezone=BA_TZ_NAME),
        id="auto_group_members_check",
        replace_existing=True,
        misfire_grace_time=300,
        coalesce=True,
        max_instances=1,
        kwargs={"client": _client},
    )

    # ═══ APRENDIZADO PERIÓDICO ═══════════════════════════════════════════
    # A cada 6 horas, escaneia DMs reais dos últimos 7 dias pra capturar
    # frases novas que apareceram. Auto-aprendizado no classifier já pega
    # cada msg em tempo real (atalho), mas esse scan retroativo agrupa
    # padrões e atualiza counts de frequência. Custo: ~$0.01/scan.
    def _periodic_learn_scan():
        try:
            from liga.funnel.learn_intents import scan_vip_intent_examples
            result = scan_vip_intent_examples(
                days_back=7,
                max_messages=500,
                use_ai_validation=True,
            )
            logger.info(
                "[learn_intents] scan periódico: scanned=%d confirmadas=%d padrões=%d custo=$%.4f",
                result.get("scanned_messages", 0),
                result.get("confirmed_by_ai", 0),
                result.get("unique_patterns", 0),
                result.get("cost_estimate_usd", 0),
            )
        except Exception:
            logger.exception("[learn_intents] erro no scan periódico")

    sched.add_job(
        _periodic_learn_scan,
        CronTrigger(hour="*/6", minute="30", timezone=BA_TZ_NAME),
        id="auto_learn_scan",
        replace_existing=True,
        misfire_grace_time=900,
        coalesce=True,
        max_instances=1,
    )

    # Backup diário 01h00 BA
    sched.add_job(
        task_daily_backup,
        CronTrigger(hour=1, minute=0, timezone=BA_TZ_NAME),
        id="auto_daily_backup",
        replace_existing=True,
        misfire_grace_time=3600,
        kwargs={"client": _client},
    )

    # Backup extra durante torneio — a cada 6h (07h, 13h, 19h BA)
    # task_daily_backup checa is_tournament_active() e só roda se sim
    from .automation import task_tournament_backup
    sched.add_job(
        task_tournament_backup,
        CronTrigger(hour="7,13,19", minute=0, timezone=BA_TZ_NAME),
        id="auto_tournament_backup",
        replace_existing=True,
        misfire_grace_time=1800,
        kwargs={"client": _client},
    )

    # Daily digest 2x ao dia durante torneio (12h00 BA além do 08h00)
    sched.add_job(
        task_daily_digest,
        CronTrigger(hour=12, minute=0, timezone=BA_TZ_NAME),
        id="auto_tournament_midday_digest",
        replace_existing=True,
        misfire_grace_time=1800,
        kwargs={"client": _client, "tournament_only": True},
    )

    # Daily digest pro admin 08h00 BA
    sched.add_job(
        task_daily_digest,
        CronTrigger(hour=8, minute=0, timezone=BA_TZ_NAME),
        id="auto_daily_digest",
        replace_existing=True,
        misfire_grace_time=3600,
        kwargs={"client": _client},
    )

    # Re-validação semanal — domingo 03h00 BA
    sched.add_job(
        task_weekly_revalidation,
        CronTrigger(day_of_week="sun", hour=3, minute=0, timezone=BA_TZ_NAME),
        id="auto_weekly_revalidation",
        replace_existing=True,
        misfire_grace_time=7200,
        coalesce=True,
        kwargs={"client": _client},
    )

    # Validação contínua de IDs pendentes — a cada hora (min 30)
    # Pega leads com liga_account_id mas status='extracted'/'needs_review' e tenta
    # validar no @QuotexPartnerBot. Cobre gap onde scan inicial atingiu cap.
    from .automation import task_validate_pending_ids
    sched.add_job(
        task_validate_pending_ids,
        CronTrigger(minute=30, timezone=BA_TZ_NAME),
        id="auto_validate_pending_ids",
        replace_existing=True,
        misfire_grace_time=600,
        coalesce=True,
        max_instances=1,
        kwargs={"client": _client},
    )

    # Rescue de sends presos em queue — a cada 5 min
    # Pega Send.status='queued' há > 10min de campanhas não-canceladas e processa
    from .automation import task_rescue_stuck_sends
    sched.add_job(
        task_rescue_stuck_sends,
        CronTrigger(minute="*/5", timezone=BA_TZ_NAME),
        id="auto_rescue_stuck_sends",
        replace_existing=True,
        misfire_grace_time=120,
        coalesce=True,
        max_instances=1,
    )

    # Consolidação do vault do agente passivo — diário 03h30 BA
    # Pega exemplos novos (in_vault=False) e escreve nos arquivos MD
    from .agent.learning import consolidate_vault as _consolidate_vault_task
    def _agent_vault_nightly():
        try:
            _consolidate_vault_task()
        except Exception:
            logger.exception("[agent vault] erro na consolidação noturna")
    sched.add_job(
        _agent_vault_nightly,
        CronTrigger(hour=3, minute=30, timezone=BA_TZ_NAME),
        id="auto_agent_vault_consolidate",
        replace_existing=True,
        misfire_grace_time=3600,
        coalesce=True,
    )

    # Recalcular VIPs — diário 02h00 BA
    sched.add_job(
        task_recalculate_vips,
        CronTrigger(hour=2, minute=0, timezone=BA_TZ_NAME),
        id="auto_recalc_vips",
        replace_existing=True,
        misfire_grace_time=3600,
    )

    # Follow-ups automáticos — diário 10h00 BA
    sched.add_job(
        task_run_follow_ups,
        CronTrigger(hour=10, minute=0, timezone=BA_TZ_NAME),
        id="auto_follow_ups",
        replace_existing=True,
        misfire_grace_time=3600,
        kwargs={"client": _client},
    )

    sched.start()
    _scheduler = sched
    logger.info("[liga] scheduler iniciado com %d jobs (TZ=%s)", len(sched.get_jobs()), BA_TZ_NAME)
    return sched


def stop_liga_scheduler() -> None:
    global _scheduler
    if _scheduler is not None and _scheduler.running:
        _scheduler.shutdown(wait=False)
    _scheduler = None
