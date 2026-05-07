"""Agendamento de campanhas com APScheduler.

Antes de cada campanha:
1. Refresh dos membros do grupo privado (pra atualizar quem virou pago).
2. Marca como EXCLUDED quem está no grupo privado.
3. Filtra leads por target_status.

Fuso horário: America/Sao_Paulo (BRT). Quando você agenda "15:00" no painel,
significa 15:00 horário de Brasília.
"""
from __future__ import annotations

import logging
from datetime import datetime
from typing import Optional, Set
from zoneinfo import ZoneInfo

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.date import DateTrigger

BRT = ZoneInfo("America/Sao_Paulo")

from db import SessionLocal
from db.models import (
    Campaign,
    CampaignStatus,
    Lead,
    LeadStatus,
    Script,
    ScriptVariant,
    Send,
    SendStatus,
)
from .leads import get_private_group_member_ids
from .sender import execute_send_record

logger = logging.getLogger(__name__)

scheduler = AsyncIOScheduler(timezone=BRT)


def _now_brt_naive() -> datetime:
    """Hora atual em Brasília, naive (pra gravar no banco)."""
    return datetime.now(BRT).replace(tzinfo=None)


def _select_variant(script: Script, strategy: str, used_so_far: int) -> Optional[ScriptVariant]:
    actives = [v for v in script.variants if v.is_active]
    if not actives:
        return None
    if strategy == "best":
        scored = sorted(actives, key=lambda v: v.score(), reverse=True)
        if scored[0].sends_count > 0:
            return scored[0]
    return actives[used_so_far % len(actives)]


async def _refresh_private_group_exclusions() -> Set[int]:
    """Re-puxa membros do grupo privado e atualiza Lead.in_private_group + status.
    Retorna o conjunto de IDs no grupo privado.
    Em caso de erro (rede etc.), retorna conjunto vazio MAS NÃO falha.
    """
    try:
        member_ids = await get_private_group_member_ids()
    except Exception as e:
        logger.error("Falha ao refresh do grupo privado: %s. Usando dados atuais do banco.", e)
        return set()

    with SessionLocal() as session:
        # Marca como in_private_group=True quem aparece, e EXCLUDED se ainda não estava
        for lid in member_ids:
            lead = session.query(Lead).filter_by(telegram_id=lid).one_or_none()
            if lead:
                lead.in_private_group = True
                if lead.status != LeadStatus.EXCLUDED.value:
                    lead.status = LeadStatus.EXCLUDED.value
        # Quem não aparece mas tinha in_private_group=True: NÃO mexer (pode ser
        # erro temporário; melhor manter excluído por segurança).
        session.commit()

    logger.info("Grupo privado refresh: %d membros (todos excluídos do envio).", len(member_ids))
    return member_ids


async def run_campaign(campaign_id: int) -> None:
    logger.info("Iniciando campanha %s", campaign_id)

    # PASSO 1: Refresh do grupo privado ANTES de gerar a fila
    private_member_ids = await _refresh_private_group_exclusions()

    with SessionLocal() as session:
        campaign = session.query(Campaign).get(campaign_id)
        if not campaign:
            logger.error("Campanha %s não encontrada", campaign_id)
            return
        if campaign.status not in (
            CampaignStatus.SCHEDULED.value, CampaignStatus.DRAFT.value,
        ):
            logger.warning("Campanha %s já está em status %s", campaign_id, campaign.status)
            return

        script = session.query(Script).get(campaign.script_id)
        if not script or not script.is_active:
            logger.error("Script inválido pra campanha %s", campaign_id)
            campaign.status = CampaignStatus.CANCELLED.value
            session.commit()
            return

        mode = (script.mode or "forward").lower()

        if mode == "forward":
            if not script.sources:
                logger.error("Script %s sem mensagens fonte", script.id)
                campaign.status = CampaignStatus.CANCELLED.value
                session.commit()
                return
        else:  # ai
            if not [v for v in script.variants if v.is_active]:
                logger.error("Script %s sem variantes ativas", script.id)
                campaign.status = CampaignStatus.CANCELLED.value
                session.commit()
                return

        target_status = campaign.target_status or LeadStatus.PENDING.value
        # Filtros DE SEGURANÇA:
        # 1) Status = target_status (não pega EXCLUDED, BLOCKED, CONVERTED)
        # 2) in_private_group = False (extra)
        # 3) Exclui qualquer telegram_id que apareceu no refresh (extra)
        # 4) Exclui VIPs por default (recebem tratamento manual via /vip-outreach)
        q = (
            session.query(Lead)
            .filter(Lead.in_private_group.is_(False))
            .filter(Lead.status == target_status)
        )
        if private_member_ids:
            q = q.filter(~Lead.telegram_id.in_(list(private_member_ids)))
        # Excluir VIPs do disparo em massa (default True)
        if getattr(campaign, "exclude_vips", True):
            q = q.filter(
                (Lead.is_vip_potential.is_(False)) | (Lead.is_vip_potential.is_(None))
            )

        # Excluir leads marcados manualmente em /scripts/{id}/preview-dispatch
        try:
            from db.models import ScriptExcludedLead
            excluded_ids = [
                row[0] for row in session.query(ScriptExcludedLead.lead_id)
                .filter(ScriptExcludedLead.script_id == script.id).all()
            ]
            if excluded_ids:
                q = q.filter(~Lead.id.in_(excluded_ids))
                logger.info("Campanha %s: %d leads excluídos manualmente do script", campaign_id, len(excluded_ids))
        except Exception:
            logger.debug("erro filtrando excluídos manuais", exc_info=True)

        # Ordena por last_dm_at DESC (leads mais recentes primeiro) — consistente
        # com a ordenação mostrada em /scripts/{id}/preview-dispatch
        from sqlalchemy import desc as _desc
        q = q.order_by(_desc(Lead.last_dm_at))

        if campaign.max_leads and campaign.max_leads > 0:
            q = q.limit(campaign.max_leads)
        leads = q.all()

        if not leads:
            logger.warning("Campanha %s sem leads alvo", campaign_id)
            campaign.status = CampaignStatus.COMPLETED.value
            campaign.started_at = _now_brt_naive()
            campaign.completed_at = _now_brt_naive()
            session.commit()
            return

        send_ids = []
        for i, lead in enumerate(leads):
            variant_id = None
            if mode == "ai":
                variant = _select_variant(
                    script, campaign.variant_strategy or "rotate", i,
                )
                if variant:
                    variant_id = variant.id
            send = Send(
                campaign_id=campaign.id,
                lead_id=lead.id,
                script_id=script.id,
                variant_id=variant_id,
                status=SendStatus.QUEUED.value,
            )
            session.add(send)
            session.flush()
            send_ids.append(send.id)

        campaign.status = CampaignStatus.RUNNING.value
        campaign.started_at = _now_brt_naive()
        session.commit()

    logger.info("Campanha %s: %d sends na fila, começando envio", campaign_id, len(send_ids))

    sent = failed = skipped = 0
    cancelled_mid_run = False

    for idx, sid in enumerate(send_ids):
        # 🛑 CHECAGEM DE CANCELAMENTO antes de cada send
        with SessionLocal() as session:
            c = session.query(Campaign).get(campaign_id)
            if c and c.status == CampaignStatus.CANCELLED.value:
                cancelled_mid_run = True
                # Marca os sends restantes como SKIPPED
                remaining = send_ids[idx:]
                session.query(Send).filter(
                    Send.id.in_(remaining),
                    Send.status == SendStatus.QUEUED.value,
                ).update(
                    {Send.status: SendStatus.SKIPPED.value,
                     Send.error: "campanha cancelada pelo usuário"},
                    synchronize_session=False,
                )
                session.commit()
                logger.warning(
                    "🛑 CAMPANHA %s CANCELADA — %d sends pulados",
                    campaign_id, len(remaining),
                )
                break

        try:
            res = await execute_send_record(sid)
            if res.success:
                sent += 1
            elif res.skipped_reason:
                skipped += 1
            else:
                failed += 1
        except Exception:
            logger.exception("Erro no send %s", sid)
            failed += 1

    with SessionLocal() as session:
        campaign = session.query(Campaign).get(campaign_id)
        if campaign:
            if cancelled_mid_run:
                # mantém CANCELLED
                campaign.completed_at = _now_brt_naive()
            else:
                campaign.status = CampaignStatus.COMPLETED.value
                campaign.completed_at = _now_brt_naive()
            session.commit()

    logger.info(
        "Campanha %s %s: %d enviados, %d skipped, %d falhas",
        campaign_id,
        "CANCELADA" if cancelled_mid_run else "finalizada",
        sent, skipped, failed,
    )


def schedule_campaign(campaign_id: int, when: Optional[datetime] = None) -> str:
    """Agenda uma campanha. Se `when` for naive (sem timezone), assume BRT.
    Banco guarda em horário de Brasília (naive BRT) pra display direto sem conversão.
    """
    job_id = f"campaign-{campaign_id}"
    if when is None:
        when = datetime.now(BRT)
    elif when.tzinfo is None:
        when = when.replace(tzinfo=BRT)
    trigger = DateTrigger(run_date=when, timezone=BRT)
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    scheduler.add_job(
        run_campaign,
        trigger=trigger,
        args=[campaign_id],
        id=job_id,
        replace_existing=True,
        misfire_grace_time=3600,
    )
    with SessionLocal() as session:
        campaign = session.query(Campaign).get(campaign_id)
        if campaign:
            campaign.status = CampaignStatus.SCHEDULED.value
            # Grava em BRT naive — o que aparece no banco é o que aparece pro usuário
            campaign.scheduled_at = when.astimezone(BRT).replace(tzinfo=None)
            session.commit()
    return job_id


def cancel_campaign(campaign_id: int) -> bool:
    """Cancela uma campanha. Funciona em DRAFT, SCHEDULED E RUNNING.
    Em RUNNING, o run_campaign vê o status e aborta antes do próximo send.
    """
    job_id = f"campaign-{campaign_id}"
    if scheduler.get_job(job_id):
        scheduler.remove_job(job_id)
    with SessionLocal() as session:
        campaign = session.query(Campaign).get(campaign_id)
        if campaign and campaign.status in (
            CampaignStatus.SCHEDULED.value,
            CampaignStatus.DRAFT.value,
            CampaignStatus.RUNNING.value,  # NOVO: também cancela RUNNING
        ):
            campaign.status = CampaignStatus.CANCELLED.value
            session.commit()
            return True
    return False


def cancel_all_campaigns() -> dict:
    """🛑 PARADA DE EMERGÊNCIA — cancela TODAS as campanhas ativas.
    Usado quando você precisa parar tudo IMEDIATAMENTE.
    """
    cancelled_count = 0
    cancelled_ids = []
    with SessionLocal() as session:
        active = session.query(Campaign).filter(
            Campaign.status.in_([
                CampaignStatus.SCHEDULED.value,
                CampaignStatus.DRAFT.value,
                CampaignStatus.RUNNING.value,
            ])
        ).all()
        for c in active:
            c.status = CampaignStatus.CANCELLED.value
            cancelled_count += 1
            cancelled_ids.append(c.id)
        session.commit()

    # Remove jobs do scheduler
    for cid in cancelled_ids:
        job_id = f"campaign-{cid}"
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)

    logger.warning("🛑 PARADA DE EMERGÊNCIA: %d campanhas canceladas", cancelled_count)
    return {"cancelled": cancelled_count, "ids": cancelled_ids}
