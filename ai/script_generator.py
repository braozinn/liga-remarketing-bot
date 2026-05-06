"""Geração de scripts em espanhol a partir de briefing PT."""
from __future__ import annotations

import json
import re
from typing import List, Optional

from .providers import generate_completion, AIError


SYSTEM_PROMPT = """Sos un copywriter experto en marketing por Telegram para una audiencia
hispanohablante de Latinoamérica (principalmente Argentina + países vecinos).

ESTILO OBLIGATORIO — leelo bien:
- Escribís como una persona real charlando por DM, NO como una empresa.
- Tono COMPLETAMENTE INFORMAL. Nunca formal, nunca corporativo.
- Usás VOSEO argentino: "vos", "tenés", "querés", "podés", "sabés", "mandá", "fijate",
  "decime", "che", "dale", "bueno", "tranqui", "boludo" (con cariño, NUNCA insultando).
- Sin "Estimado/a", sin "Cordialmente", sin "Atentamente". Eso espanta.
- Naturalidad antes que perfección gramatical: contracciones, "pa", "y bueno", está OK.

REGLAS DE FORMATO:
- TODO en español rioplatense informal.
- Mensajes cortos para Telegram (120–400 caracteres por variante, salvo briefing pedir otra cosa).
- Máximo 1–2 emojis por mensaje, solo si suenan naturales (no decorativos).
- Empezá con algo humano y específico — nunca "Hola, soy de la empresa X".
- Un solo Call-to-Action al final, casual ("dale, contame", "decime si querés", etc).
- NO inventes datos (precios, fechas, nombres). Si faltan, dejá {placeholders}.
- NO uses promesas absolutas ni lenguaje de scam ("ganá millonadas", "100% garantizado").
- Variantes DISTINTAS entre sí (ángulo, gancho, estructura, no solo sinónimos).

FORMATO DE SALIDA — Devolvé SOLO un JSON válido:
{
  "variants": [
    {"label": "A", "text": "..."},
    {"label": "B", "text": "..."},
    {"label": "C", "text": "..."}
  ]
}
Sin texto antes ni después del JSON.
"""


def _build_user_prompt(briefing_pt, objective, n_variants, tone, target_length, extra=""):
    return f"""Briefing del cliente (en portugués, traducir conceptos al español):
---
{briefing_pt}
---

Objetivo: {objective or "remarketing — reactivar lead que no convirtió"}
Tono: {tone}
Longitud aproximada: {target_length} caracteres
Cantidad de variantes: {n_variants}

{extra}

Genera {n_variants} variantes distintas, cada una con un ángulo diferente
(ej: emocional, prueba social, urgencia suave, curiosidad).
Devuelve solo el JSON."""


def generate_script_variants(
    briefing_pt: str,
    objective: str = "",
    n_variants: int = 3,
    tone: str = "rioplatense informal, humano, persuasivo sin ser invasivo",
    target_length: int = 350,
    provider: Optional[str] = None,
) -> List[dict]:
    user = _build_user_prompt(
        briefing_pt, objective,
        max(1, min(5, n_variants)),
        tone, target_length,
    )
    raw = generate_completion(
        system=SYSTEM_PROMPT, user=user,
        max_tokens=2500, temperature=0.9, provider=provider,
    )
    return _parse_variants(raw)


def regenerate_from_winner(
    briefing_pt: str,
    winner_text: str,
    winner_metrics: dict,
    n_variants: int = 3,
    objective: str = "",
    tone: str = "amigável, próximo, persuasivo",
    target_length: int = 350,
    provider: Optional[str] = None,
) -> List[dict]:
    metrics_str = (
        f"reply_rate={winner_metrics.get('reply_rate', 0):.1%}, "
        f"positive_rate={winner_metrics.get('positive_rate', 0):.1%}, "
        f"conversion_rate={winner_metrics.get('conversion_rate', 0):.1%}"
    )
    extra = f"""APRENDIZAJE: La variante con mejores métricas hasta ahora ({metrics_str}) fue:
\"\"\"
{winner_text}
\"\"\"
Crea variantes nuevas que mantengan los elementos ganadores (estructura, ángulo, gancho)
pero prueba pequeñas variaciones (CTA, primera línea, prueba social, longitud)
para seguir mejorando."""

    user = _build_user_prompt(
        briefing_pt, objective,
        max(1, min(5, n_variants)),
        tone, target_length, extra,
    )
    raw = generate_completion(
        system=SYSTEM_PROMPT, user=user,
        max_tokens=2500, temperature=0.7, provider=provider,
    )
    return _parse_variants(raw)


def _parse_variants(raw: str) -> List[dict]:
    if not raw:
        raise AIError("IA retornou texto vazio")
    text = raw.strip()
    text = re.sub(r"^```(?:json)?\s*", "", text)
    text = re.sub(r"\s*```$", "", text)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.DOTALL)
        if not match:
            raise AIError(f"IA não retornou JSON válido: {raw[:300]}")
        try:
            data = json.loads(match.group(0))
        except json.JSONDecodeError as e:
            raise AIError(f"JSON inválido: {e}") from e

    variants = data.get("variants") or []
    if not variants:
        raise AIError(f"JSON sem 'variants': {data}")

    out = []
    for i, v in enumerate(variants):
        text_es = (v.get("text") or "").strip()
        if not text_es:
            continue
        label = (v.get("label") or chr(ord("A") + i)).strip()
        out.append({"label": label, "text": text_es})
    if not out:
        raise AIError("Nenhuma variante válida na resposta da IA")
    return out
