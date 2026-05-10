# 🤖 Liga · Remarketing Bot — Guia Rápido

Documento curto pra você (e pra IA) **não se perder mais**.

---

## 🎯 O que esse bot faz

Bot Telegram (userbot Telethon) que:
1. **Cataloga** todas as DMs que você recebe
2. **Sugere** ações de remarketing (você aprova/edita antes de mandar)
3. **Analisa** prints de Quotex via Vision (extrai ID + saldo automático)
4. **Valida** IDs no @QuotexPartnerBot (sem você precisar fazer manual)
5. **Filtra** quem já é VIP do remarketing (não envia DM repetida)

**Modo de operação default: PASSIVO** — bot não responde leads sozinho. Você responde.

---

## 🚦 Estados do Lead (lifecycle)

Único campo que importa: `Lead.lifecycle`

```
new ──► lead ──► deposited ──► vip
        │           │            │
   conversou   criou conta    entrou no
              + depositou >$20  grupo VIP
```

Sub-tipo de `lead` (pra remarketing direcionado):
- `lead` SEM `liga_account_id` → "ainda não criou conta"
- `lead` COM `liga_account_id` → "criou conta mas não depositou"

---

## ⚙️ Flags `.env` que importam

| Flag | Default | O que faz |
|---|---|---|
| `AUTO_RESPOND_FUNNEL` | `0` | `1` liga o funil automático (bot responde leads sozinho) |
| `AUTO_REPLY` | `0` | `1` liga handlers da Liga (responde em estados específicos) |
| `AUTO_DM_SCAN` | `0` | `1` liga varreduras IA em batch (caro — só ligar se sabe o que faz) |
| `VISION_SONNET_FALLBACK` | `0` | `1` tenta Sonnet se Haiku falhar em ler ID (custa mais) |
| `ENABLE_LIGA` | `1` | `0` desliga TODOS os jobs/UI da Liga (pós-torneio) |
| `ADMIN_TELEGRAM_ID` | — | ID numérico do @braozin pra receber heartbeat hourly |
| `TELETHON_SESSION` | — | StringSession (sobrevive a redeploy sem .session file) |
| `FUNNEL_DEBOUNCE_SECONDS` | `2` | Tempo de agregação de DMs em rajada |

**Pra ver tudo**: `cat /opt/telegram-bot-remarketing/.env.example`

---

## 🛠 Comandos do dia-a-dia

### Deploy (ATUALIZAR depois de mudar código no GitHub)
```bash
bash /opt/telegram-bot-remarketing/deploy/update.sh
```

### Ver status
```bash
sudo systemctl status liga-bot.service --no-pager -l | head -10
```

### Ver logs em tempo real
```bash
sudo journalctl -u liga-bot.service -f
# Ctrl+C pra sair
```

### Ver logs filtrados (sem `-f`)
```bash
sudo journalctl -u liga-bot.service --since "10 min ago" --no-pager | grep -iE "passivo|vision|partner"
```

### Restart manual (se travou)
```bash
sudo systemctl restart liga-bot.service
```

### Backup imediato do data.db
```bash
cp /opt/telegram-bot-remarketing/data.db /opt/telegram-bot-remarketing/data/backups/data.db.manual.$(date +%Y%m%d_%H%M%S)
```

### Painel web (no PC, via SSH tunnel)
```powershell
ssh -L 8080:127.0.0.1:8080 root@157.230.222.177
```
Daí abre http://localhost:8080 no browser.

---

## 🚨 Comandos de emergência

### PARAR TUDO (matar bot)
```bash
sudo systemctl stop liga-bot.service
```

### Resetar lead específico (volta pra `new`)
```bash
sqlite3 /opt/telegram-bot-remarketing/data.db "UPDATE leads SET lifecycle='new', in_private_group=0, liga_state='new', liga_account_id=NULL, liga_id_status=NULL, opted_out=0 WHERE username='X';"
```

### Ver gasto IA real-time (sem delay Anthropic)
http://localhost:8080/funnel/ai-usage-realtime?hours=24

OU SQL:
```bash
sqlite3 /opt/telegram-bot-remarketing/data.db "SELECT operation, COUNT(*), SUM(cost_usd) FROM ai_usage WHERE created_at > datetime('now', '-24 hours') GROUP BY operation;"
```

### Ver últimos 20 leads catalogados
```bash
sqlite3 /opt/telegram-bot-remarketing/data.db "SELECT id, username, lifecycle, datetime(last_dm_at) FROM leads ORDER BY id DESC LIMIT 20;"
```

---

## 📂 Onde mexer pra cada coisa

### Quero...
| Tarefa | Onde |
|---|---|
| Adicionar novo intent (classifier IA) | `liga/funnel/classifier.py` (lista `ALL_INTENTS`) |
| Adicionar nova etapa do funil VIP | Painel `/automation/funnel/X` (não mexer em código) |
| Mudar prompt do Vision (extração ID) | `ai/providers.py` (`_ACCOUNT_SYSTEM_PROMPT`) |
| Adicionar nova flag `.env` | `.env.example` + ler com `os.getenv("X", "default")` |
| Adicionar novo cron job | `liga/scheduler.py` (`sched.add_job(...)`) |
| Mudar texto que bot manda no funil | Painel `/scripts` ou direto na variant |
| Bloquear lead pra remarketing | Painel `/leads` → marcar `opted_out` |

---

## 📊 Páginas do painel mais úteis

| URL | Pra quê |
|---|---|
| `/` | Dashboard geral |
| `/leads` | Lista de leads (busca, filtros) |
| `/scripts` | Scripts de remarketing (criar, editar, A/B) |
| `/campaigns` | Campanhas em andamento |
| `/diagnostic` | **Saúde do bot em tempo real** + custo IA + buffer |
| `/liga/id-review` | Leads aguardando revisão manual de ID |
| `/automation` | Funis automáticos (só ligar se `AUTO_RESPOND_FUNNEL=1`) |

---

## 🔍 Quando algo der errado

### Bot não responde DMs
1. `sudo systemctl is-active liga-bot.service` (deve ser `active`)
2. `curl -s http://localhost:8080/health` (deve ser `status: ok, telethon: connected`)
3. Se não → `sudo systemctl restart liga-bot.service`

### Lista de leads não atualiza no painel
1. Ctrl+Shift+R no browser (cache)
2. Confere filtros no topo da página `/leads`
3. Roda: `sqlite3 .../data.db "SELECT MAX(created_at) FROM lead_messages WHERE direction='in'"` — se for recente, bot tá vivo

### Vision não extrai ID de print
1. Confirma que `lead.lifecycle != 'vip'` e `in_private_group=0` (esses são pulados)
2. Roda manual: cola URL `/api/lead/{id}/context` no browser pra ver estado
3. Logs: `journalctl ... | grep -iE "vision|account|sonnet"`

### Custo IA crescendo descontroladamente
1. Abre `/diagnostic` → veja `Custo IA (24h)` e top operations
2. Provavelmente `AUTO_DM_SCAN=1` foi ligado — confere no `.env` e desliga
3. Restart: `sudo systemctl restart liga-bot.service`

---

## 🏗 Estrutura do projeto

```
/opt/telegram-bot-remarketing/
├── main.py                    ← entrypoint do bot
├── data.db                    ← banco SQLite (TUDO está aqui)
├── data/
│   ├── backups/               ← backups automáticos diários
│   ├── learned_intents.json   ← frases aprendidas pelo classifier
│   └── banned_intents.json    ← frases banidas
│
├── userbot/                   ← Telethon (escuta DMs)
│   ├── client.py              ← conexão Telethon + reconnect loop
│   ├── tracker.py             ← handlers de eventos (DM nova, member join)
│   ├── leads.py               ← extração de ID das DMs
│   └── flood_wrap.py          ← retry em FloodWaitError
│
├── liga/                      ← lógica de negócio
│   ├── lifecycle.py           ← transições de estado do lead (FONTE DA VERDADE)
│   ├── lead_context.py        ← cérebro IA (build_lead_context)
│   ├── automation.py          ← jobs de remarketing
│   ├── scheduler.py           ← cron jobs (heartbeat, backup, etc)
│   ├── notifications.py       ← DMs pro admin
│   ├── funnel/                ← funil VIP automatizado (opt-in)
│   └── agent/                 ← sugestões IA pra fila
│
├── web/                       ← painel FastAPI
│   ├── app.py                 ← TODAS as rotas (5000+ linhas)
│   ├── templates/             ← HTML Jinja2
│   └── static/                ← CSS/JS
│
├── ai/
│   └── providers.py           ← Claude Haiku/Vision/Sonnet wrappers
│
├── db/
│   ├── models.py              ← schemas SQLAlchemy
│   └── database.py            ← migrations + init
│
└── deploy/
    └── update.sh              ← script de deploy
```

---

## 🧠 Princípios

1. **Bot é PASSIVO por default** — não responde leads sozinho.
2. **`Lead.lifecycle` é fonte da verdade** — TODO código novo deve usar (`mark_as_lead/deposited/vip`).
3. **Modo Teste Real** existe pra testar fluxo automático com 1 conta sem afetar leads reais.
4. **Custo IA monitorado em tempo real** — `/diagnostic` mostra sem delay.
5. **Backup automático diário 04h ART** — restore: descomprimir `.gz` e copiar pro lugar.

---

**Última atualização**: 2026-05-09
**Estado atual**: passivo, com Vision em real-time pra DMs novas com print Quotex
