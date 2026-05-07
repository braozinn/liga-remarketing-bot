"""Análise contextual de lead via Claude Haiku, lendo da Obsidian vault.

Fluxo:
1. Lê vault: _meta/ai-context.md (system prompt) + playbook/{relevante}.md (estratégia)
2. Lê SQLite: últimas 30 mensagens do lead (LeadMessage) + dados do Lead
3. Lê arquivo do lead em vault/leads/ (se existe — contexto histórico + suas notas)
4. Manda tudo pro Haiku com prompt estruturado
5. Recebe análise em formato JSON estruturado
6. Atualiza arquivo do lead em vault preservando seção '## 📝 Suas notas'
7. Retorna análise + custo (telemetria)
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from db import SessionLocal
from db.models import Lead, LeadMessage, OperationProof

logger = logging.getLogger(__name__)


def _vault_path() -> Optional[Path]:
    raw = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    if not raw:
        return None
    p = Path(raw)
    return p if p.exists() else None


def _read_file(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except Exception:
        return ""


def _select_playbook(engagement_tag: Optional[str], liga_state: Optional[str]) -> str:
    """Escolhe qual playbook usar baseado em tags do lead."""
    mapping = {
        "first_contact_no_reply": "frios-reativar.md",
        "remarketed_no_account":  "frios-reativar.md",
        "deposit_promised":        "promessas-cobrar.md",
        "deposited":               "vip-tratamento.md",
    }
    if engagement_tag and engagement_tag in mapping:
        return mapping[engagement_tag]
    if liga_state in ("active", "at_risk", "finalist"):
        return "vip-tratamento.md"
    return "novos-leads.md"


def _build_context(lead: Lead, session) -> dict:
    """Monta o pacote completo de contexto: system prompt + playbook + lead .md + msgs."""
    vault = _vault_path()
    ctx = {
        "ai_context": "",
        "playbook": "",
        "lead_md_existing": "",
        "messages": [],
        "lead_data": {},
        "playbook_name": None,
    }

    # 1) system prompt geral
    if vault:
        ai_ctx = vault / "_meta" / "ai-context.md"
        ctx["ai_context"] = _read_file(ai_ctx)

        # 2) playbook relevante
        pb_name = _select_playbook(lead.engagement_tag, lead.liga_state)
        ctx["playbook_name"] = pb_name
        pb_file = vault / "playbook" / pb_name
        ctx["playbook"] = _read_file(pb_file)

        # 3) arquivo .md do lead (com suas notas + histórico de análises)
        from .obsidian_export import _lead_filename
        lead_md = vault / "leads" / _lead_filename(lead)
        ctx["lead_md_existing"] = _read_file(lead_md) if lead_md.exists() else ""

    # 4) últimas 30 mensagens do SQLite
    msgs = (
        session.query(LeadMessage)
        .filter(LeadMessage.lead_id == lead.id)
        .order_by(LeadMessage.created_at.desc())
        .limit(30)
        .all()
    )
    ctx["messages"] = [
        {
            "direction": m.direction,
            "kind": m.kind,
            "content": m.content,
            "duration_sec": m.duration_sec,
            "created_at": m.created_at.isoformat() if m.created_at else None,
        }
        for m in reversed(msgs)  # ordem cronológica pro Haiku
    ]

    # 5) dados estruturados do lead
    ctx["lead_data"] = {
        "lead_id": lead.id,
        "telegram_id": lead.telegram_id,
        "username": lead.username,
        "first_name": lead.first_name,
        "status": lead.status,
        "engagement_tag": lead.engagement_tag,
        "liga_state": lead.liga_state,
        "liga_account_id": lead.liga_account_id,
        "liga_id_status": lead.liga_id_status,
        "country": lead.liga_id_country,
        "balance": lead.liga_id_balance,
        "deposits_sum": lead.liga_id_deposits_sum,
        "turnover": lead.liga_id_turnover,
        "is_vip_potential": lead.is_vip_potential,
        "rewarm_candidate": lead.rewarm_candidate,
        "opted_out": lead.opted_out,
        "last_dm_at": lead.last_dm_at.isoformat() if lead.last_dm_at else None,
    }

    return ctx


def _format_messages_for_prompt(msgs: list[dict]) -> str:
    if not msgs:
        return "_(sem histórico de mensagens registrado)_"
    lines = []
    for m in msgs:
        when = m.get("created_at", "")[:16] if m.get("created_at") else "?"
        who = "VOCÊ" if m["direction"] == "out" else "LEAD"
        kind = m.get("kind", "text")
        content = (m.get("content") or "")[:300]
        prefix = f"[{when}] {who}"
        if kind != "text":
            prefix += f" ({kind}"
            if m.get("duration_sec"):
                prefix += f" {m['duration_sec']}s"
            prefix += ")"
        lines.append(f"{prefix}: {content}")
    return "\n".join(lines)


_ANALYSIS_SCHEMA = """{
  "engagement_tag": "first_contact_no_reply | account_no_deposit | remarketed_no_account | deposit_promised | deposited",
  "confidence": "alta | media | baixa",
  "lead_temperature": "frio | morno | quente | convertido | hostil",
  "razao": "1-2 frases explicando o porquê da classificação",
  "sinais_detectados": ["bullet 1", "bullet 2", ...],
  "sugestao_acao": "o que o Facundo deveria fazer agora",
  "next_review_in_days": número (quantos dias até reanalisar),
  "alertas": ["se houver: VIP potencial, suspeita de fraude, churn iminente, pergunta direta esperando resposta..."]
}"""


def _build_haiku_prompt(ctx: dict) -> tuple[str, str]:
    """Retorna (system_prompt, user_message) pra Haiku."""
    ai_ctx = ctx["ai_context"] or "Você é um assistente de classificação de leads."

    system = f"""{ai_ctx}

---

Sua tarefa AGORA: analisar o histórico de conversa abaixo e retornar um JSON com:

{_ANALYSIS_SCHEMA}

REGRAS DE OUTPUT:
- Retorne APENAS um JSON válido, sem texto antes nem depois
- Sem markdown, sem ```json
- Conteúdo das strings em português brasileiro
"""

    pb = ctx["playbook"] or ""
    pb_name = ctx["playbook_name"] or "n/a"

    existing_md = ctx["lead_md_existing"] or ""
    # Extrai só seção de notas humanas + análises anteriores se houver
    historico_humano = ""
    if "## 📝 Suas notas" in existing_md:
        parts = existing_md.split("## 📝 Suas notas", 1)
        if len(parts) > 1:
            after = parts[1]
            next_h = re.search(r"\n## ", after)
            if next_h:
                historico_humano = after[:next_h.start()].strip()
            else:
                historico_humano = after.strip()

    msgs_formatted = _format_messages_for_prompt(ctx["messages"])

    ld = ctx["lead_data"]
    lead_summary = json.dumps({k: v for k, v in ld.items() if v is not None}, ensure_ascii=False, indent=2)

    user = f"""# Lead a analisar

## Dados estruturados:
{lead_summary}

## Playbook aplicável ({pb_name}):
{pb}

## Notas humanas existentes (do Facundo) — RESPEITE essa info:
{historico_humano or '_(nenhuma anotação manual ainda)_'}

## Histórico de mensagens (últimas {len(ctx['messages'])}):
{msgs_formatted}

---

Retorne APENAS o JSON conforme schema. Nada mais."""

    return system, user


def analyze_lead_with_obsidian_context(lead_id: int) -> dict:
    """Roda a análise contextual via Haiku usando vault como cérebro.

    Retorna dict com a análise + atualiza o .md do lead na vault.
    """
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY não configurada"}

    try:
        from anthropic import Anthropic
    except ImportError:
        return {"error": "pacote anthropic não instalado"}

    with SessionLocal() as session:
        lead = session.query(Lead).get(lead_id)
        if not lead:
            return {"error": "lead não encontrado"}
        ctx = _build_context(lead, session)
        lead_display = lead.display_name

    system, user = _build_haiku_prompt(ctx)

    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    client = Anthropic(api_key=api_key)

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=1500,
            temperature=0.2,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
    except Exception as e:
        logger.exception("[analise] erro chamando Haiku")
        return {"error": str(e)}

    # Telemetria
    try:
        from ai.providers import _record_usage
        usage = getattr(msg, "usage", None)
        in_t = getattr(usage, "input_tokens", 0) if usage else 0
        out_t = getattr(usage, "output_tokens", 0) if usage else 0
        _record_usage("anthropic", model, "analyze_lead_context", in_t, out_t, lead_id=lead_id)
    except Exception:
        pass

    parts = []
    for block in msg.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    raw = "".join(parts).strip()

    # Parseia JSON
    analysis = None
    try:
        analysis = json.loads(raw)
    except json.JSONDecodeError:
        m = re.search(r"\{[\s\S]*\}", raw)
        if m:
            try:
                analysis = json.loads(m.group(0))
            except Exception:
                pass

    if not analysis:
        logger.warning("[analise] resposta sem JSON parseável: %s", raw[:200])
        return {"error": "JSON inválido", "raw": raw[:500]}

    # Atualiza o arquivo .md do lead com a nova análise IA (preserva suas notas)
    _update_lead_md_with_analysis(lead_id, analysis)

    logger.info(
        "[analise] lead=%s tag=%s conf=%s",
        lead_display, analysis.get("engagement_tag"), analysis.get("confidence"),
    )
    return {"ok": True, "analysis": analysis}


def _update_lead_md_with_analysis(lead_id: int, analysis: dict) -> None:
    """Reescreve a seção '## 🧠 Análise IA' do .md do lead, preservando 'Suas notas'."""
    vault = _vault_path()
    if not vault:
        return

    from .obsidian_export import _lead_filename, _render_lead_md, _extract_human_notes

    with SessionLocal() as session:
        lead = session.query(Lead).get(lead_id)
        if not lead:
            return

        # Atualiza engagement_tag se confiança alta
        new_tag = analysis.get("engagement_tag")
        confidence = (analysis.get("confidence") or "").lower()
        if new_tag and confidence == "alta" and new_tag != lead.engagement_tag:
            lead.engagement_tag = new_tag
            lead.engagement_tag_updated_at = datetime.utcnow()
            try:
                lead.engagement_evidence = json.dumps(
                    {"source": "haiku_contextual", "analysis": analysis},
                    ensure_ascii=False,
                )[:2000]
            except Exception:
                pass
            session.commit()

        target_dir = vault / "leads"
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / _lead_filename(lead)

        existing = target.read_text(encoding="utf-8") if target.exists() else ""
        # Renderiza o .md base normalmente
        base_md = _render_lead_md(lead, existing, session)

    # Substitui a seção "## 🧠 Análise IA" pela análise nova
    ai_block_lines = [
        f"## 🧠 Análise IA *(atualizada em {datetime.utcnow().strftime('%Y-%m-%d %H:%M')})*",
        "",
        f"**Tag sugerida**: `{analysis.get('engagement_tag', '?')}` · "
        f"Confiança: **{analysis.get('confidence', '?')}** · "
        f"Temperatura: **{analysis.get('lead_temperature', '?')}**",
        "",
        f"**Razão**: {analysis.get('razao', '_(sem razão)_')}",
        "",
        "**Sinais detectados**:",
    ]
    sinais = analysis.get("sinais_detectados") or []
    if sinais:
        for s in sinais:
            ai_block_lines.append(f"- {s}")
    else:
        ai_block_lines.append("- _(nenhum sinal específico identificado)_")

    ai_block_lines.extend([
        "",
        f"**Sugestão de ação**: {analysis.get('sugestao_acao', '_(aguardar)_')}",
        "",
        f"**Próxima revisão**: {analysis.get('next_review_in_days', '?')} dias",
    ])

    alertas = analysis.get("alertas") or []
    if alertas:
        ai_block_lines.append("")
        ai_block_lines.append("**🚨 Alertas**:")
        for a in alertas:
            ai_block_lines.append(f"- {a}")

    ai_block = "\n".join(ai_block_lines)

    # Substitui a seção no markdown
    pattern = re.compile(r"## 🧠 Análise IA[\s\S]*?(?=\n## )", re.MULTILINE)
    if pattern.search(base_md):
        new_md = pattern.sub(ai_block + "\n\n", base_md, count=1)
    else:
        # Fallback: insere antes da Linha do tempo
        new_md = base_md.replace(
            "## 📜 Linha do tempo",
            ai_block + "\n\n## 📜 Linha do tempo",
            1,
        )

    # Atomic write
    tmp = target.with_suffix(target.suffix + ".tmp")
    try:
        tmp.write_text(new_md, encoding="utf-8")
        tmp.replace(target)
    except Exception:
        logger.exception("[analise] falhou escrevendo %s", target)


# ---------------------------------------------------------------------------
# Cron: análise em batch dos leads com mais probabilidade de mudança
# ---------------------------------------------------------------------------
async def task_weekly_contextual_analysis(max_leads: int = 100) -> dict:
    """Domingo 04h00 BA — re-analisa os leads que MAIS provavelmente mudaram.

    Prioriza:
    - Leads com mensagens novas desde a última análise
    - Leads com `engagement_tag` em ('deposit_promised', 'account_no_deposit')
    - Leads VIP potencial
    - Skipa: in_private_group, opted_out, BLOCKED
    """
    import asyncio

    if not _vault_path():
        return {"error": "vault não configurada"}
    if not os.getenv("ANTHROPIC_API_KEY", "").strip():
        return {"error": "ANTHROPIC_API_KEY não configurada"}

    analyzed = 0
    failed = 0

    with SessionLocal() as s:
        # Prioriza leads com mensagens recentes + tags relevantes
        candidates = (
            s.query(Lead)
            .filter(Lead.opted_out.is_(False))
            .filter(Lead.in_private_group.is_(False))
            .filter(Lead.status.notin_(["blocked", "excluded"]))
            .filter(
                Lead.engagement_tag.in_([
                    "deposit_promised", "account_no_deposit", "remarketed_no_account",
                ])
                | Lead.is_vip_potential.is_(True)
                | Lead.rewarm_candidate.is_(True)
            )
            .order_by(Lead.last_dm_at.desc())
            .limit(max_leads)
            .all()
        )
        lead_ids = [l.id for l in candidates]

    for lead_id in lead_ids:
        try:
            result = analyze_lead_with_obsidian_context(lead_id)
            if result.get("ok"):
                analyzed += 1
            else:
                failed += 1
            await asyncio.sleep(1.5)  # respeita Anthropic API rate limit
        except Exception:
            logger.exception("[analise] erro lead %s", lead_id)
            failed += 1

    logger.info("[analise] semanal: %d analisados, %d falharam", analyzed, failed)
    return {"analyzed": analyzed, "failed": failed, "total_candidates": len(lead_ids)}
