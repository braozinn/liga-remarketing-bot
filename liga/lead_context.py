"""LeadContext — cérebro da IA conhece cada lead.

Função `build_lead_context(lead_id)` retorna dict completo com TUDO
sobre o lead, num único lugar. Usado por:
- Classifier (intent classification com contexto)
- Sugestão de mensagem manual (você ver "esse lead disse X há 3 dias")
- Vault Obsidian (escrever ficha rica, não só metadata)

Atualização incremental: a cada DM nova, `update_context_after_dm()`
mantém os campos de cache atualizados (last_intent, msg_count, etc).

Resumo IA: leads com histórico longo (>50 mensagens) geram um resumo
de 200 tokens uma vez por noite (job em scheduler.py). IA usa o resumo
+ últimas 10 msgs como context window curto — barato e completo.

REGRA: TODO código que vai chamar IA pra falar de um lead PRECISA chamar
`build_lead_context(lead.id)` antes. Sem exceção.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from typing import Optional

from db import SessionLocal
from db.models import Lead, LeadContext, LeadMessage

logger = logging.getLogger(__name__)


# ═══ CRIAR / OBTER ═══════════════════════════════════════════════════════════

def get_or_create_context(lead_id: int) -> Optional[LeadContext]:
    """Garante que existe LeadContext pro lead. Cria vazio se não existir."""
    with SessionLocal() as s:
        ctx = s.query(LeadContext).filter_by(lead_id=lead_id).first()
        if ctx:
            return ctx
        # Cria
        lead = s.query(Lead).get(lead_id)
        if not lead:
            return None
        ctx = LeadContext(lead_id=lead_id)
        s.add(ctx)
        s.commit()
        s.refresh(ctx)
        return ctx


# ═══ BUILD CONTEXT — função estrela ═══════════════════════════════════════════

def build_lead_context(lead_id: int) -> dict:
    """Constrói contexto completo do lead pra IA usar.

    Retorna dict com:
    {
        "lead": {...basic info...},
        "lifecycle": "lead",
        "remarketing_target": "lead_with_account_no_deposit",
        "summary": "Lead criou conta há 3 dias, prometeu depositar no
                    fim de semana mas não cumpriu...",
        "stats": {"msg_in": 12, "msg_out": 8, "days_since_first_dm": 5},
        "last_messages": [{"direction": "in", "content": "...", "ts": "..."}],
        "tags": ["promete_amanha"],
        "objections": [{"text": "...", "date": "...", "category": "..."}],
        "notes_admin": "...",
        "last_intent": "deposito_promessa",
        "last_inbound_at": "...",
        "quotex": {"has_account": True, "account_id": "12345",
                   "balance": 0, "deposits": 0, "has_deposited": False},
    }

    Usado ANTES de qualquer prompt IA pra dar contexto rico.
    """
    from liga.lifecycle import get_remarketing_target

    with SessionLocal() as s:
        lead = s.query(Lead).get(lead_id)
        if not lead:
            return {}

        ctx = s.query(LeadContext).filter_by(lead_id=lead_id).first()
        if not ctx:
            ctx = LeadContext(lead_id=lead_id)
            s.add(ctx)
            s.commit()
            s.refresh(ctx)

        # Recarrega últimas 10 mensagens diretas do banco (mais frescas
        # que o cache last_messages_json que pode estar desatualizado)
        recent_msgs = (
            s.query(LeadMessage)
            .filter(LeadMessage.lead_id == lead_id)
            .order_by(LeadMessage.created_at.desc())
            .limit(10)
            .all()
        )
        last_messages = [
            {
                "direction": m.direction,
                "kind": m.kind or "text",
                "content": (m.content or "")[:300],
                "ts": m.created_at.isoformat() if m.created_at else None,
            }
            for m in reversed(recent_msgs)  # ordem cronológica
        ]

        # Stats reais
        from sqlalchemy import func as _func
        in_count = (
            s.query(_func.count(LeadMessage.id))
            .filter(LeadMessage.lead_id == lead_id, LeadMessage.direction == "in")
            .scalar() or 0
        )
        out_count = (
            s.query(_func.count(LeadMessage.id))
            .filter(LeadMessage.lead_id == lead_id, LeadMessage.direction == "out")
            .scalar() or 0
        )
        first_dm = (
            s.query(_func.min(LeadMessage.created_at))
            .filter(LeadMessage.lead_id == lead_id)
            .scalar()
        )
        days_since_first = None
        if first_dm:
            days_since_first = (datetime.utcnow() - first_dm).days

        # Parse JSON fields
        try:
            tags = json.loads(ctx.tags_json) if ctx.tags_json else []
        except Exception:
            tags = []
        try:
            objections = json.loads(ctx.objections_json) if ctx.objections_json else []
        except Exception:
            objections = []

        return {
            "lead": {
                "id": lead.id,
                "telegram_id": lead.telegram_id,
                "username": lead.username,
                "first_name": lead.first_name,
                "last_name": lead.last_name,
                "display_name": lead.display_name,
            },
            "lifecycle": lead.lifecycle or "new",
            "remarketing_target": get_remarketing_target(lead),
            "summary": ctx.summary or "",
            "summary_updated_at": ctx.summary_updated_at.isoformat() if ctx.summary_updated_at else None,
            "stats": {
                "msg_in": in_count,
                "msg_out": out_count,
                "msg_total": in_count + out_count,
                "days_since_first_dm": days_since_first,
            },
            "last_messages": last_messages,
            "tags": tags,
            "objections": objections,
            "notes_admin": ctx.notes_admin or "",
            "last_intent": ctx.last_intent,
            "last_intent_at": ctx.last_intent_at.isoformat() if ctx.last_intent_at else None,
            "last_inbound_at": ctx.last_inbound_at.isoformat() if ctx.last_inbound_at else None,
            "last_outbound_at": ctx.last_outbound_at.isoformat() if ctx.last_outbound_at else None,
            "quotex": {
                "has_account": ctx.has_account or bool(lead.liga_account_id),
                "account_id": ctx.account_id or lead.liga_account_id,
                "balance_usd": ctx.balance_usd or lead.liga_id_balance,
                "deposits_total": ctx.deposits_total or lead.liga_id_deposits_sum,
                "has_deposited": ctx.has_deposited or (
                    (lead.liga_id_balance or 0) >= 20
                    or (lead.liga_id_deposits_sum or 0) >= 20
                ),
            },
            "flags": {
                "blocked": getattr(lead, "blocked", False),
                "opted_out": lead.opted_out or False,
                "in_private_group": lead.in_private_group or False,
            },
        }


# ═══ ATUALIZAÇÃO INCREMENTAL ═════════════════════════════════════════════════

def update_after_dm(
    lead_id: int,
    direction: str,
    intent: Optional[str] = None,
    timestamp: Optional[datetime] = None,
) -> None:
    """Atualiza LeadContext após uma DM nova (chamado pelo tracker).

    Args:
        direction: 'in' (lead mandou) ou 'out' (nós respondemos)
        intent: intent classificado (só faz sentido pra direction='in')
        timestamp: data/hora da msg (default: agora)
    """
    if timestamp is None:
        timestamp = datetime.utcnow()

    with SessionLocal() as s:
        ctx = s.query(LeadContext).filter_by(lead_id=lead_id).first()
        if not ctx:
            ctx = LeadContext(lead_id=lead_id)
            s.add(ctx)

        if direction == "in":
            ctx.last_inbound_at = timestamp
            ctx.msg_count_in = (ctx.msg_count_in or 0) + 1
            if intent:
                ctx.last_intent = intent
                ctx.last_intent_at = timestamp
        elif direction == "out":
            ctx.last_outbound_at = timestamp
            ctx.msg_count_out = (ctx.msg_count_out or 0) + 1

        s.commit()


def update_quotex_status(lead_id: int, account_id: Optional[str] = None,
                         balance: Optional[float] = None,
                         deposits: Optional[float] = None,
                         deposited: Optional[bool] = None) -> None:
    """Atualiza cache de status Quotex no LeadContext.

    Chamado quando partner bot retorna info nova do lead.
    """
    with SessionLocal() as s:
        ctx = s.query(LeadContext).filter_by(lead_id=lead_id).first()
        if not ctx:
            ctx = LeadContext(lead_id=lead_id)
            s.add(ctx)

        if account_id is not None:
            ctx.account_id = account_id
            ctx.has_account = True
        if balance is not None:
            ctx.balance_usd = balance
        if deposits is not None:
            ctx.deposits_total = deposits
        if deposited is True:
            ctx.has_deposited = True
            if not ctx.deposit_validated_at:
                ctx.deposit_validated_at = datetime.utcnow()

        s.commit()


def add_objection(lead_id: int, text: str, category: str = "outro") -> None:
    """Adiciona uma objeção à lista do lead. Identificada pela IA ou manual."""
    with SessionLocal() as s:
        ctx = s.query(LeadContext).filter_by(lead_id=lead_id).first()
        if not ctx:
            ctx = LeadContext(lead_id=lead_id)
            s.add(ctx)

        try:
            objections = json.loads(ctx.objections_json) if ctx.objections_json else []
        except Exception:
            objections = []

        objections.append({
            "text": text[:300],
            "category": category,
            "date": datetime.utcnow().isoformat(),
        })
        # Mantém só últimas 10 objeções
        objections = objections[-10:]
        ctx.objections_json = json.dumps(objections, ensure_ascii=False)
        s.commit()


def add_tag(lead_id: int, tag: str) -> None:
    """Adiciona uma tag semântica ao lead (idempotente)."""
    with SessionLocal() as s:
        ctx = s.query(LeadContext).filter_by(lead_id=lead_id).first()
        if not ctx:
            ctx = LeadContext(lead_id=lead_id)
            s.add(ctx)

        try:
            tags = json.loads(ctx.tags_json) if ctx.tags_json else []
        except Exception:
            tags = []
        if tag not in tags:
            tags.append(tag)
            ctx.tags_json = json.dumps(tags, ensure_ascii=False)
            s.commit()


# ═══ SUMARIZAÇÃO IA (job noturno) ═════════════════════════════════════════════

def summarize_lead_history(lead_id: int, force: bool = False) -> Optional[str]:
    """Gera/atualiza resumo IA do histórico do lead (~200 tokens).

    Roda pra leads com >50 mensagens (senão não vale a pena IA pesada).
    Atualizado UMA vez por noite — job em scheduler.py.

    Args:
        force: roda mesmo se msg_count não cresceu desde último resumo.

    Returns o resumo gerado, ou None se não foi possível.
    """
    with SessionLocal() as s:
        lead = s.query(Lead).get(lead_id)
        if not lead:
            return None
        ctx = s.query(LeadContext).filter_by(lead_id=lead_id).first()
        if not ctx:
            ctx = LeadContext(lead_id=lead_id)
            s.add(ctx)
            s.commit()
            s.refresh(ctx)

        from sqlalchemy import func as _func
        total_msgs = (
            s.query(_func.count(LeadMessage.id))
            .filter(LeadMessage.lead_id == lead_id)
            .scalar() or 0
        )

        # Skip se poucas msgs (não vale IA)
        if total_msgs < 50 and not force:
            return ctx.summary or None

        # Skip se não cresceu desde último summary
        if (
            not force
            and ctx.summary
            and ctx.summary_msg_count_at_gen
            and total_msgs - ctx.summary_msg_count_at_gen < 20
        ):
            return ctx.summary

        # Pega TODAS as mensagens (vai pro Haiku como contexto)
        all_msgs = (
            s.query(LeadMessage)
            .filter(LeadMessage.lead_id == lead_id)
            .order_by(LeadMessage.created_at.asc())
            .all()
        )
        history_text = "\n".join(
            f"[{(m.direction or '?').upper()}] {(m.content or '')[:200]}"
            for m in all_msgs
        )

    try:
        from ai.providers import generate_completion
        prompt = (
            "Resuma este histórico de DMs Telegram entre um afiliado de Quotex "
            "e um lead, em ESPANHOL RIOPLATENSE (voseo argentino), em ~200 "
            "tokens.\n\n"
            "Inclua:\n"
            "- Quem é o lead (nome se mencionado, perfil)\n"
            "- Em que estágio do funil ele está (sem conta/com conta/depositou)\n"
            "- Última intenção/comportamento dele\n"
            "- Objeções ou dúvidas relevantes que apareceram\n"
            "- Promessas que ele fez (ex: 'voy a depositar mañana')\n"
            "- O que falta pra ele converter\n\n"
            f"HISTÓRICO:\n{history_text[:8000]}\n\n"  # limita pra não estourar
            "Resumo (em espanhol rioplatense, ~200 tokens):"
        )
        summary = generate_completion(
            system="Sos un analista de relacionamiento con leads.",
            user=prompt,
            max_tokens=300,
            temperature=0.3,
        )
        summary = (summary or "").strip()[:1500]

        if summary:
            with SessionLocal() as s:
                ctx = s.query(LeadContext).filter_by(lead_id=lead_id).first()
                if ctx:
                    ctx.summary = summary
                    ctx.summary_updated_at = datetime.utcnow()
                    ctx.summary_msg_count_at_gen = total_msgs
                    s.commit()
            logger.info("[lead_context] resumo gerado pra lead=%d (%d msgs)", lead_id, total_msgs)
        return summary
    except Exception:
        logger.exception("[lead_context] erro gerando resumo pra lead=%d", lead_id)
        return ctx.summary or None


# ═══ LeadContext em formato pra prompt ═══════════════════════════════════════

def format_for_prompt(ctx_dict: dict, max_chars: int = 1500) -> str:
    """Formata o LeadContext num bloco de texto pra injetar em prompts IA.

    Saída tipo:
        LEAD: João Silva (@jsilva, +5511...)
        LIFECYCLE: lead (criou conta mas não depositou)
        ESTÁGIO: lead_with_account_no_deposit
        ESTATÍSTICAS: 12 msgs in / 8 out / 5 dias desde 1ª DM
        ÚLTIMO INTENT: deposito_promessa (há 2 dias)
        QUOTEX: conta=12345, balance=$0, deposits=$0
        TAGS: [promete_amanha]
        OBJEÇÕES: "no tengo dinero ahora" (preço, há 3 dias)
        RESUMO: ...
        ÚLTIMAS 10 MSGS:
        [IN] hola, como hago pra entrar?
        [OUT] te paso o link...
        ...
    """
    if not ctx_dict:
        return "LEAD: (sem contexto)"

    lead = ctx_dict.get("lead", {})
    stats = ctx_dict.get("stats", {})
    quotex = ctx_dict.get("quotex", {})
    flags = ctx_dict.get("flags", {})

    parts = []
    parts.append(f"LEAD: {lead.get('display_name', '?')} (id={lead.get('telegram_id')})")
    parts.append(f"LIFECYCLE: {ctx_dict.get('lifecycle', '?')}")
    parts.append(f"ALVO REMARKETING: {ctx_dict.get('remarketing_target', '?')}")
    parts.append(
        f"STATS: {stats.get('msg_in', 0)} msgs IN / {stats.get('msg_out', 0)} OUT, "
        f"{stats.get('days_since_first_dm', '?')} dias desde 1ª DM"
    )
    if ctx_dict.get("last_intent"):
        parts.append(f"ÚLTIMO INTENT: {ctx_dict['last_intent']} ({ctx_dict.get('last_intent_at', '?')})")
    if quotex.get("has_account"):
        parts.append(
            f"QUOTEX: conta={quotex.get('account_id')}, "
            f"balance=${quotex.get('balance_usd') or 0:.2f}, "
            f"deposits=${quotex.get('deposits_total') or 0:.2f}, "
            f"depositou={quotex.get('has_deposited')}"
        )
    if ctx_dict.get("tags"):
        parts.append(f"TAGS: {', '.join(ctx_dict['tags'])}")
    if ctx_dict.get("objections"):
        objs = ctx_dict["objections"][-3:]  # últimas 3
        parts.append(
            f"OBJEÇÕES: {'; '.join(o.get('text', '')[:80] for o in objs)}"
        )
    if flags.get("opted_out"):
        parts.append("⚠ OPTED_OUT (não enviar mais!)")
    if flags.get("blocked"):
        parts.append("⚠ BLOCKED (lead bloqueou bot)")
    if ctx_dict.get("notes_admin"):
        parts.append(f"NOTAS DO ADMIN: {ctx_dict['notes_admin'][:200]}")
    if ctx_dict.get("summary"):
        parts.append(f"RESUMO: {ctx_dict['summary']}")

    last_msgs = ctx_dict.get("last_messages", [])
    if last_msgs:
        parts.append("\nÚLTIMAS 10 MSGS:")
        for m in last_msgs[-10:]:
            tag = "IN" if m.get("direction") == "in" else "OUT"
            parts.append(f"[{tag}] {(m.get('content') or '')[:150]}")

    full = "\n".join(parts)
    if len(full) > max_chars:
        full = full[:max_chars] + "\n[...truncado]"
    return full
