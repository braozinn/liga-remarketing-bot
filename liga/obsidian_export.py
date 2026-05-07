"""Exporta leads pra arquivos markdown da Obsidian vault.

Estratégia:
- Front-matter YAML pros dados estruturados (bot lê/escreve livremente)
- Seções marcadas com headers (## 🧠 Análise IA / ## 📝 Suas notas)
- Bot só toca em seções "dele" — preserva a seção "Suas notas" intocada
- Atomic write: grava em .tmp, depois rename (evita corrupção)
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from sqlalchemy import desc, func

from db import SessionLocal
from db.models import (
    DailyVolume, Lead, LeadStatus, OperationProof, Send, SendStatus,
)

logger = logging.getLogger(__name__)


# Seção que o bot NUNCA toca — só você edita
HUMAN_NOTES_HEADER = "## 📝 Suas notas"
HUMAN_NOTES_PLACEHOLDER = (
    "*Espaço livre — anote o que quiser sobre esse lead. "
    "Bot nunca apaga essa seção.*\n"
)


def _vault_path() -> Optional[Path]:
    """Retorna o caminho da vault ou None se não configurada."""
    raw = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.exists():
        logger.warning("[obsidian] OBSIDIAN_VAULT_PATH=%s não existe", raw)
        return None
    return p


def _safe_filename(s: str) -> str:
    """Sanitiza nome de arquivo (Windows + Unix)."""
    if not s:
        return "unknown"
    # Remove chars inválidos em filesystems
    s = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", s)
    # Remove acentos / extras de espaços
    s = re.sub(r"\s+", " ", s).strip()
    return s[:80] or "unknown"


def _lead_filename(lead: Lead) -> str:
    """Gera o nome do .md pro lead. Padrão: '@username (id_conta).md' ou fallback."""
    handle = f"@{lead.username}" if lead.username else f"id-{lead.telegram_id}"
    name = (lead.first_name or "").strip() or "?"
    if lead.last_name:
        name += f" {lead.last_name.strip()}"
    id_part = f" ({lead.liga_account_id})" if lead.liga_account_id else ""
    raw = f"{handle} - {name}{id_part}"
    return _safe_filename(raw) + ".md"


def _extract_human_notes(existing_md: str) -> str:
    """Extrai a seção '📝 Suas notas' de um .md existente.

    Retorna o conteúdo APÓS o header (até o próximo header) ou string vazia.
    """
    if not existing_md or HUMAN_NOTES_HEADER not in existing_md:
        return ""
    parts = existing_md.split(HUMAN_NOTES_HEADER, 1)
    if len(parts) < 2:
        return ""
    after = parts[1]
    # Pega até o próximo header de mesmo nível ou fim
    next_header = re.search(r"\n## ", after)
    if next_header:
        return after[:next_header.start()].strip()
    return after.strip()


def _load_messages_summary(session, lead_id: int, limit: int = 10) -> list[dict]:
    """Pega últimos sends/replies pra mostrar 'últimas mensagens' resumidamente."""
    sends = (
        session.query(Send)
        .filter(Send.lead_id == lead_id)
        .filter(Send.status == SendStatus.SENT.value)
        .order_by(desc(Send.sent_at))
        .limit(limit)
        .all()
    )
    items = []
    for s in sends:
        items.append({
            "direction": "out",
            "date": s.sent_at,
            "text": (s.message_text or "[mensagem encaminhada]")[:200],
            "replied": s.replied,
            "reply_text": (s.reply_text or "")[:200] if s.replied else None,
            "reply_classification": s.reply_classification,
            "reply_at": s.replied_at,
        })
    return items


def _liga_journey(session, lead_id: int) -> list[str]:
    """Pega timeline da jornada Liga: 1ºcontato → ID → screenshot → ..."""
    items = []
    # Sends ordenados
    sends = (
        session.query(Send)
        .filter(Send.lead_id == lead_id)
        .order_by(Send.sent_at.asc())
        .all()
    )
    for s in sends:
        if s.sent_at:
            items.append(f"- {s.sent_at.strftime('%Y-%m-%d')}: script enviado (id={s.script_id})")
        if s.replied and s.replied_at:
            cls = s.reply_classification or "neutro"
            items.append(f"- {s.replied_at.strftime('%Y-%m-%d')}: respondeu ({cls})")
    # Proofs
    proofs = (
        session.query(OperationProof)
        .filter(OperationProof.lead_id == lead_id)
        .order_by(OperationProof.created_at.asc())
        .all()
    )
    for p in proofs:
        if p.created_at:
            tag = "✓" if p.validated else "✗"
            items.append(
                f"- {p.created_at.strftime('%Y-%m-%d')}: {tag} comprovante "
                f"(${p.volume_usd or 0:.2f}, conf={p.confidence})"
            )
    return items


def _render_lead_md(lead: Lead, existing_md: str, session) -> str:
    """Gera o conteúdo markdown completo do lead, preservando 'Suas notas'."""
    notes_existing = _extract_human_notes(existing_md)

    # YAML frontmatter
    fm_lines = [
        "---",
        f"lead_id: {lead.id}",
        f"telegram_id: {lead.telegram_id}",
    ]
    if lead.username:
        fm_lines.append(f"username: {lead.username}")
    fm_lines.append(f"status: {lead.status or '?'}")
    if lead.engagement_tag:
        fm_lines.append(f"engagement_tag: {lead.engagement_tag}")
    if lead.liga_state and lead.liga_state != "new":
        fm_lines.append(f"liga_state: {lead.liga_state}")
    if lead.liga_account_id:
        fm_lines.append(f"liga_account_id: '{lead.liga_account_id}'")
    if lead.liga_id_status:
        fm_lines.append(f"liga_id_status: {lead.liga_id_status}")
    if lead.liga_id_country:
        fm_lines.append(f"country: {lead.liga_id_country}")
    if lead.liga_id_balance is not None:
        fm_lines.append(f"balance: {float(lead.liga_id_balance):.2f}")
    if lead.liga_id_deposits_sum is not None:
        fm_lines.append(f"deposits_sum: {float(lead.liga_id_deposits_sum):.2f}")
    if lead.liga_id_turnover is not None:
        fm_lines.append(f"turnover: {float(lead.liga_id_turnover):.2f}")
    if lead.is_vip_potential:
        fm_lines.append("is_vip_potential: true")
    if lead.rewarm_candidate:
        fm_lines.append("rewarm_candidate: true")
    if lead.opted_out:
        fm_lines.append("opted_out: true")
    fm_lines.append(f"updated_at: {datetime.utcnow().isoformat(timespec='seconds')}")

    # Tags do Obsidian (acessíveis via #tag)
    obs_tags = []
    if lead.engagement_tag:
        obs_tags.append(lead.engagement_tag.replace("_", "-"))
    if lead.liga_id_country:
        obs_tags.append(lead.liga_id_country.lower().replace(" ", "-"))
    if lead.is_vip_potential:
        obs_tags.append("vip")
    if lead.rewarm_candidate:
        obs_tags.append("rewarm")
    if lead.opted_out:
        obs_tags.append("opted-out")
    if obs_tags:
        fm_lines.append(f"tags: [{', '.join(obs_tags)}]")
    fm_lines.append("---")

    # Header principal
    nome = (lead.first_name or "") + (" " + lead.last_name if lead.last_name else "")
    nome = nome.strip() or "(sem nome)"
    handle = f"@{lead.username}" if lead.username else f"id:{lead.telegram_id}"
    title = f"# {handle} — {nome}"

    # Resumo rápido
    resumo_lines = []
    if lead.liga_account_id:
        resumo_lines.append(f"- **ID Quotex**: `{lead.liga_account_id}` ({lead.liga_id_status or 'sem validação'})")
    if lead.liga_id_country:
        resumo_lines.append(f"- **País**: {lead.liga_id_country}")
    if lead.liga_id_balance is not None:
        resumo_lines.append(f"- **Saldo real**: ${float(lead.liga_id_balance):,.2f}")
    if lead.liga_id_deposits_sum is not None and lead.liga_id_deposits_sum > 0:
        resumo_lines.append(f"- **Total depositado**: ${float(lead.liga_id_deposits_sum):,.2f}")
    if lead.liga_id_turnover is not None and lead.liga_id_turnover > 0:
        resumo_lines.append(f"- **Turnover**: ${float(lead.liga_id_turnover):,.2f}")
    if lead.engagement_tag:
        resumo_lines.append(f"- **Engajamento**: `{lead.engagement_tag}`")
    if lead.liga_state and lead.liga_state != "new":
        resumo_lines.append(f"- **Liga**: `{lead.liga_state}`")
    if lead.last_dm_at:
        resumo_lines.append(f"- **Última DM**: {lead.last_dm_at.strftime('%Y-%m-%d %H:%M')}")

    # Volume total acumulado (se há comprovantes)
    total_vol = (
        session.query(func.sum(DailyVolume.volume_usd))
        .filter(DailyVolume.lead_id == lead.id)
        .scalar()
        or 0.0
    )
    if total_vol > 0:
        resumo_lines.append(f"- **Volume acumulado**: ${float(total_vol):,.2f}")

    # Análise IA — placeholder se não tem
    ai_section = "## 🧠 Análise IA\n\n*Ainda sem análise contextual. Rode a função `analyze_lead_with_obsidian_context()` ou peça via /liga/lead.*"

    # Linha do tempo
    journey = _liga_journey(session, lead.id)
    journey_section = "## 📜 Linha do tempo\n\n"
    if journey:
        journey_section += "\n".join(journey[-30:])  # últimos 30 eventos
    else:
        journey_section += "*Sem eventos registrados.*"

    # Últimas mensagens
    msgs = _load_messages_summary(session, lead.id, limit=10)
    msgs_section = "## 💬 Últimas interações\n\n"
    if msgs:
        for m in msgs:
            d = m["date"].strftime("%Y-%m-%d %H:%M") if m["date"] else "?"
            msgs_section += f"- **{d}** — você enviou: _{m['text']}_\n"
            if m["replied"]:
                rd = m["reply_at"].strftime("%Y-%m-%d %H:%M") if m["reply_at"] else "?"
                msgs_section += f"  - **{rd}** — lead respondeu ({m['reply_classification']}): _{m['reply_text']}_\n"
    else:
        msgs_section += "*Nenhum envio/reply registrado.*"

    # Notas humanas (preserva)
    if notes_existing:
        notes_block = f"{HUMAN_NOTES_HEADER}\n\n{notes_existing}"
    else:
        notes_block = f"{HUMAN_NOTES_HEADER}\n\n{HUMAN_NOTES_PLACEHOLDER}"

    # Backlinks sugeridos
    related = []
    if lead.liga_id_country:
        related.append(f"#{lead.liga_id_country.lower().replace(' ', '-')}")
    if lead.engagement_tag:
        related.append(f"#{lead.engagement_tag.replace('_', '-')}")
    if lead.is_vip_potential:
        related.append("#vip")
    related_section = "## 🔗 Tags relacionadas\n\n" + (" ".join(related) if related else "_(sem tags)_")

    # Junta tudo
    parts = [
        "\n".join(fm_lines),
        title,
        "\n".join(resumo_lines) if resumo_lines else "*Sem dados estruturados.*",
        ai_section,
        journey_section,
        msgs_section,
        notes_block,
        related_section,
    ]
    return "\n\n".join(parts) + "\n"


def export_lead_to_md(lead_id: int) -> Optional[Path]:
    """Exporta um lead pra .md na vault. Retorna o Path ou None se vault desabilitada."""
    vault = _vault_path()
    if vault is None:
        return None

    leads_dir = vault / "leads"
    leads_dir.mkdir(parents=True, exist_ok=True)

    with SessionLocal() as session:
        lead = session.query(Lead).get(lead_id)
        if not lead:
            return None
        filename = _lead_filename(lead)
        target = leads_dir / filename

        # Lê existente pra preservar seção humana
        existing = ""
        if target.exists():
            try:
                existing = target.read_text(encoding="utf-8")
            except Exception:
                existing = ""

        # Renderiza
        content = _render_lead_md(lead, existing, session)

        # Atomic write: tmp + rename
        tmp = target.with_suffix(target.suffix + ".tmp")
        try:
            tmp.write_text(content, encoding="utf-8")
            tmp.replace(target)
        except Exception:
            logger.exception("[obsidian] falhou escrevendo %s", target)
            try:
                tmp.unlink()
            except Exception:
                pass
            return None

    return target


def export_all_leads(only_active: bool = False) -> dict:
    """Exporta todos os leads relevantes pra vault. Idempotente."""
    vault = _vault_path()
    if vault is None:
        return {"ok": False, "reason": "vault não configurada"}

    with SessionLocal() as session:
        q = session.query(Lead.id)
        if only_active:
            # Só leads que importam pra remarketing
            q = q.filter(
                Lead.status.notin_([
                    LeadStatus.BLOCKED.value,
                    LeadStatus.EXCLUDED.value,
                ])
            ).filter(Lead.in_private_group.is_(False))
        ids = [row[0] for row in q.all()]

    exported = 0
    failed = 0
    for lead_id in ids:
        try:
            r = export_lead_to_md(lead_id)
            if r:
                exported += 1
            else:
                failed += 1
        except Exception:
            logger.exception("[obsidian] erro exportando lead %s", lead_id)
            failed += 1

    return {
        "ok": True,
        "vault": str(vault),
        "total": len(ids),
        "exported": exported,
        "failed": failed,
    }


def export_daily_insight(extra_lines: list[str] = None) -> Optional[Path]:
    """Cria/atualiza insights/AAAA-MM-DD.md com snapshot do dia."""
    vault = _vault_path()
    if vault is None:
        return None
    insights_dir = vault / "insights"
    insights_dir.mkdir(parents=True, exist_ok=True)

    today = datetime.utcnow().strftime("%Y-%m-%d")
    target = insights_dir / f"{today}.md"

    yesterday = (datetime.utcnow() - timedelta(days=1)).date()
    today_d = datetime.utcnow().date()

    with SessionLocal() as s:
        new_leads = s.query(Lead).filter(
            Lead.created_at >= yesterday,
            Lead.created_at < today_d + timedelta(days=1),
        ).count()
        deposits_promised = s.query(Lead).filter(
            Lead.engagement_tag == "deposit_promised"
        ).count()
        vips = s.query(Lead).filter(Lead.is_vip_potential.is_(True)).count()
        rewarm = s.query(Lead).filter(Lead.rewarm_candidate.is_(True)).count()
        active = s.query(Lead).filter(Lead.liga_state == "active").count()
        in_private = s.query(Lead).filter(Lead.in_private_group.is_(True)).count()

        # Top do dia anterior
        top = (
            s.query(Lead, func.sum(DailyVolume.volume_usd).label("vol"))
            .join(DailyVolume, DailyVolume.lead_id == Lead.id)
            .filter(DailyVolume.date == yesterday.strftime("%Y-%m-%d"))
            .group_by(Lead.id)
            .order_by(func.sum(DailyVolume.volume_usd).desc())
            .limit(3)
            .all()
        )

    lines = [
        "---",
        f"date: {today}",
        f"new_leads: {new_leads}",
        f"vips: {vips}",
        f"deposits_promised: {deposits_promised}",
        "tags: [insight, digest]",
        "---",
        "",
        f"# Insights · {today}",
        "",
        "## 📊 Números",
        "",
        f"- **{new_leads}** novos leads ontem",
        f"- 💎 **{vips}** VIPs em potencial",
        f"- 💰 **{deposits_promised}** prometeram depositar",
        f"- 🔥 **{rewarm}** candidatos a re-aquecer",
        f"- 🏆 **{active}** ativos na Liga",
        f"- ✅ **{in_private}** já no grupo privado",
        "",
    ]
    if top:
        lines.append("## 🏆 Top de ontem (volume)")
        lines.append("")
        for i, (lead, vol) in enumerate(top, 1):
            handle = f"@{lead.username}" if lead.username else f"id-{lead.telegram_id}"
            lines.append(f"{i}. {handle} — ${float(vol or 0):,.2f}")
        lines.append("")
    if extra_lines:
        lines.append("## 📝 Notas")
        lines.append("")
        lines.extend(extra_lines)

    target.write_text("\n".join(lines), encoding="utf-8")
    return target
