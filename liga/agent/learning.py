"""Modo passivo do Agente — SEMPRE ativo, aprende com toda DM.

Quando você responde um lead, esse módulo:
1. Pareia sua resposta com a pergunta anterior do lead (heurística temporal)
2. Classifica a categoria via Claude Haiku
3. Salva o par em AgentLearningExample
4. Cron diário consolida exemplos no vault Obsidian

Vault gerada:
  C:\\liga-vault\\learned-responses\\
    deposit_method.md
    link_request.md
    waiting_response.md
    deposit_promised.md
    proof_received.md
    objection_money.md
    objection_trust.md
    greeting.md
    complex.md
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from db import SessionLocal
from db.models import AgentLearningExample, Lead, LeadMessage

logger = logging.getLogger(__name__)


# Categorias do agente (alinhadas com agent_directives.py)
AGENT_CATEGORIES = [
    "deposit_method",
    "link_request",
    "waiting_response",
    "deposit_promised",
    "proof_received",
    "objection_money",
    "objection_trust",
    "greeting",
    "complex",
]


def _vault_path() -> Optional[Path]:
    raw = os.getenv("OBSIDIAN_VAULT_PATH", "").strip()
    if not raw:
        return None
    p = Path(raw)
    if not p.exists():
        try:
            p.mkdir(parents=True, exist_ok=True)
        except Exception:
            return None
    return p


def _classify_pair_category(lead_msg: str, your_reply: str) -> tuple[str, float]:
    """Classifica o par (pergunta_lead, sua_resposta) em uma das AGENT_CATEGORIES.

    Usa Claude Haiku — mais barato e suficiente pra essa tarefa.
    Retorna (categoria, confidence). Em erro retorna ('complex', 0.0).
    """
    if not your_reply or not your_reply.strip():
        return "complex", 0.0

    prompt = f"""Classifique este par de DMs (lead → você responde) em UMA categoria:

Lead: "{(lead_msg or '')[:300]}"
Você responde: "{(your_reply or '')[:300]}"

Categorias:
- deposit_method: lead pergunta como depositar / qual método
- link_request: lead pede o link de cadastro
- waiting_response: lead pergunta se tem novidade / "tô esperando"
- deposit_promised: lead promete depositar (futuro)
- proof_received: lead manda print/comprovante
- objection_money: lead diz que não tem dinheiro
- objection_trust: lead desconfia / pergunta se é golpe
- greeting: cumprimento simples
- complex: qualquer outra coisa que não bate em nada acima

Responda SOMENTE em JSON: {{"category": "<nome>", "confidence": 0.0-1.0}}"""

    try:
        from ai.providers import generate_completion
        response = generate_completion(
            system="You are a classifier. Return ONLY valid JSON.",
            user=prompt,
            max_tokens=50,
            temperature=0.0,
        )
        clean = response.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
            clean = clean.rsplit("```", 1)[0]
        data = json.loads(clean.strip())
        cat = data.get("category", "complex")
        conf = float(data.get("confidence", 0.0))
        if cat not in AGENT_CATEGORIES:
            cat = "complex"
        return cat, conf
    except Exception:
        logger.debug("[agent_learning] erro classificando par", exc_info=True)
        return "complex", 0.0


def register_dm_pair(lead_id: int, your_reply_text: str, lookback_minutes: int = 120) -> Optional[int]:
    """Pareia sua resposta `out` com a última msg `in` do lead até X min antes.

    Cria AgentLearningExample. Retorna id criado, ou None se não encontrou par.
    Idempotente — checa por duplicata na mesma janela.
    """
    if not your_reply_text or not your_reply_text.strip():
        return None

    cutoff = datetime.utcnow() - timedelta(minutes=lookback_minutes)

    with SessionLocal() as s:
        lead = s.query(Lead).get(lead_id)
        if not lead:
            return None

        # Última msg `in` do lead até X min antes
        last_in = (
            s.query(LeadMessage)
            .filter(LeadMessage.lead_id == lead_id)
            .filter(LeadMessage.direction == "in")
            .filter(LeadMessage.created_at >= cutoff)
            .order_by(LeadMessage.created_at.desc())
            .first()
        )
        if not last_in or not last_in.content:
            return None

        # Verifica se já existe exemplo pra esse par exato
        existing = (
            s.query(AgentLearningExample)
            .filter(AgentLearningExample.lead_id == lead_id)
            .filter(AgentLearningExample.lead_msg == last_in.content[:5000])
            .filter(AgentLearningExample.your_reply == your_reply_text[:5000])
            .first()
        )
        if existing:
            return existing.id

        # Classifica categoria via Haiku
        category, confidence = _classify_pair_category(last_in.content, your_reply_text)

        ex = AgentLearningExample(
            lead_id=lead_id,
            category=category,
            lead_msg=last_in.content[:5000],
            your_reply=your_reply_text[:5000],
            lead_country=getattr(lead, "liga_id_country", None),
            lead_is_vip=bool(getattr(lead, "is_vip_potential", False)),
            quality_score=confidence,
            in_vault=False,
        )
        s.add(ex)
        s.commit()
        ex_id = ex.id

    logger.info("[agent_learning] novo exemplo lead=%d cat=%s conf=%.2f", lead_id, category, confidence)
    return ex_id


def consolidate_vault() -> dict:
    """Cron diário: pega exemplos com in_vault=False e escreve nos arquivos da vault.

    Mantém top 30 mais recentes por categoria nos arquivos MD.
    Marca exemplos consolidados como in_vault=True.
    """
    vault = _vault_path()
    if not vault:
        return {"ok": False, "reason": "vault não configurada"}

    learned_dir = vault / "learned-responses"
    learned_dir.mkdir(exist_ok=True)

    counts = {"updated_categories": 0, "examples_added": 0}

    with SessionLocal() as s:
        new_examples = (
            s.query(AgentLearningExample)
            .filter(AgentLearningExample.in_vault.is_(False))
            .order_by(AgentLearningExample.created_at.desc())
            .all()
        )
        if not new_examples:
            return {"ok": True, "no_new_examples": True}

        # Agrupa por categoria
        by_cat: dict[str, list] = {}
        for ex in new_examples:
            by_cat.setdefault(ex.category, []).append(ex)

        for cat, examples in by_cat.items():
            md_file = learned_dir / f"{cat}.md"

            # Carrega exemplos existentes (top 30 mais recentes do banco)
            top_existing = (
                s.query(AgentLearningExample)
                .filter(AgentLearningExample.category == cat)
                .order_by(AgentLearningExample.created_at.desc())
                .limit(30).all()
            )

            # Monta MD
            lines = [
                f"# Respostas pra: {cat}",
                "",
                "> Auto-gerado pelo agente passivo. Atualizado diariamente.",
                f"> Última atualização: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
                f"> Total de exemplos coletados: {s.query(AgentLearningExample).filter(AgentLearningExample.category == cat).count()}",
                "",
                "## Top exemplos (mais recentes primeiro)",
                "",
            ]
            for i, ex in enumerate(top_existing, 1):
                vip_tag = " [VIP]" if ex.lead_is_vip else ""
                country_tag = f" — 🌎 {ex.lead_country}" if ex.lead_country else ""
                date_str = ex.created_at.strftime("%Y-%m-%d") if ex.created_at else "?"
                lines += [
                    f"### Exemplo {i} ({date_str}){vip_tag}{country_tag}",
                    "",
                    "**Lead:**",
                    f"> {(ex.lead_msg or '').replace(chr(10), chr(10) + '> ')}",
                    "",
                    "**Você:**",
                    f"> {(ex.your_reply or '').replace(chr(10), chr(10) + '> ')}",
                    "",
                    "---",
                    "",
                ]

            try:
                md_file.write_text("\n".join(lines), encoding="utf-8")
                counts["updated_categories"] += 1
                counts["examples_added"] += len(examples)
            except Exception:
                logger.exception("[vault] erro escrevendo %s", md_file)

            # Marca como in_vault
            for ex in examples:
                ex.in_vault = True

        s.commit()

    logger.info("[vault] consolidação: %s", counts)
    return {"ok": True, **counts}


def get_learning_stats() -> dict:
    """Retorna stats por categoria pra UI."""
    with SessionLocal() as s:
        total = s.query(AgentLearningExample).count()
        by_cat = {}
        for cat in AGENT_CATEGORIES:
            count = (
                s.query(AgentLearningExample)
                .filter(AgentLearningExample.category == cat)
                .count()
            )
            by_cat[cat] = count
        last_ex = (
            s.query(AgentLearningExample)
            .order_by(AgentLearningExample.created_at.desc())
            .first()
        )
    return {
        "total": total,
        "by_category": by_cat,
        "last_added": last_ex.created_at if last_ex else None,
        "ready_categories": [c for c, n in by_cat.items() if n >= 30],
    }
