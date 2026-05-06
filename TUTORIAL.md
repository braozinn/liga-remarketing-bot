# Tutorial - Bot de Remarketing Telegram

> **🚨 LEIA NA ORDEM. NÃO PULE NENHUMA PARTE.**

---

## Por que você teve aquele erro `No module named uvicorn`

Você apertou F5 antes de ter:
1. Movido a pasta pra um lugar definitivo
2. Criado o ambiente virtual (`.venv`)
3. Instalado as dependências dentro dele

O VS Code rodou o Python 3.14 do sistema, que não tem nenhuma biblioteca instalada. Por isso `uvicorn` não foi encontrado.

**Eu fiz um script Python que automatiza tudo isso.** Você roda UMA vez (`python setup.py`) e ele cria o `.venv`, instala tudo, configura o VS Code e cria o `.env`. Vai estar nos passos abaixo.

Outro detalhe: **Python 3.14 é muito novo.** Algumas bibliotecas podem não ter wheels prontas. **Recomendo Python 3.12.** O setup.py vai avisar você se detectar 3.14.

---

## ⚠️ ANTES DE COMEÇAR: mover a pasta

Você está executando o projeto direto da pasta de cache do Cowork:
```
C:\Users\PC\AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\...\outputs\telegram-bot-remarketing
```

**Essa pasta é temporária.** Mova/copie o projeto pra um lugar estável antes de qualquer coisa:

1. Abra o Windows Explorer
2. Vá até a pasta atual onde o projeto está
3. **Recorte** (Ctrl+X) ou **copie** (Ctrl+C) a pasta `telegram-bot-remarketing`
4. Cole em `C:\bot-remarketing\` (ou outro lugar fixo da sua escolha)

Daqui pra frente, **trabalhe sempre nesse caminho novo** (`C:\bot-remarketing\`).

---

## Parte 1 — Instalar Python 3.12 (10 min)

> Se você já tem Python 3.10, 3.11 ou 3.12 instalado, pula pra Parte 2. Se só tem 3.13/3.14, instale o 3.12 do lado.

1. https://www.python.org/downloads/release/python-3127/ (ou a 3.12 mais recente).
2. Baixa o **Windows installer (64-bit)**.
3. Abra o instalador. **MARQUE A CAIXA "Add python.exe to PATH"** (rodapé da primeira tela). É a CAUSA do problema que você teve antes.
4. → Install Now → espera → Close.
5. Reinicia o PC.
6. Verifique: abra o **cmd** (Win+R → `cmd` → Enter) → `python --version` → tem que aparecer `Python 3.12.X`.

> Se mesmo após reiniciar o `python --version` der "não reconhecido", desinstala o Python e reinstala marcando a caixa de PATH.

---

## Parte 2 — Instalar VS Code (5 min)

1. https://code.visualstudio.com/ → Download for Windows → instala normal.
2. Abra o VS Code.
3. Ícone de **Extensões** no lado esquerdo (ou Ctrl+Shift+X).
4. Pesquise **"Python"** e instala a extensão da Microsoft (com selo azul).

---

## Parte 3 — Pegar credenciais do Telegram (10 min)

### 3.1 API_ID e API_HASH
1. https://my.telegram.org → coloca seu telefone com DDI (`+5511999999999`) → cola o código que chega no app.
2. Clica em **API development tools**.
3. Preenche:
   - App title: `meu-bot`
   - Short name: `meubot`
   - Platform: Other
4. **Create application**.
5. **Anota num bloco de notas:**
   - **api_id** (número, ex: `12345678`)
   - **api_hash** (string longa, ex: `abc123def456...`)

### 3.2 Identifique seus 2 grupos
- **Grupo PRIVADO** = onde estão os já pagos / convertidos. **Esses NUNCA recebem remarketing.**
- **Grupo VIP / LEADS** = onde estão os prospects. **Daqui o bot pega quem contactar.**

Pega o `@username` ou ID negativo de cada um. Se for grupo privado sem @, use o ID (rode `@username_to_id_bot` e ele te diz).

### 3.3 (Opcional) Chave de IA — só se for usar modo AI
**Recomendado: Anthropic Claude Haiku 4.5**
- https://console.anthropic.com → cria conta → Settings → API Keys → Create Key.
- Adiciona $5-10 de crédito.

---

## Parte 4 — Setup automático (5 min)

> Esse é o passo que vai te poupar de erro. Roda 1 vez.

1. Abra o **VS Code**.
2. **File → Open Folder** → seleciona `C:\bot-remarketing\` (a pasta que você moveu).
3. Se aparecer "Do you trust the authors?" clica em **Yes, I trust the authors**.
4. Abre o terminal do VS Code: **Ctrl + ` ** (a tecla acima do Tab, ao lado do 1).
5. No terminal, digite:

```
python setup.py
```

> Se você tem Python 3.14 instalado E também 3.12, force o 3.12:
> ```
> py -3.12 setup.py
> ```

O script vai:
- ✅ Verificar a versão do Python (avisa se for 3.14)
- ✅ Criar o `.venv` (ambiente virtual)
- ✅ Instalar todas as dependências dentro dele
- ✅ Criar a pasta `.vscode` com `launch.json` correto
- ✅ Criar `.env` a partir do template
- ✅ Abrir o `.env` no Bloco de Notas pra você preencher

Demora ~3 minutos.

### 4.1 Preencha o .env
Quando o Bloco de Notas abrir:

```
TELEGRAM_API_ID=12345678                        ← seu api_id
TELEGRAM_API_HASH=abc123def456...               ← seu api_hash
TELEGRAM_PHONE=+5511999999999                   ← seu telefone com DDI

PRIVATE_GROUP=@meugrupopagos                    ← grupo dos PAGOS (excluídos)
LEADS_SOURCE_GROUP=@meugrupovip                 ← grupo VIP (fonte dos leads)

# Se quiser modo AI:
AI_PROVIDER=anthropic
ANTHROPIC_API_KEY=sk-ant-...

WEB_HOST=127.0.0.1
WEB_PORT=8080
WEB_PASSWORD=
```

**SALVE com Ctrl+S e FECHE o Bloco de Notas.**

---

## Parte 5 — Rodar o bot (1 min)

Agora sim, no VS Code:

**Aperte F5.**

> ⚠️ Se aparecer um seletor "Select a debug configuration" perguntando "Python File" / "FastAPI" / "Module", **escolha "Rodar Bot Remarketing"** (a config que o setup.py criou). Se ele NÃO aparecer, feche o VS Code, abra de novo a pasta e tenta F5 novamente.

No terminal do VS Code aparece:
```
>>> Digite o código que você recebeu no Telegram:
```

- Abra seu Telegram → vai chegar uma mensagem oficial do **Telegram (cor azul)** com código `12345`.
- Cola o código no terminal → Enter.
- Se você usa **2FA**, vai pedir a senha → digita → Enter.

Cria o arquivo `userbot.session` e nunca mais pede.

Aparece no log: `Painel web em http://127.0.0.1:8080`.

---

## Parte 6 — Acessar o painel

Abra qualquer navegador → **http://127.0.0.1:8080**

Pronto.

---

## Parte 7 — Como o bot trata os leads (importante!)

Tem 7 status possíveis. **Quem está no grupo privado NUNCA recebe nada.**

| Status | Recebe remarketing? | O que significa |
|---|---|---|
| `excluded` | ❌ NÃO | Está no grupo PRIVADO (já pagou) |
| `converted` | ❌ NÃO | Entrou no grupo privado depois de receber |
| `blocked` | ❌ NÃO | Pediu pra parar / bloqueou conta |
| `pending` | ✅ SIM | Não recebeu nada ainda — alvo principal |
| `contacted` | ✅ (se quiser) | Já recebeu, sem resposta |
| `replied` | ✅ (se quiser) | Respondeu neutro |
| `positive` | ✅ (se quiser) | Mostrou interesse |

**Camadas de proteção contra enviar pra quem está no privado:**

1. **No `Sync do grupo VIP`**: marca quem está no privado como `EXCLUDED`.
2. **No início de cada campanha**: o bot re-puxa os membros do grupo privado e atualiza o banco.
3. **Antes de cada envio individual**: re-confere que o lead não está no privado.

São 3 camadas. Só seria possível enviar pra alguém do privado se a pessoa entrasse no grupo privado entre as 3 verificações (questão de segundos), o que é quase impossível.

---

## Parte 8 — Primeira campanha (modo Forward = encaminhar mensagens)

### 8.1 Importar leads
- Painel → **Leads** → **"Sync do grupo VIP"**.
- Aguarda 1-3 minutos.
- O bot pega todos os membros do grupo VIP, cruza com o privado, e marca cada um.

### 8.2 Preparar mensagens fonte
1. Abra **"Mensagens Salvas"** no seu Telegram.
2. Mande pra lá tudo que quer encaminhar:
   - Print de feedback de aluno
   - Vídeo de testemunho
   - Texto chamando pro grupo
3. Em cada mensagem: clique direito → **Copiar link da mensagem** (formato `https://t.me/c/123.../42`).

### 8.3 Criar o script
1. Painel → **Scripts** → **Novo script**.
2. Modo: **🔄 Forward**
3. Nome: `Remarketing 03/05`
4. Objetivo: `levar pro grupo pago`
5. Criar.
6. Na tela do script, no formulário "Adicionar mensagem fonte", cole cada link.

### 8.4 Criar campanha
1. Painel → **Campanhas** → **Nova campanha**.
2. Escolhe o script.
3. Status alvo: `pending`.
4. Máx leads: `100` (pra começar pequeno).
5. **Executar agora** marcado.
6. Criar.

O bot encaminha respeitando os delays. Acompanha em tempo real abrindo a campanha.

---

## Parte 9 — Modo AI (texto editável em espanhol)

### 9.1 Criar script AI
**Scripts** → **Novo script** → Modo: **🤖 AI** → preenche:

- **Nome**: `Reativar leads aula gratuita`
- **Briefing pra IA** (em português, detalhado):
```
Lead que viu a aula gratuita "Domina el español de los negocios"
mas não comprou o curso completo.
Oferta: $97 por 48h (preço normal $197).
Inclui certificado, 12 módulos, comunidade VIP.
Quero abordar com prova social: vários alunos já são gerentes.
Tom: empático, sem urgência apelativa.
CTA: link do checkout (placeholder {link}).
```

### 9.2 Gerar variantes
Na tela do script:
- Quantas variantes (3 é bom)
- Tom, tamanho
- **Gerar com IA** → 5-15 segundos.

### 9.3 EDITAR pra corrigir vícios de linguagem
Cada variante tem o **texto editável**. Você pode trocar palavras que soam estranhas em espanhol latino, ajustar tom, encurtar, etc.

Clica **Salvar edição** em cada uma.

Pode também:
- Adicionar variante manual (escreve do zero)
- Pausar variante (não vai ser usada)
- Excluir variante

### 9.4 Rodar campanha AI
**Campanhas** → **Nova** → escolhe script AI → **Estratégia**:
- **Rotate** = A/B/C/A/B/C... (pra coletar dados)
- **Best** = sempre usa a de maior score (depois que tem dados)

### 9.5 Aprendizado contínuo
Após coletar dados:
- **Métricas** mostra ranking.
- Volta no script → **Regenerar a partir da vencedora** → IA cria novas variações inspiradas na melhor.

---

## Parte 10 — Volume e bom senso

Sua conta aguenta 1500-2000/dia. Defaults atuais:
- 120 envios/h
- Delay 15-40s entre cada
- Pausa 3min a cada 80 envios

Dá ~1500-2000/dia em 16h corridas.

**Comece com 200/dia no primeiro dia** pra confirmar que tá tudo OK. Sobe gradual.

Erros possíveis no terminal:
- `FloodWait Xs` → bot espera sozinho. Beleza.
- `PeerFloodError` → **PARE 24H.** Baixe `MAX_SENDS_PER_HOUR` pra 60 e reinicia.

---

## Parte 11 — Dia-a-dia

```
1. Abrir VS Code → F5
2. http://127.0.0.1:8080 no navegador
3. (de tempos em tempos) Leads → Sync do grupo VIP
4. (se modo Forward) Mensagens Salvas → manda mensagens novas
5. Scripts → cria/edita
6. Campanhas → Nova → executa
7. Métricas → vê ranking
8. Ctrl+C no terminal pra parar (ou só fecha VS Code)
```

---

## Solução de problemas

| Problema | Solução |
|---|---|
| `No module named uvicorn` (ou outro) | Você tá rodando Python do sistema, não a venv. Roda `python setup.py` e depois F5 |
| `python` não reconhecido | Reinstala Python 3.12 marcando "Add to PATH" |
| F5 abre seletor de config | Escolhe "Rodar Bot Remarketing". Se não aparecer, fecha VS Code, reabre a pasta |
| F5 roda uvicorn em vez de main.py | Apaga a pasta `.vscode` e roda `python setup.py` de novo |
| `PeerFloodError` | Para 24h. Baixa MAX_SENDS_PER_HOUR pra 60 |
| Sync trava em "Sincronizando..." | Tem muita gente. Espera. Não fecha. |
| start.bat bloqueado pelo Windows | **Não use start.bat.** Use `python setup.py` + F5 do VS Code |
| Erro instalando dependências | Se você tá no Python 3.14, instala 3.12 e roda `py -3.12 setup.py` |
| Lead recebeu mas não rastreou resposta | Bot precisa estar rodando quando o lead responde |
| Variante AI saiu com vício de linguagem | Edita direto na variante e salva |
| Quero re-fazer setup do zero | Apaga `.venv` e `.vscode`, roda `python setup.py` de novo |

---

## Boas práticas

- **NUNCA** contate quem nunca te mandou DM nem está no grupo VIP. Spam = ban.
- **Atualize leads** semanalmente (Sync do grupo VIP).
- **Crie 2-3 scripts diferentes** e compare métricas.
- Marque `BLOCKED` quem pedir pra parar.
- Olha o terminal de vez em quando — warnings importantes aparecem ali.

---

## Quem está no grupo PRIVADO nunca recebe nada — comprovado em 3 camadas

```
┌─────────────────────────────────────────────────────────────┐
│  Camada 1: Sync do grupo VIP                                │
│  ├─ Pega membros do grupo privado                           │
│  └─ Marca como EXCLUDED quem está lá                        │
├─────────────────────────────────────────────────────────────┤
│  Camada 2: Início de cada campanha                          │
│  ├─ Re-puxa membros do grupo privado AGORA                  │
│  ├─ Atualiza in_private_group de todo mundo                 │
│  └─ Filtra a fila excluindo quem está lá                    │
├─────────────────────────────────────────────────────────────┤
│  Camada 3: Antes de CADA envio individual                   │
│  ├─ Re-consulta o lead no banco                             │
│  ├─ Se in_private_group=True, status=EXCLUDED, CONVERTED    │
│  │   ou BLOCKED → SKIP                                      │
│  └─ Loga "SKIP <nome>: ..." pra você ver no terminal        │
└─────────────────────────────────────────────────────────────┘
```

Mesmo que alguém entre no grupo privado depois que a campanha começou, o lead vai ser SKIPPED no momento do envio. **Nunca vai receber.**
