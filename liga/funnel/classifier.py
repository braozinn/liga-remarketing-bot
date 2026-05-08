"""Classificador de intenção de DMs via Claude Haiku.

Usado pelo dispatcher do funil pra decidir transição da state machine.
Recebe estado atual + mensagem + histórico, retorna {intent, confidence}.

Modelo: claude-haiku-4-5 (rápido, barato, preciso o suficiente).
Custo típico: ~$0.0002 por classificação.
"""
from __future__ import annotations

import json
import logging
from typing import Optional

logger = logging.getLogger(__name__)


# Catálogo de intents reconhecidas pelo classificador
ALL_INTENTS = [
    "quer_entrar_vip",      # interesse em entrar no grupo VIP
    "confirmou",            # "listo", "hecho", "ya está"
    "enviou_id_texto",      # número de ID em texto
    "enviou_id_imagem",     # imagem (handler decide se tem ID)
    "tem_duvida",           # pergunta sobre processo
    "objecao",              # resistência, desconfiança
    "desistiu",             # "mejor no", "después te aviso"
    "off_topic",            # qualquer outra coisa
    "deposito_promessa",    # "voy a depositar mañana"
    "saudacao",             # "hola", "buenos días"
]


# Quais intents são válidas em cada estado (pra prompt focado)
INTENTS_BY_STATE = {
    "new":              ["quer_entrar_vip", "saudacao", "tem_duvida", "off_topic"],
    "onboarding":       ["confirmou", "tem_duvida", "objecao", "desistiu", "off_topic"],
    "waiting_id":       ["enviou_id_texto", "enviou_id_imagem", "tem_duvida", "objecao", "off_topic"],
    "waiting_deposit":  ["confirmou", "deposito_promessa", "tem_duvida", "objecao", "off_topic"],
    "waitlist":         ["confirmou", "deposito_promessa", "tem_duvida", "off_topic"],
    "active":           ["confirmou", "off_topic", "tem_duvida"],
    "at_risk":          ["confirmou", "deposito_promessa", "off_topic"],
    "eliminated":       ["off_topic"],
    "finalist":         ["off_topic"],
}


_SYSTEM_PROMPT_TEMPLATE = """Sos un clasificador de intención para un bot de funnel de ventas en Telegram (en español, audiencia argentina). Tu único trabajo es identificar la intención del último mensaje del lead y devolver un JSON.

ESTADO ACTUAL DEL LEAD: {estado}

INTENCIONES VÁLIDAS EN ESTE ESTADO: {intents_validas}

HISTORIAL RECIENTE (últimos {n_hist} mensajes):
{historial}

ÚLTIMO MENSAJE RECIBIDO: "{mensaje}"

{few_shot_block}LISTA DE INTENCIONES Y CUÁNDO USARLAS:

- `quer_entrar_vip`: el lead expresa interés en entrar al grupo VIP/privado/de señales (ej: "Quiero participar en el VIP", "Como entro?", "Información VIP", "Quiero las señales", "Como me sumo?")
- `confirmou`: el lead dice que completó el paso pedido (ej: "Listo", "Hecho", "Ya está", "Ok ya hice", "Pronto", "Ya", "Ya me registré", "Ya deposité")
- `enviou_id_texto`: el mensaje contiene un número de ID de cuenta Quotex (típicamente 7-9 dígitos), con o sin prefijo "ID:" / "id:" / "Mi id es"
- `enviou_id_imagem`: el mensaje contiene una IMAGEN/screenshot (no texto) — esto se detecta separadamente, vos solo respondes esto si dice "[image]" o similar
- `tem_duvida`: el lead hace una pregunta sobre el proceso (ej: "Cuanto tengo que depositar?", "Es seguro?", "Como funciona?", "Que es Quotex?", "Por que tengo que crear cuenta nueva?")
- `objecao`: el lead resiste, desconfía, o no quiere hacer un paso (ej: "No quiero crear cuenta nueva", "No tengo dinero ahora", "Es estafa?", "Y si pierdo la plata?")
- `deposito_promessa`: el lead dice que va a depositar pronto pero no lo hizo aún (ej: "voy a depositar mañana", "esta semana lo hago", "ya casi")
- `desistiu`: el lead dice claramente que no quiere seguir (ej: "Mejor no", "Después te aviso", "No me interesa", "Olvidalo")
- `saudacao`: solo un saludo sin contexto (ej: "Hola", "Buenas", "Buenos días")
- `off_topic`: cualquier otra cosa que no encaje (comentarios random, mensajes confusos, otros idiomas, audios sin transcripción)

REGLAS:
1. Devolvé SOLO el JSON, sin texto antes ni después, sin markdown.
2. La intención DEBE estar en la lista de "INTENCIONES VÁLIDAS EN ESTE ESTADO". Si no encaja, devolvé "off_topic".
3. Si confidence < 0.7, preferí "off_topic" para forzar escalada humana.
4. No inventes intenciones fuera de la lista.

FORMATO DE RESPUESTA (JSON exacto):
{{"intent": "<una de la lista>", "confidence": <0.0 a 1.0>}}"""


# Confidence mínima por intent — mais ALTA pra intents "críticas" (primeiro
# contato, dispara fluxo grande), mais baixa pra ações neutras.
MIN_CONFIDENCE_BY_INTENT = {
    "quer_entrar_vip":    0.85,  # crítico: dispara funil inteiro de aquisição
    "confirmou":          0.80,  # importante: avança estado
    "enviou_id_texto":    0.80,
    "deposito_promessa":  0.75,
    "objecao":            0.75,
    "tem_duvida":         0.75,
    "saudacao":           0.70,
    "desistiu":           0.80,
    "off_topic":          0.0,
    "enviou_id_imagem":   1.0,
}


def _build_few_shot_block(intents_for_state: list[str]) -> str:
    """Monta bloco de few-shot examples a partir de intents aprendidos do banco.

    Carrega data/learned_intents.json e injeta as top frases reais que leads
    usaram. Se não houver dados aprendidos ainda, retorna string vazia.
    """
    try:
        from liga.funnel.learn_intents import get_learned_examples_for_intent
    except Exception:
        return ""

    blocks = []
    for intent in intents_for_state:
        examples = get_learned_examples_for_intent(intent, top_n=8)
        if not examples:
            continue
        examples_str = "\n".join(f"  • \"{e}\"" for e in examples)
        blocks.append(f"Ejemplos REALES de '{intent}' (frases que leads ya usaron):\n{examples_str}")

    if not blocks:
        return ""

    return (
        "EJEMPLOS APRENDIDOS DEL HISTORIAL REAL DE DMs:\n"
        + "\n\n".join(blocks)
        + "\n\n---\n\n"
    )


def classify_intent(
    message: str,
    state: str,
    history: Optional[list] = None,
    is_image: bool = False,
) -> dict:
    """Classifica intent da mensagem usando Claude Haiku.

    Returns dict {"intent": str, "confidence": float, "raw": str (opcional erro)}.
    Em caso de erro, retorna {"intent": "off_topic", "confidence": 0.0, "raw": err}.
    """
    # Se for imagem direta, não chama IA (já é detectável)
    if is_image:
        return {"intent": "enviou_id_imagem", "confidence": 1.0, "raw": "image_direct"}

    if not message or not message.strip():
        return {"intent": "off_topic", "confidence": 0.0}

    intents_for_state = INTENTS_BY_STATE.get(state, ["off_topic"])

    # ═══ ATALHO: match exato com padrão aprendido ═══
    # Se a mensagem é IDÊNTICA (normalizada) a uma frase já confirmada como
    # 'quer_entrar_vip' (ou outro intent aprendido), retorna direto sem chamar IA.
    # Economiza ~$0.0002 por chamada e dá resposta instantânea.
    try:
        from liga.funnel.learn_intents import match_learned_pattern
        for candidate_intent in intents_for_state:
            if candidate_intent == "off_topic":
                continue
            if match_learned_pattern(message, candidate_intent):
                logger.info(
                    "[classifier] match exato com padrão aprendido → intent=%s (sem IA)",
                    candidate_intent,
                )
                return {
                    "intent": candidate_intent,
                    "confidence": 0.99,
                    "raw": "learned_exact_match",
                }
    except Exception:
        logger.debug("[classifier] erro no match aprendido", exc_info=True)

    # Monta histórico
    hist_str = ""
    if history:
        for h in history[-4:]:
            who = "Lead" if h.get("direction") == "in" else "Você"
            hist_str += f"{who}: {h.get('content', '')[:200]}\n"
    if not hist_str:
        hist_str = "(sin historial)"

    # Few-shot examples a partir do banco aprendido
    few_shot_block = _build_few_shot_block(intents_for_state)

    prompt = _SYSTEM_PROMPT_TEMPLATE.format(
        estado=state,
        intents_validas=", ".join(intents_for_state),
        n_hist=min(4, len(history) if history else 0),
        historial=hist_str.strip(),
        mensaje=(message or "")[:500].replace('"', '\\"'),
        few_shot_block=few_shot_block,
    )

    try:
        from ai.providers import generate_completion, _record_usage
        # Como Haiku já vem por padrão, generate_completion usa Anthropic/Haiku
        response = generate_completion(
            system="You are a classifier. Respond ONLY with valid JSON.",
            user=prompt,
            max_tokens=50,
            temperature=0.0,
        )
    except Exception as e:
        logger.exception("[classifier] erro chamando Claude")
        return {"intent": "off_topic", "confidence": 0.0, "raw": f"error: {e}"}

    # Parse JSON
    try:
        # Limpa possíveis markdown code blocks
        clean = response.strip()
        if clean.startswith("```"):
            clean = clean.split("\n", 1)[1] if "\n" in clean else clean[3:]
            clean = clean.rsplit("```", 1)[0]
        clean = clean.strip()

        data = json.loads(clean)
        intent = data.get("intent", "off_topic")
        confidence = float(data.get("confidence", 0.0))

        # Valida que intent tá na lista válida do estado
        if intent not in intents_for_state and intent != "off_topic":
            logger.warning(
                "[classifier] intent %s não é válido em estado %s — fallback off_topic",
                intent, state,
            )
            return {"intent": "off_topic", "confidence": 0.0, "raw": response}

        # Confidence mínima POR INTENT (mais rígido pra intents críticos)
        min_conf_for_intent = MIN_CONFIDENCE_BY_INTENT.get(intent, 0.7)
        if confidence < min_conf_for_intent:
            logger.info(
                "[classifier] confidence %.2f < %.2f exigido pra '%s' — fallback off_topic",
                confidence, min_conf_for_intent, intent,
            )
            return {
                "intent": "off_topic",
                "confidence": confidence,
                "raw": response,
                "rejected_intent": intent,
                "min_required": min_conf_for_intent,
            }

        logger.info(
            "[classifier] estado=%s msg=%r → intent=%s (conf=%.2f, exigido=%.2f) ✓",
            state, (message or "")[:60], intent, confidence, min_conf_for_intent,
        )
        return {"intent": intent, "confidence": confidence, "raw": response}
    except Exception as e:
        logger.warning("[classifier] erro parseando JSON: %s — raw: %s", e, response[:200])
        return {"intent": "off_topic", "confidence": 0.0, "raw": response}
