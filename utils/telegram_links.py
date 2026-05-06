"""Parser de links do Telegram + classificador de respostas heurístico."""
from __future__ import annotations

import re
from typing import Optional


_TME_PUBLIC = re.compile(r"^https?://t\.me/(?P<username>[A-Za-z0-9_]{4,})/(?P<msg>\d+)/?", re.IGNORECASE)
_TME_PRIVATE = re.compile(r"^https?://t\.me/c/(?P<chat>\d+)/(?:\d+/)?(?P<msg>\d+)/?", re.IGNORECASE)


def parse_telegram_link(link: str) -> Optional[dict]:
    """Recebe um link do Telegram e retorna {chat, message_id, kind}.

    Suporta:
    - https://t.me/username/123          -> público
    - https://t.me/c/1234567890/123      -> privado (canal/grupo)
    - https://t.me/c/1234567890/45/123   -> privado com tópico (pega 123)

    Para Saved Messages, use o link que aparece quando você clica direito
    em uma mensagem no seu próprio chat e copia o link da mensagem.

    Retorna None se o link não bate com nenhum padrão.
    """
    if not link:
        return None
    link = link.strip()

    m = _TME_PRIVATE.match(link)
    if m:
        chat_id = int(m.group("chat"))
        # Telegram channels privados precisam de prefixo -100 quando passados como id
        chat = -1000000000000 - chat_id  # -100 + chat_id
        return {
            "chat": str(chat),
            "message_id": int(m.group("msg")),
            "kind": "private",
            "raw_link": link,
        }

    m = _TME_PUBLIC.match(link)
    if m:
        username = m.group("username").lower()
        # 'c' é reservado pra privado
        if username == "c":
            return None
        return {
            "chat": "@" + username,
            "message_id": int(m.group("msg")),
            "kind": "public",
            "raw_link": link,
        }

    return None


# ---------------------------------------------------------------------------
# Classificador de resposta - 100% heurístico, não precisa de IA
# ---------------------------------------------------------------------------
_CONVERSION_KW = [
    "entré", "ya entré", "entre", "estoy adentro", "adentro", "estoy dentro",
    "pagué", "ya pagué", "pague", "pagado", "comprado", "comprado el curso",
    "entrei", "paguei", "comprei", "tô dentro", "to dentro",
]
_POSITIVE_KW = [
    "sí", "si", "sí me interesa", "me interesa", "claro", "perfecto",
    "perfeto", "quiero", "quiero saber", "quiero más", "vale", "dale",
    "ok", "interesado", "interesada", "más info", "mas info", "más informacion",
    "como pago", "cómo pago", "link", "enlace", "manda el link", "manda link",
    "información", "información por favor",
    "sim", "quero", "tenho interesse", "manda o link", "como funciona",
]
_NEGATIVE_KW = [
    "no me interesa", "no gracias", "no quiero", "ya no", "stop",
    "déjame", "déjame en paz", "para", "no insistas", "no más",
    "não tenho interesse", "não quero", "para de mandar", "me deixa",
]


def classify_reply_heuristic(reply_text: str) -> dict:
    """Classifica resposta sem usar IA. Retorna {classification, confidence}.

    classification ∈ {positive, negative, neutral, conversion}
    """
    text = (reply_text or "").lower().strip()
    if not text:
        return {"classification": "neutral", "confidence": 0.0}

    for kw in _CONVERSION_KW:
        if kw in text:
            return {"classification": "conversion", "confidence": 0.8}
    for kw in _NEGATIVE_KW:
        if kw in text:
            return {"classification": "negative", "confidence": 0.8}
    for kw in _POSITIVE_KW:
        if kw in text:
            return {"classification": "positive", "confidence": 0.7}

    # Sinais fracos
    if "?" in text or len(text.split()) > 5:
        return {"classification": "neutral", "confidence": 0.5}
    return {"classification": "neutral", "confidence": 0.3}
