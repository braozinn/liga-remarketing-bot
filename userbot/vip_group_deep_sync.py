"""Deep sync do grupo privado VIP — quebra o limite de 200 membros.

Problema: client.iter_participants(group) só lista até 200 membros mesmo
com aggressive=True em grupos grandes. Resultado: leads que estão no
grupo recebem remarketing porque não foram detectados como membros.

Solução: pra cada lead no DB, faz client.iter_participants(group,
search=nome_ou_username, limit=10). Telegram suporta busca por nome
e retorna match exato. Lento mas garantido.

Workflow:
1. Pega lista de leads do DB que NÃO estão marcados como in_private_group
2. Pra cada um, busca no grupo por:
   - username (mais confiável)
   - first_name
   - first_name + last_name
   - telegram_id (não funciona com search, mas tenta direct lookup)
3. Se algum match encontrar telegram_id certo, marca in_private_group=True
4. Delay entre buscas pra não bater rate limit
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime
from typing import Optional

logger = logging.getLogger(__name__)

# Estado global do job (pra UI ver progresso)
_sync_state = {
    "running": False,
    "started_at": None,
    "finished_at": None,
    "total": 0,
    "processed": 0,
    "in_group": 0,
    "not_in_group": 0,
    "errors": 0,
    "current_lead": None,
}


def get_sync_state() -> dict:
    """Retorna estado do sync atual (pra UI mostrar progresso)."""
    return dict(_sync_state)


async def deep_check_lead_in_group(
    client, group_entity, lead, max_search_results: int = 10
) -> tuple[bool, str]:
    """Verifica se um lead específico está no grupo VIP, fazendo busca por nome.

    Tenta múltiplas estratégias:
    1. Busca por username (se houver) — mais confiável
    2. Busca por first_name (se único o suficiente)
    3. Busca por first_name + last_name
    4. Get participant direto pelo ID (se Telethon permitir)

    Returns (is_in_group, method_matched).
    """
    if not lead:
        return False, "no_lead"

    queries = []
    if lead.username:
        queries.append(("username", lead.username[:32]))
    if lead.first_name:
        queries.append(("first_name", lead.first_name[:32]))
        if lead.last_name:
            queries.append(("full_name", f"{lead.first_name} {lead.last_name}"[:32]))

    for method, query in queries:
        if not query or len(query) < 2:
            continue
        try:
            async for member in client.iter_participants(
                group_entity, search=query, limit=max_search_results
            ):
                if getattr(member, "id", None) == lead.telegram_id:
                    return True, method
        except Exception as e:
            logger.debug("[deep_check] busca falhou (%s=%r): %s", method, query, e)
        await asyncio.sleep(0.3)  # rate limit por busca

    # Tenta direct lookup pelo ID (último recurso)
    try:
        from telethon.tl.functions.channels import GetParticipantRequest
        result = await client(GetParticipantRequest(
            channel=group_entity, participant=lead.telegram_id
        ))
        if result:
            return True, "direct_id_lookup"
    except Exception:
        pass  # Não é membro ou erro silencioso

    return False, "not_found"


async def full_deep_sync_vip_group(
    only_unmarked: bool = True,
    delay_between_leads: float = 0.8,
) -> dict:
    """Itera TODOS os leads do DB e verifica se estão no grupo VIP via busca.

    Lento mas garantido — quebra o limite de 200 membros.

    Args:
        only_unmarked: se True (default), só processa leads com
                      in_private_group == False (mais rápido). Se False,
                      re-verifica todos.
        delay_between_leads: segundos entre cada lead (rate limit).

    Returns dict com resultado.
    """
    global _sync_state
    if _sync_state["running"]:
        return {"error": "sync já em andamento", "state": dict(_sync_state)}

    from db import SessionLocal
    from db.models import Lead
    from userbot.client import get_client
    from userbot.leads import get_private_group_entity

    try:
        client = await get_client()
        group = await get_private_group_entity()
        if not group:
            return {"error": "PRIVATE_GROUP não configurado"}
    except Exception as e:
        return {"error": f"erro inicializando: {e}"}

    # Pega leads
    with SessionLocal() as s:
        q = s.query(Lead).filter(Lead.telegram_id.isnot(None))
        if only_unmarked:
            q = q.filter(
                (Lead.in_private_group == False) | (Lead.in_private_group.is_(None))
            )
        leads = q.all()
        leads_data = [
            {
                "id": l.id,
                "telegram_id": l.telegram_id,
                "username": l.username,
                "first_name": l.first_name,
                "last_name": l.last_name,
                "display_name": l.display_name or l.first_name or l.username or str(l.telegram_id),
            }
            for l in leads
        ]

    _sync_state.update({
        "running": True,
        "started_at": datetime.utcnow().isoformat(),
        "finished_at": None,
        "total": len(leads_data),
        "processed": 0,
        "in_group": 0,
        "not_in_group": 0,
        "errors": 0,
        "current_lead": None,
    })

    logger.info(
        "[deep_sync] iniciando sync de %d leads (only_unmarked=%s, delay=%.1fs)",
        len(leads_data), only_unmarked, delay_between_leads,
    )

    matches = []  # leads que apareceram no grupo

    # Usa SimpleNamespace pra objeto-like seguro (em vez de class ad-hoc)
    from types import SimpleNamespace

    try:
        for ld in leads_data:
            _sync_state["current_lead"] = ld["display_name"]
            try:
                # SimpleNamespace é o jeito padrão Python de criar objeto
                # com atributos sem definir class. Mais limpo + idiomatico.
                lobj = SimpleNamespace(
                    id=ld["id"],
                    telegram_id=ld["telegram_id"],
                    username=ld["username"],
                    first_name=ld["first_name"],
                    last_name=ld["last_name"],
                )

                is_in, method = await deep_check_lead_in_group(client, group, lobj)

                if is_in:
                    _sync_state["in_group"] += 1
                    matches.append({
                        "lead_id": ld["id"],
                        "display_name": ld["display_name"],
                        "method": method,
                    })
                    # Atualiza no banco
                    with SessionLocal() as s:
                        fresh = s.query(Lead).get(ld["id"])
                        if fresh:
                            fresh.in_private_group = True
                            s.commit()
                    logger.info(
                        "[deep_sync] ✓ %s ESTÁ no grupo (via %s) — marcado",
                        ld["display_name"], method,
                    )
                else:
                    _sync_state["not_in_group"] += 1

            except Exception:
                _sync_state["errors"] += 1
                logger.exception("[deep_sync] erro pra lead id=%d", ld["id"])

            _sync_state["processed"] += 1

            if _sync_state["processed"] % 25 == 0:
                logger.info(
                    "[deep_sync] progresso: %d/%d (in_group=%d, errors=%d)",
                    _sync_state["processed"], _sync_state["total"],
                    _sync_state["in_group"], _sync_state["errors"],
                )

            await asyncio.sleep(delay_between_leads)
    finally:
        _sync_state["running"] = False
        _sync_state["finished_at"] = datetime.utcnow().isoformat()
        _sync_state["current_lead"] = None

    logger.info(
        "[deep_sync] terminado: %d/%d processados, %d no grupo (corrigidos), %d erros",
        _sync_state["processed"], _sync_state["total"],
        _sync_state["in_group"], _sync_state["errors"],
    )

    return {
        "ok": True,
        "state": dict(_sync_state),
        "matches": matches[:100],  # primeiros 100 pra não sobrecarregar resposta
    }
