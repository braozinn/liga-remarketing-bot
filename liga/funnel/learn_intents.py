"""Aprende intents a partir do histórico real de DMs no banco.

Ideia: o classifier do funil usa Claude Haiku pra adivinhar o intent de
cada mensagem. Mas o Haiku acerta MAIS quando recebe few-shot examples
das frases que leads REAIS usam. Em vez de inventar exemplos, escaneamos
o banco de DMs já catalogadas e aprendemos automaticamente.

Como funciona:
1. scan_intent_examples() lê todas as mensagens IN dos últimos N dias
2. Pré-filtra com keywords óbvias (vip, grupo, entrar, etc) — pra economizar IA
3. Manda cada mensagem em batch pro Haiku perguntar "essa msg = {intent}?"
4. Salva as confirmadas em data/learned_intents.json com contagem

Depois o classifier carrega esse JSON e:
- Usa as top frases como few-shot examples no system prompt
- Adicionalmente, faz match exato com as frases aprendidas pra pular IA
  (economiza $ — se a frase é IDÊNTICA a uma já confirmada, retorna direto)
"""
from __future__ import annotations

import json
import logging
import os
import re
import unicodedata
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

from db import SessionLocal
from db.models import Lead, LeadMessage

logger = logging.getLogger(__name__)


# Diretório onde salva o aprendizado. Cria se não existe.
DATA_DIR = Path(os.getenv("LIGA_DATA_DIR", "data"))
DATA_DIR.mkdir(parents=True, exist_ok=True)
LEARNED_FILE = DATA_DIR / "learned_intents.json"
BANNED_FILE = DATA_DIR / "banned_intents.json"  # frases que NUNCA devem aparecer


# Keywords (sem acento, lowercase) usadas como pré-filtro pra `quer_entrar_vip`.
# Filtro liberal — manda mais pra IA, ela decide. Falsos positivos aqui são OK.
VIP_PREFILTER_KEYWORDS = {
    # Espanhol
    "vip", "grupo", "privado", "canal", "premium", "exclusivo", "señales",
    "senales", "entrar", "unirme", "sumar", "sumarme", "ingresar", "acceso",
    "acceder", "como hago", "quiero entrar", "quiero participar", "como me",
    "como funciona el grupo", "como me uno", "informacion", "info",
    # Português (LatAm misturada)
    "quero entrar", "como faço", "grupo privado", "grupo vip", "fazer parte",
    # Genérico
    "interesado", "interesada", "interes", "list", "broker", "quotex",
}


def _normalize(text: str) -> str:
    """Remove acentos, lowercase, colapsa espaços. Pra match estável."""
    if not text:
        return ""
    nfkd = unicodedata.normalize("NFKD", text)
    no_accents = "".join(c for c in nfkd if not unicodedata.combining(c))
    return re.sub(r"\s+", " ", no_accents.lower().strip())


def _matches_vip_prefilter(text: str) -> bool:
    """Heurística rápida — texto tem alguma palavra-chave 'VIP-ish'?"""
    norm = _normalize(text)
    if not norm or len(norm) < 3 or len(norm) > 200:
        return False
    return any(kw in norm for kw in VIP_PREFILTER_KEYWORDS)


def load_banned() -> dict:
    """Carrega lista de frases banidas (norms que NUNCA devem voltar)."""
    if not BANNED_FILE.exists():
        return {"intents": {}}
    try:
        return json.loads(BANNED_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {"intents": {}}


def save_banned(data: dict) -> None:
    """Salva lista de frases banidas atomicamente."""
    BANNED_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = BANNED_FILE.with_suffix(".tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp_path.write_text(payload, encoding="utf-8")
    tmp_path.replace(BANNED_FILE)
    logger.info("[learn_intents] banidos salvos: %s", BANNED_FILE.resolve())


def add_to_banned(intent: str, norm: str, text: str = "") -> None:
    """Marca uma frase como banida — scans futuros vão pular ela."""
    if not norm:
        return
    data = load_banned()
    intents = data.setdefault("intents", {})
    banned_list = intents.setdefault(intent, [])
    # Evita duplicata
    norms_set = {b.get("norm") for b in banned_list if isinstance(b, dict)}
    if norm not in norms_set:
        banned_list.append({"norm": norm, "text": text or norm})
        data["intents"] = intents
        save_banned(data)
        logger.info("[learn_intents] BANIDO: norm=%r intent=%s", norm, intent)


def remove_from_banned(intent: str, norm: str) -> bool:
    """Remove uma frase da lista de banidos (caso você tenha banido por engano)."""
    data = load_banned()
    intents = data.get("intents", {})
    banned_list = intents.get(intent, [])
    new_list = [b for b in banned_list if b.get("norm") != norm]
    if len(new_list) == len(banned_list):
        return False
    intents[intent] = new_list
    data["intents"] = intents
    save_banned(data)
    return True


def is_banned(intent: str, norm: str) -> bool:
    """Verifica se uma frase está banida."""
    data = load_banned()
    banned_list = data.get("intents", {}).get(intent, [])
    return any(b.get("norm") == norm for b in banned_list if isinstance(b, dict))


def auto_add_learned(intent: str, text: str, source: str = "auto_classify") -> bool:
    """Adiciona uma frase ao aprendizado automaticamente (vinda da classificação
    em tempo real do Haiku). Atualiza count se já existe.

    Skipa se:
    - intent é off_topic ou similar
    - frase está banida
    - texto vazio ou muito longo
    """
    if not text or not intent or intent in ("off_topic", "saudacao"):
        return False
    text = text.strip()
    if len(text) > 200 or len(text) < 3:
        return False

    norm = _normalize(text)
    if not norm:
        return False

    # Pula banidas — user não quer essas
    if is_banned(intent, norm):
        return False

    data = load_learned()
    intents = data.setdefault("intents", {})
    examples = intents.setdefault(intent, [])

    # Procura se já existe (atualiza count) ou adiciona
    found = False
    for ex in examples:
        if ex.get("norm") == norm:
            ex["count"] = (ex.get("count") or 0) + 1
            ex["last_seen"] = datetime.utcnow().isoformat()
            found = True
            break

    if not found:
        examples.append({
            "text": text,
            "norm": norm,
            "count": 1,
            "source": source,
            "first_seen": datetime.utcnow().isoformat(),
            "last_seen": datetime.utcnow().isoformat(),
        })
        # Re-ordena por count desc, mantém top 100
        examples.sort(key=lambda e: -(e.get("count") or 0))
        if len(examples) > 100:
            examples = examples[:100]
        intents[intent] = examples

    data["intents"] = intents
    save_learned(data, update_timestamp=False)

    # Telemetria — incrementa contadores diários
    try:
        _bump_learning_stats("auto_added" if not found else "auto_count_bumped")
    except Exception:
        pass

    return True


def _bump_learning_stats(metric: str) -> None:
    """Incrementa contador diário de aprendizado pra UI mostrar."""
    stats_file = DATA_DIR / "learning_stats.json"
    today = datetime.utcnow().strftime("%Y-%m-%d")
    try:
        if stats_file.exists():
            data = json.loads(stats_file.read_text(encoding="utf-8"))
        else:
            data = {}
        day = data.setdefault(today, {})
        day[metric] = (day.get(metric) or 0) + 1
        # Mantém só últimos 30 dias
        for k in list(data.keys()):
            if k < (datetime.utcnow() - timedelta(days=30)).strftime("%Y-%m-%d"):
                data.pop(k, None)
        stats_file.write_text(json.dumps(data, indent=2), encoding="utf-8")
    except Exception:
        logger.exception("[learn_intents] erro bumpando stats")


def get_learning_stats(days: int = 7) -> dict:
    """Retorna stats de aprendizado dos últimos N dias."""
    stats_file = DATA_DIR / "learning_stats.json"
    if not stats_file.exists():
        return {"daily": {}, "totals": {}}
    try:
        data = json.loads(stats_file.read_text(encoding="utf-8"))
        cutoff = (datetime.utcnow() - timedelta(days=days)).strftime("%Y-%m-%d")
        daily = {k: v for k, v in data.items() if k >= cutoff}
        totals = {}
        for day_data in daily.values():
            for metric, val in day_data.items():
                totals[metric] = (totals.get(metric) or 0) + val
        return {"daily": daily, "totals": totals, "days": days}
    except Exception:
        return {"daily": {}, "totals": {}}


def load_learned() -> dict:
    """Carrega o JSON de intents aprendidos. Retorna dict vazio se não existir."""
    if not LEARNED_FILE.exists():
        logger.debug("[learn_intents] LEARNED_FILE não existe: %s", LEARNED_FILE.resolve())
        return {"intents": {}, "last_scan_at": None, "stats": {}}
    try:
        content = LEARNED_FILE.read_text(encoding="utf-8")
        data = json.loads(content)
        return data
    except Exception:
        logger.exception("[learn_intents] erro lendo %s — começando do zero", LEARNED_FILE.resolve())
        return {"intents": {}, "last_scan_at": None, "stats": {}}


def save_learned(data: dict, update_timestamp: bool = True) -> None:
    """Salva o JSON de intents aprendidos. Garante flush em disco.

    Args:
        data: dict completo a salvar
        update_timestamp: se True, atualiza last_scan_at (default).
                         Set False quando salvando após delete (não foi um scan).
    """
    if update_timestamp:
        data["last_scan_at"] = datetime.utcnow().isoformat()
    else:
        data["last_modified_at"] = datetime.utcnow().isoformat()

    # Garante que o diretório existe
    LEARNED_FILE.parent.mkdir(parents=True, exist_ok=True)

    # Escreve via tmp file + rename (atomic) pra evitar corrupção
    tmp_path = LEARNED_FILE.with_suffix(".json.tmp")
    payload = json.dumps(data, ensure_ascii=False, indent=2)
    tmp_path.write_text(payload, encoding="utf-8")
    # No Windows replace é atomic; no Linux também
    tmp_path.replace(LEARNED_FILE)

    # Confirma escrita re-lendo
    try:
        verify = json.loads(LEARNED_FILE.read_text(encoding="utf-8"))
        n_intents = sum(len(v) for v in verify.get("intents", {}).values() if isinstance(v, list))
        logger.info(
            "[learn_intents] salvo %s (%d frases totais, %d bytes)",
            LEARNED_FILE.resolve(), n_intents, len(payload),
        )
    except Exception:
        logger.exception("[learn_intents] FALHA ao verificar JSON salvo!")


def scan_vip_intent_examples(
    days_back: int = 90,
    max_messages: int = 0,  # 0 = sem limite (scan tudo)
    use_ai_validation: bool = True,
) -> dict:
    """Escaneia DMs antigas e aprende como leads pedem pra entrar no VIP.

    Args:
        days_back: quantos dias pra trás escanear (default 90)
        max_messages: limita por segurança (default 0 = SEM LIMITE, lê tudo).
                      Se você tem milhões de DMs, talvez queira limitar pra
                      não estourar quota.
        use_ai_validation: se False, só usa heurística (mais barato, menos preciso)

    Returns:
        dict com:
        - examples: lista [{"text": str, "count": int, "norm": str}, ...] ordenada por count desc
        - scanned_messages: total de IN scanned
        - prefiltered: msgs que passaram do prefilter
        - confirmed_by_ai: quantas IA confirmou como quer_entrar_vip
        - cost_estimate: custo aprox em USD
    """
    cutoff = datetime.utcnow() - timedelta(days=days_back)

    # 1. Pega mensagens IN do período
    with SessionLocal() as s:
        q = (
            s.query(LeadMessage)
            .filter(LeadMessage.direction == "in")
            .filter(LeadMessage.created_at >= cutoff)
            .filter(LeadMessage.content.isnot(None))
            .order_by(LeadMessage.created_at.desc())
        )
        # max_messages=0 ou negativo = SEM limite (scan tudo)
        if max_messages and max_messages > 0:
            q = q.limit(max_messages)
        rows = q.all()
        candidates = [
            {"id": r.id, "text": r.content, "lead_id": r.lead_id, "created_at": r.created_at}
            for r in rows
        ]

    scanned = len(candidates)
    logger.info("[learn_intents] escaneando %d msgs IN dos últimos %dd", scanned, days_back)

    # 2. Pré-filtro: descarta msgs que claramente não pedem VIP
    prefiltered = [c for c in candidates if _matches_vip_prefilter(c["text"])]
    logger.info("[learn_intents] %d/%d passaram do prefilter", len(prefiltered), scanned)

    # 3. Validação por IA (opcional)
    confirmed = []
    if use_ai_validation and prefiltered:
        confirmed = _ai_batch_validate_vip_intent(prefiltered)
    else:
        # Sem IA: confia no prefilter
        confirmed = prefiltered

    # 4. Agrupa por texto normalizado e conta frequência
    # Carrega lista de banidos ANTES de agrupar pra excluir desde já
    banned_data = load_banned()
    banned_norms = {
        b.get("norm")
        for b in banned_data.get("intents", {}).get("quer_entrar_vip", [])
        if isinstance(b, dict) and b.get("norm")
    }
    if banned_norms:
        logger.info("[learn_intents] %d frases banidas serão excluídas do resultado", len(banned_norms))

    counter = Counter()
    text_by_norm: dict[str, str] = {}
    skipped_banned = 0
    for c in confirmed:
        text = (c["text"] or "").strip()
        if not text or len(text) > 200:
            continue
        norm = _normalize(text)
        if not norm:
            continue
        if norm in banned_norms:
            skipped_banned += 1
            continue
        counter[norm] += 1
        # Mantém a versão "mais bonita" (com case original) do texto
        if norm not in text_by_norm or len(text) < len(text_by_norm[norm]):
            text_by_norm[norm] = text

    if skipped_banned:
        logger.info("[learn_intents] %d ocorrências de frases banidas filtradas", skipped_banned)

    examples = [
        {"text": text_by_norm[norm], "norm": norm, "count": count}
        for norm, count in counter.most_common(50)  # top 50 padrões
    ]

    # 5. Custo estimado (Haiku ~ $1 / 1M input tokens; cada msg ~50 tokens)
    cost_estimate = round(len(prefiltered) * 50 / 1_000_000 * 1.0, 4) if use_ai_validation else 0.0

    result = {
        "examples": examples,
        "scanned_messages": scanned,
        "prefiltered": len(prefiltered),
        "confirmed_by_ai": len(confirmed),
        "unique_patterns": len(examples),
        "cost_estimate_usd": cost_estimate,
        "days_back": days_back,
    }

    # 6. Salva no JSON aprendido (acumula com scans anteriores)
    data = load_learned()
    intents = data.get("intents", {})
    intents["quer_entrar_vip"] = examples  # sobrescreve o intent escaneado
    data["intents"] = intents
    data["stats"] = result
    save_learned(data)

    logger.info(
        "[learn_intents] scan completo: %d candidatos → %d prefilter → %d confirmados → %d padrões únicos",
        scanned, len(prefiltered), len(confirmed), len(examples),
    )
    return result


def _ai_batch_validate_vip_intent(candidates: list[dict]) -> list[dict]:
    """Pergunta pra Haiku quais mensagens da lista são pedido de entrar no VIP.

    Manda em batches de 20 pra otimizar. Retorna apenas as confirmadas.
    """
    confirmed = []
    BATCH = 20

    for i in range(0, len(candidates), BATCH):
        batch = candidates[i:i+BATCH]
        try:
            from ai.providers import generate_completion
        except Exception:
            logger.exception("[learn_intents] sem AI provider — pulando validação IA")
            return candidates  # devolve todas como "confirmadas" (fallback)

        msgs_numeradas = "\n".join(
            f"{idx+1}. {(c['text'] or '')[:200].replace(chr(10), ' ')}"
            for idx, c in enumerate(batch)
        )

        prompt = (
            "Sos un clasificador. Te paso una lista numerada de mensajes que leads enviaron por DM "
            "a un vendedor de afiliado de Quotex. Marcá cuáles son CLARAMENTE el lead pidiendo entrar "
            "al grupo VIP/privado/de señales/premium (en cualquier idioma — español, portugués).\n\n"
            "Ejemplos POSITIVOS:\n"
            "- 'quiero entrar al vip'\n"
            "- 'como hago para sumarme al grupo'\n"
            "- 'me interesa el privado'\n"
            "- 'quero entrar no grupo'\n"
            "- 'como funciona el vip'\n\n"
            "Ejemplos NEGATIVOS (NO marcar):\n"
            "- 'hola' (saludo simple)\n"
            "- 'ya deposité' (confirmación)\n"
            "- '12345678' (ID de cuenta)\n"
            "- 'gracias' (agradecimiento)\n"
            "- 'cuanto puedo ganar?' (duda general, no pidió entrar)\n\n"
            f"MENSAJES:\n{msgs_numeradas}\n\n"
            "Devolvé SOLO un JSON con los números de los positivos. Ejemplo: "
            "{\"positivos\": [1, 4, 7]} — sin texto extra."
        )

        try:
            response = generate_completion(
                system="You are a strict binary classifier. Respond ONLY with valid JSON.",
                user=prompt,
                max_tokens=200,
                temperature=0.0,
            )
            clean = response.strip()
            if clean.startswith("```"):
                clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
                clean = clean.rsplit("```", 1)[0]
            clean = clean.strip()
            data = json.loads(clean)
            positives = data.get("positivos", [])
            for n in positives:
                if isinstance(n, int) and 1 <= n <= len(batch):
                    confirmed.append(batch[n-1])
        except Exception:
            logger.exception("[learn_intents] erro batch %d — pulando", i)
            continue

    return confirmed


def count_total_in_messages(days_back: Optional[int] = None) -> dict:
    """Conta quantas mensagens IN tem no banco (no período ou total).

    Útil pra mostrar no painel antes do scan: "tem X msgs IN — todas serão lidas".
    """
    from sqlalchemy import func as _func
    with SessionLocal() as s:
        q = s.query(_func.count(LeadMessage.id)).filter(LeadMessage.direction == "in")
        total_all = q.scalar() or 0
        if days_back:
            cutoff = datetime.utcnow() - timedelta(days=days_back)
            total_period = (
                s.query(_func.count(LeadMessage.id))
                .filter(LeadMessage.direction == "in")
                .filter(LeadMessage.created_at >= cutoff)
                .scalar() or 0
            )
        else:
            total_period = total_all
    return {"total_all": total_all, "total_period": total_period, "days_back": days_back}


def delete_learned_pattern(intent: str, key: str, match_field: str = "norm") -> bool:
    """Remove uma frase específica do JSON aprendido.

    Args:
        intent: qual intent (ex: 'quer_entrar_vip')
        key: valor a procurar
        match_field: campo a comparar — 'norm' (default) ou 'text'

    Returns True se removeu, False se não achou.
    """
    data = load_learned()
    intents = data.get("intents", {})
    patterns = intents.get(intent, [])
    before = len(patterns)
    new_patterns = [p for p in patterns if p.get(match_field) != key]
    after = len(new_patterns)

    if before == after:
        logger.info(
            "[learn_intents] DELETE %s=%r não bate em nenhum dos %d padrões",
            match_field, key, before,
        )
        return False

    # Captura os patterns que foram removidos pra adicionar aos banidos
    removed_patterns = [p for p in patterns if p.get(match_field) == key]

    intents[intent] = new_patterns
    data["intents"] = intents
    save_learned(data, update_timestamp=False)

    # Bana cada pattern removido
    for rp in removed_patterns:
        rp_norm = rp.get("norm", "") if isinstance(rp, dict) else ""
        rp_text = rp.get("text", "") if isinstance(rp, dict) else ""
        if rp_norm:
            add_to_banned(intent, rp_norm, rp_text)

    logger.info(
        "[learn_intents] DELETE OK — %s=%r removido do intent=%s (antes=%d, depois=%d) [BANIDOS=%d]",
        match_field, key, intent, before, after, len(removed_patterns),
    )
    return True


def delete_learned_by_index(intent: str, index: int) -> dict:
    """Remove a Nª frase do intent (0-based). Mais robusto que match por
    string — não tem ambiguidade.

    Returns dict com {removed: bool, removed_pattern: dict|None, total_before, total_after}.
    """
    data = load_learned()
    intents = data.get("intents", {})
    patterns = intents.get(intent, [])
    before = len(patterns)

    if index < 0 or index >= before:
        return {
            "removed": False,
            "reason": f"index {index} fora de range (tem {before} frases)",
            "total_before": before,
            "total_after": before,
        }

    removed_pattern = patterns[index]
    new_patterns = patterns[:index] + patterns[index+1:]
    intents[intent] = new_patterns
    data["intents"] = intents
    save_learned(data, update_timestamp=False)

    # IMPORTANTE: também adiciona à lista de banidos pra que scans futuros
    # NÃO recriem essa frase (ela já existe nas DMs reais do banco — se não
    # fosse banida, o próximo scan ressuscitava ela).
    rp_norm = removed_pattern.get("norm", "") if isinstance(removed_pattern, dict) else ""
    rp_text = removed_pattern.get("text", "") if isinstance(removed_pattern, dict) else ""
    if rp_norm:
        add_to_banned(intent, rp_norm, rp_text)

    logger.info(
        "[learn_intents] DELETE BY INDEX OK — idx=%d removeu %r do intent=%s (antes=%d, depois=%d) [BANIDO]",
        index, removed_pattern, intent, before, len(new_patterns),
    )
    return {
        "removed": True,
        "removed_pattern": removed_pattern,
        "total_before": before,
        "total_after": len(new_patterns),
        "banned": True,
    }


def clear_learned_intent(intent: str) -> int:
    """Apaga todas as frases aprendidas de um intent. Retorna quantas removeu."""
    data = load_learned()
    intents = data.get("intents", {})
    n = len(intents.get(intent, []))
    intents[intent] = []
    data["intents"] = intents
    save_learned(data)
    logger.info("[learn_intents] limpou %d padrões do intent=%s", n, intent)
    return n


def get_learned_examples_for_intent(intent: str, top_n: int = 10) -> list[str]:
    """Retorna as top frases aprendidas pro intent (texto puro, sem stats).

    Usado pelo classifier pra montar few-shot examples no prompt do Haiku.
    """
    data = load_learned()
    examples = data.get("intents", {}).get(intent, [])
    return [e["text"] for e in examples[:top_n] if e.get("text")]


def match_learned_pattern(text: str, intent: str = "quer_entrar_vip") -> bool:
    """Match exato (normalizado) com algum padrão aprendido.

    Se True, pode pular chamada IA do classifier. Economiza $.
    """
    norm = _normalize(text)
    if not norm:
        return False
    data = load_learned()
    patterns = data.get("intents", {}).get(intent, [])
    return any(p.get("norm") == norm for p in patterns)
