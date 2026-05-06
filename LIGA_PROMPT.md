# Prompt para Claude Code — Liga Torneio

Cole este prompt diretamente no Claude Code (terminal, com `claude` no diretório do projeto).

---

## PROMPT

```
Você está trabalhando em um bot de remarketing para Telegram construído com Telethon + FastAPI + SQLAlchemy (SQLite). O projeto está em Python 3.10+.

Preciso que você adicione um módulo completo de gerenciamento de torneio ("Liga") sobre a estrutura existente, SEM quebrar nenhuma funcionalidade atual.

---

## ESTRUTURA ATUAL (leia antes de modificar)

- `db/models.py` — modelos: Lead, Script, ScriptVariant, ScriptSource, ScriptMedia, Campaign, Send, Setting
- `db/database.py` — SQLite via SQLAlchemy. Tem sistema de migração leve via ALTER TABLE na função `init_db()`
- `userbot/tracker.py` — escuta DMs e entradas no grupo privado via Telethon events
- `userbot/client.py` — singleton do TelegramClient com suporte a 2FA
- `ai/providers.py` — abstração Anthropic/OpenAI. Função principal: `generate_completion(system, user, ...)`
- `ai/script_generator.py` — geração de scripts em espanhol com IA
- `utils/telegram_links.py` — parser de links + classificador heurístico `classify_reply_heuristic()`
- `config.yaml` — pesos de score + palavras-chave positivas/conversão
- `requirements.txt` — já tem: telethon, sqlalchemy, apscheduler, anthropic, openai, fastapi, pyyaml

---

## O QUE CONSTRUIR — 4 MÓDULOS EM ORDEM DE PRIORIDADE

---

### MÓDULO 1 — Máquina de estados da Liga (FUNDAÇÃO)

**1.1 Novos campos no modelo `Lead` (arquivo: `db/models.py`)**

Adicione estes campos à classe `Lead` existente:

```python
# Liga — estado da jornada
liga_state        = Column(String(30), default="new", index=True)
liga_account_id   = Column(String(100))           # ID da conta na plataforma
liga_balance      = Column(Float, default=0.0)     # saldo/banca atual
proof_sent_today  = Column(Boolean, default=False) # reset todo dia
lead_score        = Column(Integer, default=0)     # score de prioridade
last_bot_action   = Column(String(100))            # última ação do bot
conversation_ctx  = Column(Text)                   # JSON: últimas 5 msgs
streak_days       = Column(Integer, default=0)     # dias consecutivos com prova
```

Adicione um novo Enum:

```python
class LigaState(str, PyEnum):
    NEW              = "new"
    ONBOARDING       = "onboarding"
    WAITING_ID       = "waiting_id"
    WAITING_DEPOSIT  = "waiting_deposit"
    WAITLIST         = "waitlist"        # depósito < $100
    ACTIVE           = "active"          # depósito >= $100, enviando provas
    AT_RISK          = "at_risk"         # abaixo de $100 no checkpoint
    ELIMINATED       = "eliminated"      # falhou checkpoint
    FINALIST         = "finalist"        # top 3 na validação final
```

**1.2 Nova tabela `OperationProof` (arquivo: `db/models.py`)**

```python
class OperationProof(Base):
    __tablename__ = "operation_proofs"

    id              = Column(Integer, primary_key=True)
    lead_id         = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    proof_date      = Column(String(10), nullable=False)   # "YYYY-MM-DD"
    volume_usd      = Column(Float, default=0.0)
    account_id_raw  = Column(String(100))                  # extraído da imagem
    platform        = Column(String(100))
    confidence      = Column(String(10))                   # alta | media | baixa
    image_path      = Column(String(500))
    validated       = Column(Boolean, default=False)
    raw_ai_response = Column(Text)                         # JSON completo da IA
    created_at      = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead", backref="proofs")
```

**1.3 Nova tabela `DailyVolume` (arquivo: `db/models.py`)**

```python
class DailyVolume(Base):
    __tablename__ = "daily_volume"

    id         = Column(Integer, primary_key=True)
    lead_id    = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False, index=True)
    date       = Column(String(10), nullable=False)    # "YYYY-MM-DD"
    volume_usd = Column(Float, default=0.0)
    created_at = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead")
```

**1.4 Nova tabela `Objection` (arquivo: `db/models.py`)**

```python
class Objection(Base):
    __tablename__ = "objections"

    id         = Column(Integer, primary_key=True)
    lead_id    = Column(Integer, ForeignKey("leads.id", ondelete="CASCADE"), nullable=False)
    text       = Column(Text)
    category   = Column(String(100))   # "preco" | "tempo" | "desconfianca" | "outro"
    response_used = Column(Text)
    created_at = Column(DateTime, default=datetime.utcnow)

    lead = relationship("Lead")
```

**1.5 Migrações em `db/database.py`**

Adicione estas entradas na lista `migrations` dentro de `init_db()`:

```python
("leads", "liga_state",       "VARCHAR(30) DEFAULT 'new'"),
("leads", "liga_account_id",  "VARCHAR(100)"),
("leads", "liga_balance",     "REAL DEFAULT 0.0"),
("leads", "proof_sent_today", "BOOLEAN DEFAULT 0"),
("leads", "lead_score",       "INTEGER DEFAULT 0"),
("leads", "last_bot_action",  "VARCHAR(100)"),
("leads", "conversation_ctx", "TEXT"),
("leads", "streak_days",      "INTEGER DEFAULT 0"),
```

---

### MÓDULO 2 — Claude Vision para leitura de comprovantes

**2.1 Nova função em `ai/providers.py`**

Adicione a função `analyze_proof_image(image_bytes: bytes) -> dict` que:

1. Converte `image_bytes` para base64
2. Detecta o mime_type pela assinatura dos bytes (PNG = `\x89PNG`, JPG = `\xFF\xD8`)
3. Chama a API Anthropic com o modelo `claude-haiku-4-5-20251001` usando conteúdo multimodal (image + text)
4. Usa este system prompt exato:

```
Você é um validador de comprovantes de operações financeiras de trading.
Analise a imagem e extraia APENAS estes campos em JSON:
- data_operacao: data no formato YYYY-MM-DD (null se não visível)
- valor_usd: número decimal em USD/USDT (null se não visível)
- id_conta: identificador da conta ou usuário na plataforma (null se não visível)
- plataforma: nome da plataforma se visível (ou "desconhecida")
- confianca: "alta" se todos os campos foram extraídos com clareza, "media" se algum campo é incerto, "baixa" se a imagem não parece um comprovante de operação
- valido: true se a imagem parece um comprovante de trading, false caso contrário

Retorne SOMENTE JSON válido, sem texto antes ou depois.
```

5. Parseia o JSON retornado e retorna como dict Python
6. Em caso de erro, retorna `{"valido": False, "confianca": "baixa", "erro": str(e)}`

Formato da chamada à API Anthropic (messages com conteúdo multimodal):
```python
messages=[{
    "role": "user",
    "content": [
        {
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": mime_type,  # "image/png" ou "image/jpeg"
                "data": base64_data,
            }
        },
        {
            "type": "text",
            "text": "Analise este comprovante de operação de trading e retorne o JSON conforme instruído."
        }
    ]
}]
```

---

### MÓDULO 3 — Router de contexto em `userbot/tracker.py`

Refatore a função `_on_dm` para usar um padrão de roteamento por estado, mantendo toda a lógica existente de métricas de scripts (replies_count, positive_count, conversions_count).

**3.1 Crie um novo arquivo `userbot/liga_handlers.py`** com estas funções async, cada uma recebendo `(event, lead, session, client)`:

- `handle_waiting_id(event, lead, session, client)`:
  - Captura o texto enviado como `liga_account_id`
  - Atualiza `liga_state` para `"waiting_deposit"`
  - Responde: "✅ ID *[id]* registrado! Agora faça um depósito de no mínimo *$100* na sua conta e me envie o comprovante aqui."
  - Atualiza `last_bot_action` = "asked_deposit"

- `handle_waiting_deposit(event, lead, session, client)`:
  - Se a mensagem contiver uma foto/imagem:
    - Baixa os bytes da imagem com `await event.message.download_media(bytes)`
    - Chama `analyze_proof_image(image_bytes)` de `ai/providers.py`
    - Se `valido=True` e `valor_usd >= 100`:
      - Atualiza `liga_balance = valor_usd`
      - Atualiza `liga_state = "active"`
      - Cria registro em `OperationProof`
      - Responde com mensagem de boas-vindas ativa: "🏆 Perfeito! Banca de *$[valor]* confirmada. Você está oficialmente na Liga! A janela de envio é das 00h01 às 23h59 (horário de Buenos Aires). Me manda seu comprovante de operações todo dia."
    - Se `valor_usd < 100`:
      - Atualiza `liga_balance = valor_usd`
      - Atualiza `liga_state = "waitlist"`
      - Responde: "Recebi seu depósito de *$[valor]*. Para participar da Liga você precisa de no mínimo *$100*. Faltam *$[diferença]*. Assim que completar, me manda o comprovante!"
    - Se `confianca = "baixa"` ou `valido=False`:
      - Responde pedindo reenvio com o guia: "Não consegui identificar os dados do comprovante. Garanta que o print mostra: ① A data ② O valor ③ O ID da sua conta. Tenta de novo! 📸"
  - Se não for imagem:
    - Responde: "Preciso do *print do comprovante* de depósito 📸 Me manda a imagem diretamente aqui."

- `handle_active_waiting_proof(event, lead, session, client)`:
  - Se for imagem:
    - Baixa os bytes e chama `analyze_proof_image()`
    - Verifica se a data do comprovante é de hoje (fuso Buenos Aires = UTC-3)
    - Se válido:
      - Cria `OperationProof` com os dados
      - Atualiza/cria `DailyVolume` para hoje
      - Marca `proof_sent_today = True`
      - Incrementa `streak_days`
      - Atualiza `lead_score` chamando `calc_lead_score(lead)`
      - Responde com template: "✅ Dia *[N]* confirmado! Volume hoje: *$[valor]* | Acumulado: *$[total]* | Sequência: *[streak] dias* 🔥\n\n[PERGUNTA DE RELACIONAMENTO ROTATIVA baseada no dia da semana]"
      - As perguntas rotativas (1 por dia da semana): Segunda: "Qual par você mais operou hoje?" | Terça: "Quantas operações você fez hoje?" | Quarta: "Você usa algum indicador como base?" | Quinta: "Qual foi seu maior desafio hoje?" | Sexta: "Qual a sua meta de volume para semana que vem?" | Sábado: "Você opera no fim de semana?" | Domingo: "Como foi sua semana no geral?"
    - Se confiança baixa: pede reenvio
    - Se data errada: "Este comprovante parece ser de [data extraída]. A janela de hoje (horário Buenos Aires) ainda não foi enviada. Me manda o de hoje! 📅"
  - Se for texto e `proof_sent_today = True`:
    - Responde normalmente como conversa (usa classify_reply_heuristic existente)
  - Se for texto e `proof_sent_today = False`:
    - Responde: "Aguardando seu comprovante de hoje 📸 Me manda o print quando fizer suas operações!"

- `handle_waitlist(event, lead, session, client)`:
  - Se for imagem: chama `analyze_proof_image()` e verifica se agora atingiu $100
  - Se sim: transiciona para ACTIVE
  - Se não: responde com saldo atual e quanto falta

- `handle_unknown(event, lead, session, client)`:
  - Usa `classify_reply_heuristic()` existente para classificar
  - Mantém a lógica atual de `_bump_metrics()` intacta
  - Responde com mensagem genérica se não entender

**3.2 Refatore `_on_dm` em `tracker.py`** para:

```python
LIGA_HANDLERS = {
    "waiting_id":       handle_waiting_id,
    "waiting_deposit":  handle_waiting_deposit,
    "active":           handle_active_waiting_proof,
    "waitlist":         handle_waitlist,
}

# No início de _on_dm, ANTES da lógica existente de métricas:
liga_state = getattr(lead, "liga_state", "new")
if liga_state in LIGA_HANDLERS:
    await LIGA_HANDLERS[liga_state](event, lead, session, client)
    # Ainda executa a lógica de métricas existente depois
```

IMPORTANTE: não remova nenhuma linha da lógica existente de `_bump_metrics`, `lead.status`, `session.commit()`.

---

### MÓDULO 4 — Lead Scoring + Tarefas Agendadas

**4.1 Novo arquivo `liga/scoring.py`**

Crie a função `calc_lead_score(lead, session) -> int`:

```python
def calc_lead_score(lead, session) -> int:
    score = 0

    # Velocidade de resposta (usa last_dm_at e created_at)
    if lead.last_dm_at and lead.created_at:
        horas = (lead.last_dm_at - lead.created_at).total_seconds() / 3600
        if horas < 2:   score += 25
        elif horas < 24: score += 10

    # Depósito
    bal = lead.liga_balance or 0.0
    if bal >= 100:   score += 100
    elif bal > 0:    score += 50

    # Status de conversão existente
    if lead.status == "converted": score += 30
    if lead.status == "positive":  score += 15

    # Engajamento no grupo
    if lead.in_leads_group:   score += 20
    if lead.in_private_group: score += 40

    # Sequência ativa
    streak = lead.streak_days or 0
    if streak >= 7:  score += 30
    elif streak >= 3: score += 15

    # Volume acumulado
    total_vol = session.query(func.sum(DailyVolume.volume_usd))\
        .filter(DailyVolume.lead_id == lead.id).scalar() or 0.0
    score += int(total_vol / 100) * 5   # +5 pts por cada $100 movimentados

    return min(score, 200)  # cap em 200
```

Crie também `get_lead_tier(score: int) -> str`:
- 90+: "vip"
- 60–89: "hot"
- 30–59: "warm"
- 0–29: "cold"

**4.2 Novo arquivo `liga/scheduler.py`**

Use o `APScheduler` (já no requirements.txt) para agendar estas tarefas. Use timezone `America/Argentina/Buenos_Aires` em todas:

- **Lembrete diário — 21h00 todos os dias:**
  - Busca todos os leads com `liga_state = "active"` e `proof_sent_today = False`
  - Para cada um, envia DM: "📸 Ei [nome]! Ainda não recebi seu comprovante de hoje. A janela fecha à meia-noite (horário Buenos Aires). Me manda quando puder!"
  - Loga quantos lembretes foram enviados

- **Reset diário — 00h01 todos os dias:**
  - Atualiza `proof_sent_today = False` para todos os leads ativos

- **Checkpoint #1 — Domingo 17/05/2025 às 08h00:**
- **Checkpoint #2 — Domingo 24/05/2025 às 08h00:**
- **Checkpoint #3 — Domingo 31/05/2025 às 08h00:**
- **Corte Final — Quarta 11/06/2025 às 23h55:**

  Cada checkpoint executa `run_checkpoint(checkpoint_num)`:
  - Busca leads com `liga_state in ("active", "at_risk")` e `liga_balance < 100`
  - Para o corte final: muda `liga_state = "eliminated"`
  - Para checkpoints 1–3: muda para `"at_risk"` e envia: "⚠️ Atenção [nome]! Sua banca está em *$[valor]*, abaixo dos $100 mínimos. Você tem até o próximo checkpoint para recuperar. Caso contrário será removido do ranking."
  - Para quem JÁ estava `at_risk` no checkpoint anterior: muda para `"eliminated"` e envia: "Infelizmente você foi removido do ranking da Liga por estar abaixo de $100 no checkpoint de hoje. Obrigado por participar!"

- **Relatório semanal — Segunda-feira 09h00:**
  - Calcula: total de ativos, volume total acumulado, top 5 por volume, quantos estão em risco
  - Formata e envia para o número/conta configurado em `ADMIN_TELEGRAM_ID` no `.env`

- **Ranking diário — 22h00 todos os dias:**
  - Busca top 10 por volume acumulado total (`DailyVolume`)
  - Formata ranking com emoji de posição
  - Envia para o grupo da liga configurado em `LIGA_GROUP` no `.env`
  - Inclui barra de progresso da meta $1M: `▓░` proporcional ao total acumulado

**4.3 Inicialização do scheduler**

Crie `liga/scheduler.py` com uma função `start_liga_scheduler(client)` que recebe o TelegramClient e inicia o APScheduler com `AsyncIOScheduler`. Esta função deve ser chamada no ponto de inicialização principal do bot (onde `start_reply_listener()` é chamado).

---

## REGRAS OBRIGATÓRIAS

1. **Não quebre nada existente.** O sistema de remarketing (campanhas, scripts, sends, métricas) deve continuar funcionando identicamente.

2. **Migrações via `init_db()`.** Não use Alembic. Apenas adicione entradas na lista `migrations` de `database.py`.

3. **Timezone sempre Buenos Aires.** Use `pytz.timezone("America/Argentina/Buenos_Aires")` ou `zoneinfo.ZoneInfo("America/Argentina/Buenos_Aires")` para calcular datas.

4. **Fallback se Claude Vision falhar.** Se a API não responder ou der erro, solicita reenvio ao usuário. Nunca trave o bot.

5. **Logs descritivos.** Use `logger.info()` para cada ação da Liga: validação de prova, mudança de estado, envio de lembrete, score calculado.

6. **Variáveis de ambiente novas** a adicionar no `.env.example`:
   - `LIGA_GROUP` — @username ou ID do grupo da liga
   - `ADMIN_TELEGRAM_ID` — seu Telegram ID para receber alertas VIP e relatórios
   - `LIGA_START_DATE` — "2025-05-11" (início do torneio)
   - `LIGA_END_DATE` — "2025-06-11" (corte final)

7. **Arquivos novos a criar:**
   - `liga/__init__.py` (vazio)
   - `liga/scoring.py`
   - `liga/scheduler.py`
   - `userbot/liga_handlers.py`

8. **Só modifique estes arquivos existentes:**
   - `db/models.py` — adicionar campos e tabelas
   - `db/database.py` — adicionar migrações
   - `ai/providers.py` — adicionar `analyze_proof_image()`
   - `userbot/tracker.py` — adicionar router no início de `_on_dm`
   - `.env.example` — adicionar novas variáveis
   - `requirements.txt` — adicionar `pytz` se necessário

---

## ORDEM DE EXECUÇÃO

Implemente nesta ordem exata:
1. `db/models.py` — novos campos + tabelas
2. `db/database.py` — migrações
3. `ai/providers.py` — função de visão
4. `userbot/liga_handlers.py` — todos os handlers
5. `userbot/tracker.py` — router
6. `liga/scoring.py` — lead score
7. `liga/scheduler.py` — tarefas agendadas
8. `.env.example` — novas variáveis

Após implementar cada módulo, verifique se importa sem erros com `python -c "from [módulo] import *"` antes de avançar para o próximo.

Ao final, rode `python -c "from db import init_db; init_db(); print('DB OK')"` para confirmar que as migrações funcionaram.
```

---

## Como usar

1. Abra o terminal na pasta do projeto
2. Ative o ambiente virtual: `.venv\Scripts\activate` (Windows) ou `source .venv/bin/activate` (Linux/Mac)
3. Execute: `claude` para abrir o Claude Code
4. Cole o conteúdo entre os blocos de código acima
5. Aguarde a implementação completa

## Depois da implementação

Adicione no seu `.env`:
```
LIGA_GROUP=@seu_grupo_da_liga
ADMIN_TELEGRAM_ID=seu_id_aqui
LIGA_START_DATE=2025-05-11
LIGA_END_DATE=2025-06-11
```
