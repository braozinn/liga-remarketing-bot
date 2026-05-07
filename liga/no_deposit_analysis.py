"""Clusteriza razões pelas quais leads em `account_no_deposit` não depositaram.

Roda mensalmente. Pega últimas msgs dos leads com tag `account_no_deposit` e
manda em batch pro Claude Haiku, que retorna categorias com %.

Resultado salvo em Setting (key='no_deposit_analysis_cache') pra exibir
em /metrics/no-deposit.
"""
from __future__ import annotations

import json
import logging
import os
import re
from datetime import datetime
from typing import Optional

from db import SessionLocal
from db.models import Lead, LeadMessage, Setting

logger = logging.getLogger(__name__)

CACHE_KEY = "no_deposit_analysis_cache"
SAMPLE_SIZE = 80  # leads na amostra
MAX_MSGS_PER_LEAD = 15  # mensagens por lead na análise


_ANALYSIS_PROMPT = """Você está analisando conversas reais entre suporte de afiliado de Quotex e leads
hispanohablantes de LatAm que CRIARAM conta na plataforma mas NÃO DEPOSITARAM ainda.

Sua tarefa: ler as conversas abaixo e CLUSTERIZAR as razões mais comuns pelas quais
esses leads não depositaram.

Categorias possíveis (você pode propor outras):
- silencio: lead não respondeu mais nada após criar conta
- sem_dinheiro: mencionou problema financeiro ("no tengo plata", "esperando sueldo")
- objecao_familia: precisa aprovação esposo/esposa/família
- problema_tecnico: problema com cartão, deposito não funciona, banco bloqueou
- medo_iniciante: "es nuevo pra mí", "tengo miedo de perder"
- desconfianca: pergunta se é golpe, pede mais info, hesita
- outro_broker: mencionou usar outro broker (IQ Option, Pocket Option, etc)
- quer_aprender_mais: pediu mais material, curso, mentor antes de investir
- ja_perdeu: já tentou trading antes e perdeu, tem trauma
- outro: razão não enquadrada acima

Para cada categoria, retorne:
- count: quantos leads CLARAMENTE se enquadram
- percentage: % do total da amostra
- example_phrase: 1 frase exemplo real do dataset (em ES, copy-paste literal)
- recommendation: como abordar esses leads (1 frase em PT-BR)

Formato de output (apenas JSON, sem markdown):
{
  "total_analyzed": <int>,
  "categories": [
    {
      "key": "silencio",
      "label": "Silêncio total após cadastro",
      "count": 32,
      "percentage": 40,
      "example_phrase": "...",
      "recommendation": "..."
    },
    ...
  ],
  "summary": "1-2 frases resumindo os principais bloqueios"
}"""


async def task_analyze_no_deposit_reasons(sample_size: int = SAMPLE_SIZE) -> dict:
    """Roda análise mensalmente. Cacheia resultado em Setting."""
    api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
    if not api_key:
        return {"error": "ANTHROPIC_API_KEY ausente"}

    try:
        from anthropic import Anthropic
    except ImportError:
        return {"error": "pacote anthropic não instalado"}

    # Coleta amostra
    with SessionLocal() as s:
        leads = (
            s.query(Lead)
            .filter(Lead.engagement_tag == "account_no_deposit")
            .filter(Lead.opted_out.is_(False))
            .filter(Lead.in_private_group.is_(False))
            .order_by(Lead.last_dm_at.desc())
            .limit(sample_size)
            .all()
        )
        lead_ids = [l.id for l in leads]
        lead_summaries = []
        for lead in leads:
            msgs = (
                s.query(LeadMessage)
                .filter(LeadMessage.lead_id == lead.id)
                .order_by(LeadMessage.created_at.desc())
                .limit(MAX_MSGS_PER_LEAD)
                .all()
            )
            msgs_text = []
            for m in reversed(msgs):
                who = "VOCÊ" if m.direction == "out" else "LEAD"
                content = (m.content or "")[:200]
                msgs_text.append(f"[{who}] {content}")
            if msgs_text:
                lead_summaries.append({
                    "lead_id": lead.id,
                    "country": lead.liga_id_country or "?",
                    "messages": msgs_text,
                })

    if len(lead_summaries) < 5:
        return {"error": "amostra muito pequena (< 5 leads com mensagens)"}

    # Monta prompt com os dados
    user_msg = f"Total de leads na amostra: {len(lead_summaries)}\n\n"
    for i, ls in enumerate(lead_summaries, 1):
        user_msg += f"--- Lead {i} ({ls['country']}) ---\n"
        user_msg += "\n".join(ls["messages"][-MAX_MSGS_PER_LEAD:])
        user_msg += "\n\n"
    user_msg += f"\n\nAgora clusterize. Retorne APENAS JSON conforme schema."

    # Chama Haiku
    model = os.getenv("ANTHROPIC_MODEL", "claude-haiku-4-5-20251001")
    client = Anthropic(api_key=api_key)

    try:
        msg = client.messages.create(
            model=model,
            max_tokens=2500,
            temperature=0.3,
            system=_ANALYSIS_PROMPT,
            messages=[{"role": "user", "content": user_msg}],
        )
    except Exception as e:
        logger.exception("[no_deposit] erro Haiku")
        return {"error": str(e)}

    # Telemetria
    try:
        from ai.providers import _record_usage
        usage = getattr(msg, "usage", None)
        in_t = getattr(usage, "input_tokens", 0) if usage else 0
        out_t = getattr(usage, "output_tokens", 0) if usage else 0
        _record_usage("anthropic", model, "no_deposit_analysis", in_t, out_t)
    except Exception:
        pass

    parts = []
    for block in msg.content:
        if hasattr(block, "text"):
            parts.append(block.text)
    raw = "".join(parts).strip()

    # Parse JSON
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
        return {"error": "JSON inválido", "raw": raw[:500]}

    analysis["analyzed_at"] = datetime.utcnow().isoformat()
    analysis["sample_size"] = len(lead_summaries)

    # Salva em cache
    try:
        with SessionLocal() as s:
            row = s.query(Setting).filter_by(key=CACHE_KEY).first()
            value = json.dumps(analysis, ensure_ascii=False)[:50_000]
            if row:
                row.value = value
            else:
                s.add(Setting(key=CACHE_KEY, value=value))
            s.commit()
    except Exception:
        logger.exception("[no_deposit] erro salvando cache")

    logger.info("[no_deposit] análise concluída: %d categorias",
                len(analysis.get("categories", [])))
    return analysis


def get_cached_analysis() -> Optional[dict]:
    """Retorna a última análise cacheada, ou None se nunca rodou."""
    try:
        with SessionLocal() as s:
            row = s.query(Setting).filter_by(key=CACHE_KEY).first()
            if row and row.value:
                return json.loads(row.value)
    except Exception:
        pass
    return None
