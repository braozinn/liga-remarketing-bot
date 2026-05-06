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

## 🛡️ Compliance / segurança / anti-spam

Coisas que protegem você de banir o userbot ou queimar a conta:

28. **Opt-out automático** — se o lead manda "stop", "no insistas", "para de mandar", "deixa", "no quiero más" → marca `BLOCKED` automaticamente e nunca mais entra em qualquer disparo (mesmo manual). Lista em ES + PT. Já existe parcialmente em `_NEGATIVE_KW` do `classify_reply_heuristic` mas não está conectada com o status do lead.

29. **Rate limit por lead** — máximo N mensagens/semana pro mesmo lead, mesmo se múltiplas campanhas tentarem. Anti-fadiga + anti-banimento. Implementar como check em `sender.py` antes do envio.

30. **Cooldown após bloqueio** — se o lead bloqueou o userbot, marcar e nunca mais tentar enviar (Telethon retorna erro específico — capturar e setar `lead.status = BLOCKED`).

31. **Account warming meter** — métrica visível no painel: "saúde do userbot" baseada em sends/dia, reply rate, taxa de erro. Se tudo cair de repente = sinal de shadow ban / penalização. Hoje você não tem como saber.

32. **Modo preview/teste** — flag pra "dry run" — gera scripts, mostra o que ENVIARIA pra cada lead, mas não envia nada. Útil pra validar uma campanha de 500 leads antes de soltar.

33. **Detecção de bot/fake** — se o lead nunca tem foto, nome estranho (só números, "user_xxx"), entrou ontem no Telegram, sem histórico = provável fake. Marcar e excluir do remarketing.

34. **Blacklist por palavras-chave** — lista de palavras-gatilho ("scam", "denuncia", "policia") que se aparecerem em DM, marcam o lead como problemático e congelam envios pro lead até revisão manual.

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

55. **Multi-admin com assignment** — vários operadores compartilhando o painel. Cada lead pode ter um "owner". Admin A vê só os dele.

56. **Tasks/TODO por lead** — você marca "ligar terça pro João", "esperar resposta da Maria até sábado". Notificação quando vencer.

57. **Saved filters / lead lists** — "minha lista de hot leads", "deposits prometidos esta semana", "leads colombianos waitlist". Salva combinações de filtros recorrentes.

58. **Tutorial interativo onboarding** — overlay que guia o admin novo nos primeiros 5 cliques. Reduz fricção quando você passar o bot pra outra pessoa operar.

59. **Recipe book interno** — página `/help` com fluxos comuns: "como criar campanha de reativação?", "como rodar checkpoint manualmente?", "o que fazer quando lead manda print da demo?".

60. **VIP potential detection** ⭐ — usar dados do `@QuotexPartnerBot` (deposits_sum, balance, turnover) pra flagar leads com **alto valor** automaticamente. Threshold configurável. Esses viram prioridade absoluta — no `/leads` aparecem com flag dourada, no daily digest do admin entram em destaque, e qualquer reactivação vai pra eles primeiro. Filtro novo "💎 VIP" em `/leads`.

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

## ✅ Próximos passos sugeridos (ordem)

Se for pra encarar, recomendo nessa ordem (pelo retorno):

1. **Backup automático** (item #7) — 1h de trabalho, salva o projeto inteiro de um disco corrompido.
2. **Opt-out automático** (item #28) — 1h, **previne ban** do userbot.  Crítico.
3. **Rate limit por lead** (item #29) — 2h, anti-fadiga + anti-banimento.
4. **Daily digest pro admin** (item #6) — 2h, te dá visibilidade sem abrir o painel.
5. **Follow-up automático por engagement_tag** (seção dedicada) — 4h, é onde o bot vira "agente" de verdade em vez de só registrador.
6. **Cache de análise de imagem** (item #44) — 2h, **economiza muito** em Claude Vision.
7. **Cost dashboard Anthropic** (item #41) — 2h, sem isso você não sabe quanto gasta.
8. **Re-validação periódica via partner bot** (item #8) — 1h, mantém os dados frescos.
9. **Notas livres + bulk-actions** (itens #9, #10) — 2h cada, ganho de produtividade.
10. **Hash de imagens anti-fraude** (item #2) — 2h, imuniza contra prints reciclados.
11. **Account warming meter** (item #31) — 2h, vê se o userbot tá com problema antes de banir.
12. **VIP potential detection** (NOVO) — 1h, leads com saldo/depósitos altos viram prioridade #1 do remarketing. Filtro + flag automático no painel.

Total: ~25h de codificação pra cobrir os 90% mais úteis.

---

## 📈 Quadro de prioridade

| Prioridade | Itens | Critério |
|---|---|---|
| 🔴 **Crítico** (faz já) | #7 backup, #28 opt-out, #29 rate-limit, #44 cache imagem, #41 cost dashboard, #60 VIP detection | Sem isso o bot pode quebrar / ser banido / sangrar dinheiro / perder leads quentes |
| 🟠 **Alto valor** (próximas semanas) | Follow-up auto, daily digest, re-validação, notes, bulk, hash anti-fraude | Multiplicador de produtividade |
| 🟡 **Médio prazo** | Timeline, sentiment, drip campaigns, multi-admin, sentinel bot | Sofisticação operacional |
| 🟢 **Nice-to-have** | Heatmap geográfico, NFT finalistas, PWA, recipe book, tutorial | Polimento
