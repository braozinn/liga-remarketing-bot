# Bot de Remarketing - Telegram Userbot

Userbot que **encaminha** ou **envia** mensagens (em modo AI ou Forward) pra leads que estão no seu grupo VIP mas não no grupo privado pago. Painel local em PT-BR. Setup com 1 comando.

---

## ⚡ Quickstart (3 comandos)

1. **Mova a pasta** pra `C:\bot-remarketing\` (não rode da pasta de cache do Cowork)
2. **Abre no VS Code** → terminal (Ctrl + `) → roda:
   ```
   python setup.py
   ```
3. **Aperta F5** no VS Code

Tudo o resto está em [`TUTORIAL.md`](TUTORIAL.md). **Leia antes de mais nada.**

---

## O que esse bot faz

- Pega leads do **grupo VIP** (`LEADS_SOURCE_GROUP`)
- **Exclui automaticamente** quem está no **grupo privado pago** (`PRIVATE_GROUP`) — em **3 camadas de proteção**
- 2 modos:
  - 🔄 **Forward**: encaminha mensagens das suas "Mensagens Salvas" com origem oculta
  - 🤖 **AI**: gera texto editável em espanhol via Claude Haiku 4.5
- Tracking de respostas → métricas por script/variante → ranking pra escolher o melhor

---

## ⚠️ Erro comum: `No module named uvicorn`

Significa que você apertou F5 sem rodar `python setup.py` antes. O Python do sistema não tem as bibliotecas; tem que usar o `.venv` que o setup cria.

Solução: roda `python setup.py` (ou `py -3.12 setup.py` se tiver Python 3.14) e depois F5.

---

## Estrutura

```
telegram-bot-remarketing/
├── setup.py              ← roda 1 vez pra configurar tudo
├── main.py               ← entrypoint (F5 do VS Code)
├── requirements.txt
├── .env.example
├── README.md
├── TUTORIAL.md           ← passo-a-passo COMPLETO
│
├── vscode-setup/         ← copiado pra .vscode/ pelo setup.py
├── userbot/              ← Telethon (cliente, leads, sender, tracker, scheduler)
├── ai/                   ← geração de scripts em ES (Claude/OpenAI)
├── utils/                ← parser de links Telegram + classificação heurística
├── db/                   ← SQLAlchemy + SQLite
└── web/                  ← FastAPI + Jinja2 (PT-BR)
```

---

## Limites recomendados (conta aquecida)

```
SEND_DELAY_MIN=15
SEND_DELAY_MAX=40
MAX_SENDS_PER_HOUR=120
LONG_PAUSE_EVERY=80
LONG_PAUSE_SECONDS=180
```

→ ~1500-2000 envios/dia em 16h.

---

## Garantia: quem está no grupo PRIVADO **nunca** recebe nada

```
Camada 1 — Sync do grupo VIP:    marca quem está no privado como EXCLUDED.
Camada 2 — Início da campanha:   re-puxa o privado, exclui da fila.
Camada 3 — Antes de cada envio:  re-confere o status do lead.
```

Veja o detalhe técnico no fim do `TUTORIAL.md`.

---

## Aviso

Userbots em uso comercial agressivo violam o TOS do Telegram. Mesmo com conta aquecida, **comece com 200/dia** e suba gradualmente. Marque `BLOCKED` quem pedir pra parar. Use por sua conta e risco.
