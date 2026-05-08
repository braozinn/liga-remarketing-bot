"""Suggester — gera sugestão de resposta usando vault + Haiku.

Quando Modo 3 (agente ativo) está ON:
- Pra cada DM nova fora do funil, classifica
- Busca top 5 exemplos da categoria
- Gera resposta no estilo do Facundo via Haiku
- Cria AgentSuggestion (status='pending')
- Aparece em /agent/queue pra aprovação
"""
from __future__ import annotations

import logging
import os
from datetime import datetime
from typing import Optional

from db import SessionLocal
from db.models import AgentSuggestion, AgentLearningExample, Lead, LeadMessage

logger = logging.getLogger(__name__)


def is_agent_active() -> bool:
    """Modo 3 está ON?"""
    from db.models import Setting
    try:
        with SessionLocal() as s:
            row = s.query(Setting).filter_by(key="agent_active").one_or_none()
            return row is not None and row.value == "1"
    except Exception:
        return False


def set_agent_active(active: bool) -> None:
    from db.models import Setting
    with SessionLocal() as s:
        row = s.query(Setting).filter_by(key="agent_active").one_or_none()
        if row:
            row.value = "1" if active else "0"
        else:
            s.add(Setting(key="agent_active", value="1" if active else "0"))
        s.commit()


def _build_examples_block(category: str, lead, max_examples: int = 5) -> str:
    """Monta bloco de exemplos pro prompt, priorizando exemplos do mesmo país/VIP."""
    with SessionLocal() as s:
        q = s.query(AgentLearningExample).filter(AgentLearningExample.category == category)
        if lead and getattr(lead, "is_vip_potential", False):
            vip_exs = q.filter(AgentLearningExample.lead_is_vip.is_(True)).order_by(
                AgentLearningExample.created_at.desc()
            ).limit(max_examples).all()
            non_vip_exs = q.filter(AgentLearningExample.lead_is_vip.is_(False)).order_by(
                AgentLearningExample.created_at.desc()
            ).limit(max_examples - len(vip_exs)).all()
            examples = vip_exs + non_vip_exs
        else:
            examples = q.order_by(AgentLearningExample.created_at.desc()).limit(max_examples).all()

        ex_data = [(ex.lead_msg, ex.your_reply, ex.lead_country) for ex in examples]

    if not ex_data:
        return "(sem exemplos coletados ainda dessa categoria)"

    lines = []
    for i, (lm, yr, country) in enumerate(ex_data, 1):
        country_tag = f" [{country}]" if country else ""
        lines += [
            f"Exemplo {i}{country_tag}:",
            f"Lead: \"{(lm or '')[:200]}\"",
            f"Você responde: \"{(yr or '')[:300]}\"",
            "",
        ]
    return "\n".join(lines)


def generate_suggestion(lead: Lead, dm_text: str, category: Optional[str] = None) -> dict:
    """Gera sugestão de resposta pra DM nova.

    Returns dict com category, confidence, suggested_response.
    Não envia nada — só cria a sugestão.
    """
    if not dm_text or not dm_text.strip():
        return {"ok": False, "error": "dm vazia"}

    # Classifica se não veio definido
    if not category:
        from .learning import _classify_pair_category
        category, _ = _classify_pair_category(dm_text, "(placeholder)")

    examples_block = _build_examples_block(category, lead)

    # Importa diretrizes do voseo
    try:
        from liga.agent_directives import build_system_prompt
        system_prompt = build_system_prompt(extra_context=f"Categoria detectada: {category}")
    except Exception:
        system_prompt = (
            "Você é assistente de remarketing Quotex no LatAm. Responda em ES rioplatense, "
            "sempre 'vos' (NUNCA 'tú'). Tom informal, direto, conversacional."
        )

    first_name = (lead.first_name or "").split()[0] if lead and lead.first_name else "amigo"
    country = getattr(lead, "liga_id_country", None)
    is_vip = bool(getattr(lead, "is_vip_potential", False))

    user_prompt = f"""Lead: {first_name}{f' (de {country})' if country else ''}{' [VIP]' if is_vip else ''}

Mensagem que ele enviou agora:
"{dm_text[:500]}"

EXEMPLOS DE COMO VOCÊ JÁ RESPONDEU CASOS SIMILARES (siga esse estilo, não invente):

{examples_block}

GERE UMA RESPOSTA NO MESMO ESTILO. Regras:
- ES rioplatense, "vos" sempre, NUNCA "tú"
- Tom igual aos exemplos (informal, direto)
- Curta: 1-3 frases
- Personalize com o nome se fizer sentido
- Se não tem exemplo bom pra essa situação, prefira responder algo genérico curto

Retorne APENAS a resposta a enviar (texto puro, sem aspas, sem markdown)."""

    try:
        from ai.providers import generate_completion
        response = generate_completion(
            system=system_prompt,
            user=user_prompt,
            max_tokens=200,
            temperature=0.4,
        )
        suggested = (response or "").strip()
        # Remove aspas externas se vieram
        if suggested.startswith('"') and suggested.endswith('"'):
            suggested = suggested[1:-1]
        return {
            "ok": True,
            "category": category,
            "suggested_response": suggested,
        }
    except Exception as e:
        logger.exception("[suggester] erro gerando sugestão")
        return {"ok": False, "error": str(e), "category": category}


def create_suggestion_for_dm(lead_id: int, dm_text: str, dm_telegram_id: Optional[int] = None) -> Optional[int]:
    """Cria AgentSuggestion pendente. Chamado pelo tracker quando agente está ativo.

    Returns ID criado ou None.
    """
    if not is_agent_active():
        return None

    with SessionLocal() as s:
        lead = s.query(Lead).get(lead_id)
        if not lead:
            return None
        # Snapshot atributos pra usar fora da session
        lead_attrs = {
            "id": lead.id,
            "first_name": lead.first_name,
            "liga_id_country": lead.liga_id_country,
            "is_vip_potential": lead.is_vip_potential,
        }

    # Cria objeto Lead-like pra suggester
    class _LeadStub:
        def __init__(self, d):
            for k, v in d.items():
                setattr(self, k, v)

    lead_stub = _LeadStub(lead_attrs)
    res = generate_suggestion(lead_stub, dm_text)
    if not res.get("ok"):
        return None

    with SessionLocal() as s:
        sug = AgentSuggestion(
            lead_id=lead_id,
            dm_text=dm_text[:5000],
            dm_telegram_id=dm_telegram_id,
            category=res.get("category"),
            confidence=0.8,
            suggested_response=res.get("suggested_response", "")[:5000],
            status="pending",
        )
        s.add(sug)
        s.commit()
        sug_id = sug.id

    logger.info(
        "[suggester] sugestão criada lead=%d cat=%s len=%d",
        lead_id, res.get("category"), len(res.get("suggested_response") or ""),
    )
    return sug_id


def get_pending_count() -> int:
    with SessionLocal() as s:
        return s.query(AgentSuggestion).filter(AgentSuggestion.status == "pending").count()
