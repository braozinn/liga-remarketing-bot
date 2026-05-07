# Roadmap — Bot Liga · Remarketing

Documento vivo. Reúne ideias, dívidas técnicas e — o mais importante — **operações que hoje rodam só com clique e deveriam ser automatizadas**.

Atualizado em: 2026-05-06.

---

## 🤖 Auditoria de automação — operações manuais que deveriam virar cron

Hoje o painel tem 4 botões que disparam tarefas pesadas. Cada um deveria ter equivalente automático rodando em background:

| Endpoint manual | O que faz | Recomendação |
|---|---|---|
| `POST /api/leads/sync` | Varre DMs + grupo, importa novos leads, extrai IDs, valida no `@QuotexPartnerBot` | **Cron diário 06h00 BA** — pega leads novos da madrugada antes de você acordar |
| `POST /leads/recategorize` | Reclassifica `engagement_tag` de todos | **Cron diário 06h30 BA** (sem scan de mensagens, só usa DB — leve) <br> + **Cron semanal segunda 07h00 BA** com `scan_messages=True` (varre DMs procurando promessas de depósito) |
| `POST /liga/recalc-scores` | Recalcula `lead_score` de todos | **Cron diário 02h00 BA** (depois do reset diário da Liga) |
| `POST /liga/run/{job}` | Dispara lembrete/ranking/etc na hora | Mantém manual (já tem cron oficial) — mas o botão é útil pra teste |

**Não automatizado nada** ainda:

- **Re-validação periódica de IDs no `@QuotexPartnerBot`**: leads já validados não são re-checados. Saldo muda. Recomendo cron **semanal segunda 03h00 BA** rodando em todos com `liga_id_status="validated"` (re-roda `validate_id_via_partner_bot` em batch com cap de 200/dia pra não estressar o partner bot).
- **Backup do banco**: zero. Recomendo cron **diário 01h00 BA** que zipa `data.db + media/proofs/` e envia pra `ADMIN_TELEGRAM_ID` no Saved Messages do próprio admin.
- **Health check**: nada monitora se o Telethon caiu. Cron a cada 5 min que faz `client.get_me()` — se falhar 3× seguidas, manda DM pra admin.
- **Cleanup de mídia órfã**: prints sem registro no `OperationProof` ficam acumulando em `media/proofs/`. Cron mensal que apaga arquivos > 90 dias sem referência.
- **Detecção de churn**: quem está em `engaged` mas não envia há 2 dias deveria virar `slipping` automaticamente — hoje só acontece via reset diário se a Liga estiver ativa. Pra leads gerais (fora da Liga), nenhum sinal.

---

## 📨 Follow-up automático por categoria de engajamento  ⭐ *alta prioridade*

Já temos as 5 tags (`first_contact_no_reply`, `account_no_deposit`, `remarketed_no_account`, `deposit_promised`, `deposited`). Faltam **disparos automáticos baseados na tag + tempo desde a última interação**.

| Tag | Gatilho | Ação |
|---|---|---|
| `deposit_promised` | há 2 dias sem depósito | DM auto: "ainda planejas depositar? bora resolver" |
| `account_no_deposit` | há 5 dias parado | DM auto: vídeo motivacional + script "primeiros pasos" |
| `first_contact_no_reply` | há 7 dias | Segundo contato com ângulo diferente (script B) |
| `remarketed_no_account` | há 14 dias | Mensagem de prova social + reativação |

**Modelo**: novo `Campaign` tipo `auto_follow_up` que tem:
- `target_engagement_tag` (qual tag dispara)
- `min_days_since_action` (cooldown)
- `script_id` (script a usar)
- `max_per_lead` (1 por padrão — não fica spammando)

Cron diário 10h00 BA que itera todos os auto_follow_ups e dispara pros leads que matcham. Throttle compartilhado com o sender principal.

---

## 💡 Features recomendadas (lista completa do brainstorm)

### Inteligência

1. **Resumo da conversa via Claude** (botão na página do lead) — pega últimas 30 DMs, gera 3 bullets do que aconteceu. Útil pra retomar leads frios.

2. **Hash perceptual de imagens** (anti-fraude) — se 2 leads diferentes mandam o mesmo print, flagga ambos. Mesma lógica do `id_mismatch` mas pra imagens. Lib sugerida: `imagehash` (pHash).

3. **Detecção de sequências/IDs suspeitos** — IDs sequenciais ou patterns muito perfeitos sinalizam contas falsas. Olhar no `@QuotexPartnerBot`: se "Registration Date" é hoje e "Deposits Sum: $0", flagga.

4. **Linha do tempo do lead** (timeline view) — em `/liga/lead/<id>`, lista cronológica de tudo: 1º contato → script enviado → reply → screenshot → ID validado → categorização. Já tem os dados, falta a UI.

5. **Predicted finalists** (Liga) — projeção de quem vai chegar no top com base no ritmo atual de volume. Linear regression simples por lead: `volume_acum / dias_decorridos × dias_restantes`.

### Operacional

6. **Daily digest pro admin** ⭐ — cron 08h00 BA manda DM pro `ADMIN_TELEGRAM_ID` com:
   ```
   📊 Bom dia
   • 12 novos leads
   • 3 prometeram depositar (revisar)
   • 2 IDs com mismatch
   • 5 prints na fila
   • Top do dia: @karina ($340)
   ```

7. **Backup automático** ⭐ — cron 01h00 BA gera zip e envia. Trivial, salva vidas.

8. **Re-validação periódica de IDs** ⭐ — cron semanal re-checa todos os validados. Atualiza saldo/turnover. Ajuda a flaggar quem virou churn.

9. **Notas livres por lead** — campo de texto na página do lead. Já tem coluna `notes`, falta UI.

10. **Bulk-actions em /leads** — checkbox + dropdown "ações em massa": mudar status, enviar script, marcar como blocked. Ganho de produtividade alto.

11. **Export CSV** — botão respeitando o filtro atual. 30 linhas de código.

12. **Tags livres customizadas** — admin pode taggar leads com labels arbitrárias ("VIP", "comprou_curso", etc). Tabela `LeadTag` n×n.

### Visibilidade

13. **Activity feed** — página `/activity` com feed cronológico do que o bot fez (sends, replies recebidos, mudanças de estado, validações). Útil pra debug.

14. **Health check page** — `/health` com status: Telethon conectado? API key Anthropic OK? Último cron de cada job? Quantos jobs falharam nas últimas 24h?

15. **Métricas de funil** — chart em `/metrics`: contacted → replied → ID extracted → validated → deposited. Por script/campanha.

16. **Push notifications via Telegram** — alertar admin em tempo real quando: novo mismatch, novo print pendente, lead VIP entrou no privado.

### Comunicação / scripts

17. **Templates com variáveis** — usar `{nombre}`, `{ID}`, `{country}`, `{deposito_promised}` nos scripts. Resolução em tempo de envio.

18. **Auto-reply rules** (intent routing) — se lead pergunta "como pago", auto-resposta com instruções. Pequena lib de intents heurísticos.

19. **A/B test winner auto-promotion** — se variante A tem reply_rate > 1.5× a variante B com >= 30 envios cada, marca A como `is_active=False` da B. Ou pelo menos sinaliza no painel.

20. **Best time to send por lead** — extrai do histórico que horário cada lead costuma responder, agenda envios nesse horário. Pode dobrar reply rate.

### Segurança / qualidade

21. **Rate limit no `@QuotexPartnerBot`** — hoje cap de 100/sync. Adicionar contador rolling 1h pra não tomar ban (ex: máximo 50/hora).

22. **Modo de leitura (read-only)** — flag `READ_ONLY=1` no `.env` que desabilita TODOS os envios. Útil pra testar sem disparar nada por engano.

23. **Confirmação dupla pra ações destrutivas** — corte final, eliminar lead, reset de streak. Hoje qualquer botão clica e executa.

24. **Audit log** — toda mudança de estado/categoria/score grava em tabela `AuditLog` com timestamp + user. Permite rollback e compliance.

### Integrações

25. **Webhook de conversão** — quando um lead entra no `PRIVATE_GROUP`, POST pra URL configurável (Make/Zapier/CRM próprio).

26. **Sync com Google Sheets** — top metrics + ranking diário → planilha compartilhada. 1 cron, lib `gspread`.

27. **Mobile / PWA** — `manifest.json` + service worker básico = instalável no celular. Ranking + revisão de prints na palma da mão.

---

## 🛡️ Anti-spam — descontinuado do roadmap

Decisão tomada: como o bot opera em **modo passivo** (zero auto-resposta, remarketing só com aprovação manual), as proteções anti-spam complexas perdem importância. Já temos o essencial implementado:

- ✅ Opt-out automático (palavras "stop/para/déjame")
- ✅ Rate limit por lead (3 sends/semana, configurável)
- ✅ Account warming meter (badge no navbar)

**Não vamos investir em**: detecção de fake, blacklist por palavras, cooldown elaborado, modo preview/teste de campanhas. Você decide cada envio à mão, esses checks ficaram redundantes.

---

## 📊 Analytics avançado

Hoje as métricas são por script. Outras dimensões que faltam:

35. **Cohort analysis** — leads importados na semana X convertem em quanto %, contactados pelo script Y dia Z viram quantos depósitos? Tabela cruzada `semana_import × dias_até_conversão`.

36. **Funnel por fonte** — comparar pipeline de leads de `dm_history` vs `leads_group` vs `private_group_member`. Pode ser que uma fonte converta 10× mais.

37. **Geographic heatmap** — agora que temos `liga_id_country` no `@QuotexPartnerBot`, gerar choropleth: qual país tem maior reply rate, qual tem maior depósito médio. Investir mais nos top.

38. **Time-to-conversion** — médiana de dias entre primeiro contato e entrar no grupo privado. Por script. Por país. Por categoria de engajamento.

39. **Script wear-out detection** — variantes que TINHAM bom desempenho mas decaíram nos últimos 30 dias. Sugere regenerar.

40. **Retention curve** — depois de quanto tempo um lead vira frio (último reply > X dias). Usado pra calibrar quando disparar reativação.

41. **Cost dashboard (Anthropic)** — registra cada chamada à API com `provider/model/tokens/$_estimate` numa tabela `AIUsage`. Página `/metrics/ai` mostrando quanto você gastou no mês.

---

## 🧠 Inteligência adicional

42. **Sentiment analysis nas respostas** — atualmente classificamos só `positive/negative/neutral/conversion` por keyword. Subir pra Claude classificar emoção (interessado / cético / confuso / hostil / curioso). Routing condicional por sentimento.

43. **Question detection** — flag automático: se o lead fez uma pergunta direta ("¿cuánto cuesta?", "¿cómo funciona?"), sinaliza pra resposta humana imediata em vez de auto-reply genérico.

44. **Cache de análise de imagem** — calcula `imagehash.phash()` da imagem. Se já analisamos uma com hash igual no DB, reusa o resultado. Evita pagar Claude Vision 2× pelo mesmo print.

45. **Detecção de tradutor / idioma anômalo** — se um lead que devia falar ES de repente manda algo que parece traduzido por máquina (hindi, russo via google), flagga. Pode ser fake/farm.

46. **Drip campaigns automáticas** — sequências pré-definidas de N scripts: dia 0 (primeiro contato), dia 3 (lembrete), dia 7 (prova social), dia 14 (última tentativa). Cada lead avança no funil baseado em status/engajamento. Hoje você dispara campanhas one-shot.

47. **Auto-personalização do opener** — Claude gera 1-2 frases personalizadas no início do script baseado em: nome, país, scripts anteriores que recebeu, status atual. Aumenta reply rate (pesquisas mostram +15-30%).

---

## 🛠️ Confiabilidade / operação

48. **Sentinel bot** — segunda conta Telegram (ou bot oficial via BotFather) que faz heartbeat ping no userbot a cada 5 min. Se não responder em 3 tentativas, manda alerta DM pro admin. Cobre o cenário "Telethon caiu silenciosamente às 3h da manhã".

49. **Distributed lock** — arquivo `.lock` ou Redis flag pra impedir 2 instâncias do bot rodarem ao mesmo tempo (envia mensagem 2× pro lead, dá race condition no DB). Crítico se você roda em servidor com auto-restart.

50. **DB vacuum periódico** — SQLite acumula páginas mortas. Cron mensal `VACUUM;` reduz tamanho do `data.db` em 30-60%.

51. **Migration test mode** — antes de aplicar nova migração, copia `data.db → data.test.db`, roda lá, verifica integridade, só aí aplica na real. Salva de migrações catastróficas.

52. **Graceful shutdown** — quando você dá Ctrl+C, espera os jobs em voo terminarem (envios em andamento, validações no partner bot) antes de sair. Hoje pode interromper no meio.

53. **Quick admin bot** (separado do userbot) — bot do BotFather com comandos rápidos: `/status`, `/leads pending`, `/digest now`, `/disable_sends`. Você gerencia do celular sem abrir painel.

54. **Plugin do Quotex/QXBroker** — se a plataforma tem API de afiliado, pular screenshots e validar tudo direto via API. Mais rápido, mais confiável, sem custo de Vision.

---

## 🔄 Workflow / colaboração

55. ~~**Multi-admin com assignment**~~ — descontinuado. Você opera sozinho.

56. **Tasks/TODO por lead** — você marca lembretes tipo "responder o João até quarta", "esperar depósito da Maria até sábado", "checar se o Pedro voltou". Notificação no daily digest quando vencer.

57. **Saved filters / lead lists** — "minha lista de hot leads", "deposits prometidos esta semana", "leads colombianos waitlist". Salva combinações de filtros recorrentes.

58. **Tutorial interativo onboarding** — overlay que guia o admin novo nos primeiros 5 cliques. Reduz fricção quando você passar o bot pra outra pessoa operar.

59. **Recipe book interno** — página `/help` com fluxos comuns: "como criar campanha de reativação?", "como rodar checkpoint manualmente?", "o que fazer quando lead manda print da demo?".

60. **VIP potential detection** ⭐ — usar dados do `@QuotexPartnerBot` (deposits_sum, balance, turnover) pra flagar leads com **alto valor** automaticamente. Threshold configurável. Esses viram prioridade absoluta — no `/leads` aparecem com flag dourada, no daily digest do admin entram em destaque, e qualquer reactivação vai pra eles primeiro. Filtro novo "💎 VIP" em `/leads`.

---

## 🧠 Análise contextual completa via Haiku  ⭐⭐ *crítico pra precisão*

**Problema**: hoje a categorização é por regex em mensagens isoladas. Funciona em 70-80% dos casos óbvios, mas falha em:
- Promessas condicionais ("tengo que ver con mi marido si puedo poner plata")
- Hesitação implícita ("ya intenté tres veces y no funcionó la tarjeta")
- Sarcasmo, ironia
- Mistura ES + PT
- **Áudios** (30-40% das DMs LatAm — bot ignora 100%)
- Contexto multi-turn ("não" significa coisas diferentes dependendo do que foi perguntado antes)

**Solução**: cron semanal lê o histórico completo de cada lead (últimas 200-500 msgs + transcrições de áudio via Whisper local) e manda pro Claude Haiku classificar em **modo sugestão** (não commit automático — só sugere).

61. **Tabela `LeadMessage`** — guarda toda DM (in/out, texto/áudio/imagem) com timestamp e telegram_msg_id. Tracker passa a popular automaticamente. Custa zero a mais.

62. **Whisper local pra áudio** ⭐⭐ — `faster-whisper` ou `whisper.cpp` rodando offline em CPU. Modelo `base` ou `small` em ES. ~3-5s por áudio. **Sem isso a análise fica cega em 1/3 dos leads.**

63. **Análise contextual semanal via Haiku** ⭐⭐ — cron domingo 04h BA:
    - Pega últimas 200 msgs de cada lead não-bloqueado
    - Inclui transcrições de áudio
    - Manda pro Haiku com prompt estruturado: "classifique status (frio/morno/quente/convertido/hostil), depositou? engajamento? razão? tag sugerida?"
    - Salva em colunas `engagement_tag_ai`, `engagement_reason`, `analysis_confidence`, `last_analyzed_at`
    - **NÃO sobrescreve `engagement_tag` automaticamente** — só sugere
    - Painel mostra tag IA + justificativa, com botões `[Aceitar]` `[Corrigir]`

64. **Card "Análise da conversa"** em `/liga/lead/<id>` — mostra:
    ```
    🧠 Análise da conversa (Claude Haiku · há 2 dias)
    Status sugerido: deposit_promised · Confiança: alta
    Razão: Lead disse 3× que ia depositar — última vez "esta semana
           mando", há 4 dias. Mostrou interesse mas tem hesitação.
    Sinais detectados:
    • Promessa de depósito condicional (esposo)
    • Engajamento médio-alto (responde rápido)
    • Áudio enviado ontem 23h (transcrito): "che, esta semana sin falta"
    [Aceitar tag] [Corrigir →] [Ver conversa completa]
    ```

65. **Cache do Anthropic + delta análise** — chamada na semana N reusa a maior parte dos tokens da semana N-1 (cache do Anthropic cobra ~10% pelo histórico repetido). Custo cai 80-90%. Re-análise só quando há ≥10 mensagens novas desde a última.

**Precisão esperada**: ~92-96% (vs 70-80% da regex atual).
**Custo realista**: ~$3-8/mês com cache, pra ~1000 leads × 1 análise/semana.
**Trabalho**: ~7h pra deixar tudo rodando (LeadMessage + Whisper + função de análise + cron + UI).

**Limitações honestas (intransponíveis)**:
- Secret chats / mensagens efêmeras: impossível, fica fora do scope
- Conversas com < 5 msgs: confiança baixa, vai pra fila manual (comportamento correto)
- Mensagens > 12 meses: Telegram às vezes corta pra contas comuns

---

## 🎯 Ideias novas — alinhadas com seu workflow real

Essas faltavam no roadmap antigo. Foram identificadas vendo seu padrão de uso (observação passiva + remarketing manual baseado em engajamento):

### Inteligência de leads (ajuda você decidir quem priorizar)

75. **Decay de engajamento** — alerta quando um lead "quente" não responde há X dias. Hoje a tag fica fixa. Quero ver "esse lead era `deposit_promised` há 5 dias e sumiu — hora de cobrar". No daily digest entra um bloco "leads esfriando".

76. **Sentiment trend over time** — Claude vê 3 análises seguidas e detecta direção: "esse lead vinha esquentando ('positivo'→'positivo'→'positivo')" ou "tá esfriando ('positivo'→'neutro'→'negativo')". Sinal forte pra priorizar reaproximação.

77. **Best time to send por lead** — extrai do `LeadMessage` o histórico de quando cada lead respondeu nos últimos 30 dias. Calcula a janela de melhor reply rate. No painel mostra "Karina costuma responder 18h-21h" → você dispara remarketing nesse horário, dobra o reply rate.

78. **Lead source attribution** — onde cada lead apareceu primeiro: DM history vs `LEADS_SOURCE_GROUP` vs `PRIVATE_GROUP_INVITE_LINK`. Já existe campo `Lead.source` parcialmente usado. Métrica: qual fonte converte mais.

79. **Profile change tracking** — log automático quando lead muda foto / username / nome (sinal de que mudou de conta ou tá começando do zero). Salva em `LeadProfileChange` table. Útil pra detectar "esse lead apagou tudo, deve estar evitando".

80. **Snapshot histórico de saldo** — tabela `BalanceSnapshot` (lead_id, valor, fonte: 'partner_bot' | 'screenshot', timestamp). Cada re-validação semanal vira um snapshot. Daí você vê GRÁFICO de evolução: "esse lead foi de $0 → $200 → $400 → $0 → $1500". Ouro.

81. **Dedup automático** — leads que parecem ser a mesma pessoa: nome igual + foto igual mas IDs Telegram diferentes (mudou de número/conta). Flag no painel "possível duplicata de @karina77".

### Visibilidade em tempo real (você acompanha sem refresh)

82. **Feed `/feed` em tempo real** ⭐ — página com stream cronológico das últimas 24h:
    ```
    14:32  📥 @karina77 mandou screenshot (Cuenta real $25)
    14:28  ✓ ID 87035300 validado VE pelo partner bot
    14:15  💎 @abraao virou VIP (deposits_sum: $520)
    14:02  🔥 @maria parou de responder há 3 dias
    13:48  ⚠ ID divergente: @nuevolead enviou ID diferente do registrado
    ```
    Server-sent events (SSE) ou polling de 10s. Páginas separadas por dia.

83. **Lembretes pra VOCÊ** (substituem os lembretes pro lead) — cron de 4 em 4h durante horário comercial BA: "tem 8 leads aguardando sua resposta há mais de 2h". DM pra você (ADMIN_TELEGRAM_ID).

84. **Detecção de pergunta direta** — quando lead manda msg com `?`, flag automático "🔴 esperando resposta há Xh". Lista no topo do painel /leads ou em `/feed`.

85. **Sino de notificação no painel** — ícone no canto direito do navbar com contador de "coisas que precisam sua atenção": novos prints na fila, mismatches, perguntas pendentes, VIPs novos. Click abre dropdown com lista.

### Workflow manual (acelera o que você faz à mão)

86. **Áudios "evergreen" pré-gravados** — você grava 10-15 áudios em ES rioplatense respondendo perguntas frequentes (preço, como funciona, depósito mínimo, dúvidas técnicas, motivacional). Bot tagga cada áudio e, quando o lead manda algo que match com tag, painel sugere "responder com áudio X" e você manda com 1 clique. **Não é resposta auto** — é assistente.

87. **Saved replies / templates rápidos** — mesma lógica mas pra texto: snippets com variáveis (`{nombre}`, `{ID}`, `{country}`) que você dispara com 1 clique do painel. Caixa de "respostas favoritas".

88. **Status "lido sem responder"** — bot marca msg como vista (✓✓ azul) automaticamente, mas NÃO responde. Lead vê que foi visto, não cria ansiedade. Você responde no seu tempo. Já tem `client.send_read_acknowledge()` no Telethon — só falta plugar.

89. **Sugestão de script por lead** — abrindo `/liga/lead/<id>` na sidebar aparece "Pra esse lead (engagement_tag = `deposit_promised`, há 4 dias sem responder), recomendamos enviar script Y" com botão "Enviar agora" que dispara um Send manual.

### Análise / inteligência adicional (post-Haiku)

90. **Resumo da conversa por lead** — botão "Resumir" em `/liga/lead/<id>` que pega últimas 50 msgs e o Claude gera 5 bullets do que aconteceu. Ajuda quando você abre um lead que já não fala há semanas. Cache 24h.

91. **Translation pra admin** (PT-BR) — você lê PT melhor que ES. Botão "🇧🇷 Traduzir" em cada conversa que mostra resposta do lead em PT-BR. Claude faz a tradução. Cache.

92. **Group memberships visibility** — pra cada lead, lista os outros grupos do Telegram em que vocês dois compartilham (Telethon expõe). Se ele tá em "Trading Argentina" e em "Crypto LatAm", isso é dado interessante.

93. **Engagement score numérico (0-100)** — combina sinais: tempo desde 1º contato, msgs trocadas, depósitos, streak, last_reply_at, etc. Score único pra ordenar todos os leads por "calor". Já tem `lead_score` mas só calcula features básicas.

94. **First-message-time analysis** — em qual horário cada lead te procurou pela 1ª vez? Padrão por país/perfil. Útil pra entender quando essa pessoa tá disponível.

### Operacional (proteção / reliability)

95. **Mass message preview** — antes de disparar campanha pra N leads, painel mostra preview do que vai sair pra cada um (com variáveis substituídas) + lista dos leads. Você confere e clica "Confirmar" pra enviar.

96. **Estimated reach** — em campaigns, antes de criar, calcula quantos leads atingem o filtro selecionado. "Essa campanha vai pra 247 leads". Sem surpresa.

97. **Audit log de mudanças** — toda mudança em `engagement_tag`, `liga_state`, `liga_id_status` grava em `AuditLog` (timestamp, lead_id, campo, antes, depois, fonte: 'auto'|'manual'|'cron'). Visível em `/liga/lead/<id>`. Permite rollback se algo for taggado errado.

98. **Tag history audit** — extensão do #97 só pras tags. "Esse lead foi `first_contact_no_reply` em 2026-04-10, virou `account_no_deposit` em 2026-04-15, virou `deposit_promised` em 2026-04-20". Línea do tempo da jornada categórica.

---

## 🎯 Modo "passivo" — bot só observa, você responde tudo

Decisão tomada na conversa: **não queremos respostas automáticas**, principalmente durante o torneio. O bot é assistente de catalogação, não agente conversacional. Toda DM passa por aprovação humana.

### Para entrar nesse modo, é preciso desligar:

| Onde | O que faz hoje | Ação |
|---|---|---|
| `userbot/liga_handlers.py` | Auto-replies em 5 estados (waiting_id, waiting_deposit, waitlist, active, mismatch, demo, low conf) | **Desligar via flag `AUTO_REPLY=0` no .env** |
| `liga/scheduler.py` `task_daily_reminder` 21h BA | DM "ainda não recebi seu comprovante" | **Desligar ou trocar por lembrete pro admin** |
| `liga/scheduler.py` checkpoint warnings | DM "você está em risco" / "você foi eliminado" | **Trocar por: muda estado no DB + flag visual no painel, sem DM** |
| `liga/automation.py` `task_run_follow_ups` | Dispara campanhas autom. por engagement_tag | **Trocar pra modo "sugestão" — bot prepara fila, você aprova manualmente** |

### O que MANTER (tudo que é leitura/catalogação invisível pro lead):

- Sync de leads (DM history + grupo)
- Extração de ID (texto + imagem)
- Validação no `@QuotexPartnerBot`
- Cache de imagem
- Anti-fraude por hash
- Categorização interna de engagement (read-only)
- VIP detection
- Backup automático
- Daily digest pro admin
- Re-validação semanal de IDs
- Account warming meter
- Cost dashboard
- Scan incremental 5min
- Análise contextual via Haiku (#61-65) — **só sugere, nunca commita**
- Ranking diário no LIGA_GROUP (público, opcional)

### Gaps importantes pra esse modo:

66. **Sem captura de áudio** — leads LatAm mandam 30-40% das DMs em áudio. Whisper local resolve (#62).

67. **Sem `LeadMessage` populado** — `conversation_ctx` no modelo existe mas nunca foi usado. Tracker precisa começar a salvar (#61).

68. **Sem snapshot histórico de saldo** — re-validação semanal sobrescreve `liga_id_balance`. Preciso de tabela `BalanceSnapshot` pra ver evolução. Detecta automaticamente "esse lead tinha $200 mês passado, hoje tem $0".

69. **Sem timeline cronológica unificada** no `/liga/lead/<id>` — info espalhada em 4 cards. Linha do tempo única (1º contato → script → reply → screenshot → ID validado → categorização → análise IA) seria muito mais útil.

70. **Sem feed de novidades em tempo real** — `/feed` mostrando "lead X mandou screenshot agora", "VIP Y validado". Pra você acompanhar sem dar refresh.

71. **Sem sugestão de áudio evergreen** — você grava 10-15 áudios FAQ. Bot detecta "lead pediu Y" e te sugere áudio Z pra responder com 1 clique. **Não é resposta auto** — é assistente de produtividade.

72. **Sem detecção de pergunta direta** — quando lead manda `?`, flag automático "🔴 esperando sua resposta há Xh" pra não esquecer ninguém.

73. **Sem lembretes pro ADMIN** (em vez de pro lead) — substitui o lembrete 21h BA: "tem 12 leads aguardando sua resposta há mais de 2h".

74. **Sem status "lido sem responder"** — bot marca msg como vista (2 checks azuis) mas não responde. Lead não fica preocupado, você responde no seu tempo.

**Pacote mínimo viável pra rodar tranquilo no torneio (~9h):**

1. Desligar auto-replies (1h) — flag `AUTO_REPLY=0`
2. Tabela `LeadMessage` + tracker grava DMs (1.5h)
3. Whisper local (2h)
4. Snapshot histórico de saldo (1h)
5. Feed `/feed` em tempo real (2h)
6. Lembretes pro admin (1.5h)

A partir daí: bot vira **observador silencioso**. Catalogação completa, zero risco de mandar mensagem errada, você decide cada interação.

---

## 🐛 Dívida técnica conhecida

- **`Lead.notes`** existe mas a UI nunca usa.
- **`LigaState.ONBOARDING`** definido no enum mas nenhum handler entra/sai dele.
- **`Lead.conversation_ctx`** definido mas nunca populado (era pra guardar últimas 5 msgs em JSON pra contexto da IA).
- **`Objection`** — tabela criada mas nada classifica/grava objeções. Era pra ter classificador de "preço/tempo/desconfiança" via Claude.
- **`engagement_tag` legacy values** — `engaged/slipping/eliminated` da Liga e `deposit_promised/account_no_deposit/...` do remarketing geral coexistem em UIs diferentes mas usam a mesma lógica conceitual. Talvez unificar.
- **`misfire_grace_time`** só está nos checkpoints. Outros jobs (lembrete/ranking) podem ser perdidos se o bot estiver offline na hora exata. Adicionar nos jobs cron também.
- **Sem retry no `validate_id_via_partner_bot`** — se a primeira chamada deu timeout, o lead vira `extracted` e nunca mais é re-validado automaticamente.
- **`/static/style.css`** carrega Bootstrap 5 + Bootstrap Icons + Inter + Fraunces + JetBrains Mono via CDN. ~600KB no primeiro load. Self-hosting reduziria pra ~120KB.
- **Sem testes automatizados** — qualquer mudança você precisa testar à mão. Pelo menos uns testes unitários do `categorizer`, `liga.scoring` e `_parse_partner_response` seriam úteis.

---

## 📌 Snapshot do estado atual (o que já está pronto e funciona)

**Backend**:
- Telethon userbot conectado, listener de DMs e ChatActions ✓
- DB SQLite + SQLAlchemy + migrações leves ✓
- Modelos: Lead (com 25+ campos), Script, ScriptVariant, Campaign, Send, OperationProof, DailyVolume, Objection, Setting ✓
- Claude API: scripts ES rioplatense, vision pra contas e operações ✓
- Liga: máquina de estados completa (8 estados), 8 jobs cron ativos ✓
- Validação de ID via `@QuotexPartnerBot` (resposta crua + parse de país/saldo/depósitos/turnover) ✓
- Categorização de engajamento em tempo real + bulk com progress bar ✓

**Frontend**:
- Painel web FastAPI + Jinja2 ✓
- Design system custom (paleta Anthropic/Claude, fontes Inter+Fraunces) ✓
- Light + dark mode com persistência ✓
- Dashboards alternáveis (Torneio / Pós-torneio) ✓
- Filas de revisão manual (prints + IDs) ✓

**Cron jobs ativos** (TZ Buenos Aires):
- 00:01 — reset diário Liga (proof_sent_today + streak)
- 21:00 — lembrete pra ativos sem prova
- 22:00 — ranking diário no `LIGA_GROUP`
- Segunda 09:00 — relatório semanal pro admin
- Datas específicas — checkpoints (3 + corte final)

---

## ✅ Próximos passos sugeridos — pra seu workflow real

**Workflow atual**: bot observa, cataloga e sugere — você responde tudo manualmente, dispara remarketing baseado em engagement_tag confiável.

**Já feito** (não vou listar, só pra contexto): backup automático, opt-out, rate limit, cache imagem, cost dashboard, daily digest, re-validação semanal, hash anti-fraude, VIP detection, account warming meter, scan incremental 5min, partner bot validation.

**Próximos por retorno** (ordem recomendada):

### 🔴 Tier 1 — fundamentos da observação inteligente (~12h total)

1. **Tabela `LeadMessage` + tracker grava DMs** (#61) — 1.5h.
   Sem isso o resto fica capenga. É a base de toda a análise contextual.

2. **Whisper local pra áudio** (#62) — 2h.
   Áudio é 30-40% das DMs. Sem isso você está cego pra essa fatia.

3. **Análise contextual via Haiku** (#63-65) — 4h.
   O salto de precisão que você precisa pra confiar nas tags durante o torneio (70-80% → 92-96%). Sugere, você confirma.

4. **Snapshot histórico de saldo** (#80) — 1.5h.
   Rastreia evolução: "esse lead foi de $0 → $200 → $400 → $0" = ouro pra decisão.

5. **Desligar auto-replies** (modo passivo) — 1h.
   Flag `AUTO_REPLY=0` no `.env`. Bot vira observador silencioso 100%.

6. **Decay de engajamento + alerta** (#75) — 2h.
   Detecta quando lead "quente" esfriou. Aparece no daily digest.

### 🟠 Tier 2 — visibilidade e produtividade no painel (~10h total)

7. **Feed `/feed` em tempo real** (#82) — 3h.
   Stream do que tá acontecendo agora. Você acompanha sem refresh.

8. **Sino de notificação + contador** (#85) — 1.5h.
   "5 coisas precisam sua atenção" sempre visível.

9. **Detecção de pergunta direta** (#84) — 1h.
   Flag "🔴 esperando resposta" pra não esquecer ninguém.

10. **Lembretes pra VOCÊ** (#83) — 1.5h.
    DM "8 leads aguardando há +2h" no seu Telegram, durante horário comercial.

11. **Notas livres por lead** (#9) — 1h.
    Já tem coluna `Lead.notes`, só falta UI. (Já feito? Conferir.)

12. **Bulk-actions em /leads** (#10) — 2h.
    Você triga ações em lote rápido.

### 🟡 Tier 3 — assistente de produtividade (~8h total)

13. **Áudios evergreen** (#86) — 4h (incluindo gravação dos áudios).
    Você responde perguntas comuns com 1 clique mandando áudio seu.

14. **Saved replies / templates rápidos** (#87) — 2h.
    Snippets de texto com variáveis pra você disparar manualmente.

15. **"Lido sem responder"** (#88) — 30min.
    Bot marca ✓✓ automático mas não responde. Reduz ansiedade do lead.

16. **Sugestão de script por lead** (#89) — 1.5h.
    Painel sugere o melhor script pra cada lead baseado na tag.

### 🟢 Tier 4 — análise e auditoria (~10h total)

17. **Best time to send por lead** (#77) — 2h.
    Aprende horário ideal, dobra reply rate.

18. **Resumo da conversa via Haiku** (#90) — 1.5h.
    Botão "Resumir" gera 5 bullets do histórico. Cache 24h.

19. **Sentiment trend** (#76) — 2h.
    Direção do engajamento: esquentando ou esfriando.

20. **Tradução PT-BR pra admin** (#91) — 1h.
    Botão 🇧🇷 traduz reply do lead pra você ler rápido.

21. **Audit log + tag history** (#97-98) — 2h.
    Rastreia toda mudança automática/manual. Permite rollback.

22. **Profile change tracking** (#79) — 1.5h.
    Detecta lead que mudou foto/nome/username.

---

**Total dos tiers 1+2 = ~22h** = pacote pra você operar **muito bem** durante o torneio: bot inteligente, observador, te alimenta com dados precisos e você decide tudo manualmente.

Tiers 3+4 são incrementais, fazem em paralelo nas semanas seguintes.

---

## 📈 Quadro de prioridade

| Prioridade | Itens | Critério |
|---|---|---|
| 🔴 **Tier 1 — fundamentos** | #61 LeadMessage, #62 Whisper, #63-65 Haiku contextual, #80 saldo histórico, #75 decay alert, modo passivo | Sem isso a categorização do torneio é frágil |
| 🟠 **Tier 2 — visibilidade** | #82 feed real-time, #85 sino, #84 pergunta direta, #83 lembretes pro admin, #9 notas, #10 bulk-actions | Você acompanha o jogo sem dar refresh |
| 🟡 **Tier 3 — produtividade manual** | #86 áudios evergreen, #87 saved replies, #88 lido sem responder, #89 sugestão de script | Você responde mais rápido sem perder qualidade |
| 🟢 **Tier 4 — análise profunda** | #77 best time, #90 resumo conversa, #76 sentiment trend, #91 tradução PT, #97-98 audit log | Sofisticação a longo prazo |
| ⚫ **Descontinuado** | Anti-spam complexo, multi-admin, follow-up automático, drip campaigns | Não bate com workflow passivo
