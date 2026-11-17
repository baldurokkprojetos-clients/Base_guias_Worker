# Plano de Correção — Guias válidas exibidas como "Sem Guias" (Unimed Goiânia)

**Data**: 11/11/2026 · **PO**: Diogo · **Dev Senior**: — · **QA Senior**: —
**Baseline analisada**: lote cron 10/11/2026 23:01 UTC (795 jobs) + HTML de listagem/detalhe do portal (anexado na task)

---

## 1. Contexto e evidências

Lote de 10/11: 438 válidas · 255 bloqueadas · **102 "sem guias"**. Classificação por log:

| Causa raiz | Qtd | % | Evidência |
|---|---|---|---|
| Corte de 270 dias sobre **data de autorização** | 91 | 89% | `"Found N rows... Guia date DD/MM/AAAA is older than limit. Stopping."` |
| Timeout na carga da tabela (portal) | 4 | 4% | `"Timeout waiting for results table"` — 3 surgiram em 09/11, 1 em 10/11; zero entre 27/10–08/11 |
| Tabela vazia de fato | 4 | 4% | `"Found 3 rows"` + `no such element` na linha 1 |
| Somente guias com status `Negado` | 3 | 3% | `"Row N com status 'Negado' ignorado."` |

**Caso emblemático (MARIA LUISA ALMEIDA CAMPOS, 0064.8000.053327.69-5, job 186574):**
listagem com 12 linhas `Autorizado`; detalhe da guia **66363404** (CD_GUIA interno 63342796):
autorização 09/10/2025, senha 953231282, **validade da senha 01/06/2027 (vigente)** → descartada
pelo corte de 270d → `tipo_json: null` → card **"Sem Guias"**. Pelo menos 8 pacientes do lote
estão nesta situação com guia vigente na base (ex.: HEITOR MENDES 3 guias vál. 2027-06;
SAMUEL NUNES 5 guias vál. até 2038; GAEL 1 vál. 2027-11).

**Fatores agravantes confirmados no HTML do portal:**
1. A **validade não existe na listagem** — só no detalhe (`#CampoValidadeSenha`). A listagem já
   traz: solicitação (td[1]), nrGuia (link td[4]), código/descrição (hidden `HTML_HINT_ITEM`),
   status (td[6]), **senha (td[7])**, solicitante (td[8]), grupo (td[9]).
2. O detalhe é um **GET simples** (`exames/emaberto/nova.do?CD_GUIA=...&dynaHash=<por linha>&...`),
   com todos os parâmetros disponíveis no `href` de cada linha da listagem.
3. A página do detalhe expõe ainda: `jsonConsistencia` (`hasItemAtivo`, `cdGuia`, `nrGuia`,
   `cdPrestPrincipal`), `jsonPrestadores` (CLMF = cm 39138), constantes `MSG_BLOQ_*`
   (bloqueios) e o endpoint AJAX `lista_procedimentos.do?CD_GUIA=...&dynaHash=...` (grid de
   terapias/qtdes).
4. **Cloudflare** ativo na sessão (`__CF$cv$params`) → `requests` puro pode receber challenge;
   o fetch deve rodar **dentro do navegador Selenium** (`execute_script` + `fetch` com
   `credentials:'include'`), herdando cookies e clearance.

---

## 2. Diagnóstico técnico (pontos de mudança)

| # | Local | Comportamento atual | Problema |
|---|---|---|---|
| D1 | `worker/Worker/ImportBaseGuias.py:619-641` (e 486-502 Em Estudo) | `cutoff = hoje − 270d` sobre a data da **1ª linha** (solicitação); para o loop | Derruba guias com autorização antiga e **validade vigente** |
| D2 | `ImportBaseGuias.py:550-553, 770-774` | Detalhe via **clique** + leitura DOM (`tr[3]/td[2]`, `tr[4]/td[4]`, `tr[5]/td[2]`, `#CampoValidadeSenha`) | 3s+/guia, frágil, e só ocorre para linhas já dentro da janela |
| D3 | `ImportBaseGuias.py:395-402` | Timeout da tabela → retorna `{"tipo_json": None}` | Portal fora → job `success` + card "Sem Guias" (sem retry) |
| D4 | `dispatcher.py:293-294, 511-512` | `status=success` sempre que o worker responde `success` | Consolida D3; retry automático (`attempts<3`) nunca dispara |
| D5 | `backend/routes/jobs.py:100-107` + `frontend/src/pages/Importacoes.jsx:1046-1073` | `tipo_json ∈ {All Sucess, Thered, null}` | "Sem Guias" é rótulo-guarda-chuva (vazio real, antigas, vencidas, negadas, timeout) |
| D6 | Fluxo prefixo ≠ 0064 (`ImportBaseGuias.py:386-390`) | Clique extra `Button_Consulta` | 3 dos 4 timeouts são prefixo `0000` — suspeita de mudança no portal em 09/11 |

---

## 3. Regra de negócio acordada (decisão de produto)

> **R1 (rev. 11/11 — decisão do PO)** — Uma guia é **elegível** para `base_guias`/PEI quando
> satisfaz **os dois marcos simultaneamente**:
> **(a) autorização ≤ 270 dias** *(crivo mantido — a Unimed pode bloquear o PEI mesmo com
> guia dentro da validade)* **E (b) validade da senha ≥ hoje**.
> Guias que falham em qualquer marco **não entram nem renovam `base_guias`** — são apenas
> **exibidas** no frame do job com o motivo (proteção do PEI/trigger
> `calculate_patient_pei`, que filtra por `Vinculo_prestador LIKE 'Guia V%lida'` sem checar
> validade).
>
> **R2** — O card passa a distinguir: **Válidas** · **Bloqueadas** · **Vencidas** (dentro da
> janela 270d, validade < hoje) · **Antigas (270d)** (Autorizado/Em Estudo com autorização
> > 270d — ainda que vigentes) · **Negadas** · **Sem Guias** (vazio real) · **Timeout**
> (visível só em jobs `error`).
>
> **R3** — Falha de carga do portal (timeout popup/tabela) é **erro retryável**, nunca
> "Sem Guias".

Consequências da regra dupla:
- **Guia fora da janela 270d não exige fetch do detalhe** (já é inelegível pelo marco (a)) —
  a ordenação DESC por solicitação permite **manter a parada na 1ª linha antiga**, como hoje.
  O detalhe (T1) é buscado **somente para linhas dentro da janela**, cuja validade (marco
  (b)) precisa ser checada. Varredura continua limitada pela própria janela — não há
  varredura infinita nem necessidade de `MAX_LINHAS`.
- Caso MARIA LUISA sob a regra final: guias 663634xx (autorização 09/10/2025 > 270d,
  validade 01/06/2027) **não entram na base**; o card sai de "Sem Guias" e passa a exibir
  **"Antigas (270d)"** — o atendente vê que existe guia, mas fora da janela PEI (ação:
  renovar guia junto à Unimed).

---

## 4. Backlog — Dev Senior

> Padrões: módulos novos isolados, funções puras de parse (testáveis sem Selenium), sem
> duplicação de seletores (fonte única de verdade), logs estruturados no padrão `self.log()`
> existente. Estimativas em pontos relativos (S/M/L).

### T1 — Módulo `guia_detail`: detalhe da guia via fetch no contexto do browser (S/M)
**Como** o scraper, **quero** obter validade/bloqueios de guias **dentro da janela 270d** por
HTTP na própria sessão Selenium, **para** aplicar o marco (b) da R1 sem custo/fragilidade de
clique. *(Reescopo: com o crivo 270d mantido, o fetch não é mais pré-requisito do T2 — vira
otimização/robustez e pode rodar em paralelo.)*

- Novo `worker/Worker/guia_detail.py`:
  - `extrair_links_guia(driver) -> list[LinhaGuia]` — parse do `href` de cada linha
    (`CD_GUIA`, `dynaHash`, params) **e** dos campos de listagem (solicitação, nrGuia,
    código/descrição do hint, status, senha td[7]) — fonte única, reutilizada pelo fluxo atual.
  - `fetch_detalhe(driver, linha: LinhaGuia) -> DetalheGuia` — `driver.execute_script`
    com `fetch(url, {credentials:'include'}).then(r=>r.text())`; **fallback** para o clique
    Selenium atual (D2) se fetch falhar/vier com challenge (detectar `challenge-platform`).
  - `parse_detalhe(html) -> DetalheGuia` — **função pura** (BeautifulSoup): nrGuia
    (`tr[3]/td[2]`), autorização (`tr[4]/td[4]`), senha (`tr[5]/td[2]`), validade
    (`#CampoValidadeSenha`), `jsonConsistencia.hasItemAtivo`, bloqueio (`MSG_BLOQ_*` /
    `label_error*` visíveis). Reutilizar seletores atuais em constantes compartilhadas (DRY).
- Sem mudança de comportamento de gravação nesta task (só infraestrutura + testes unitários
  do parse com o HTML real anexado como fixture).

**AC:** `parse_detalhe` retorna validade `01/06/2027` para o HTML anexo; fallback aciona
quando fetch responde challenge; para linhas dentro da janela não há clique no caminho feliz
(log confirma); linhas fora da janela **nunca** geram fetch.

### T2 — Critério duplo: janela 270d (autorização) E validade ≥ hoje (M)
**Como** operação, **quero** que a coleta aplique os **dois marcos** da R1 e que o motivo da
exclusão apareça no card, **para** guias vencidas/antigas pararem de poluir a base/PEI e o
atendente saber o porquê de "não elegível".

- Em `ImportBaseGuias.py` (ramos `Autorizado` D1 e `Em Estudo`), a decisão por linha vira:
  1. `solicitação > 270d` → **para a varredura** (ordenação DESC mantém a otimização atual)
     e marca flag `fora_janela_270d` (havia guia Autorizado/Em Estudo fora da janela);
  2. dentro da janela → detalhe (T1 fetch, fallback clique) → `validade`:
     - `validade ≥ hoje` → coleta/UPSERT normal (renova `ativo`/`updated_at`);
     - `validade < hoje` → **não grava na base**; registra em
       `valida_prestador.guias[nn]` com `status_guia: "Vencida"` (exibição R2);
     - `validade ausente` → mantém coleta atual (guia sem senha/validade segue regra de
       bloqueio `Thered` vigente);
  3. bloqueio (`MSG_BLOQ_*`/`hasItemAtivo=false`/erro rede) → mantém classificação `Thered`.
- `tipo_json` final (truth table): qualquer elegível → `All Sucess`/`Thered` (compat);
  zero elegíveis + vencidas → `"Vencidas"`; + fora da janela → `"Antigas270"`; todas
  `Negado` → `"Negadas"`; tabela vazia → `"Sem Guias"` (string explícita; `null` = legado).
- Base existente: guias da base que deixarem de ser renovadas (vencidas/antigas) seguem o
  ciclo atual do cron 24h (`ativo=False`) — **sem** limpeza retroativa.

**AC:** reprocessando MARIA LUISA: nenhuma guia nova entra na base (663634xx é fora da
janela); guias 753638xx não renovam; job sai de "Sem Guias" → **"Antigas270"**; uma guia
dentro da janela com validade passada é classificada "Vencida" e não grava; guia Negada não
entra; os 8 pacientes com guia vigente na evidência passam a exibir "Antigas270" (sem
mudança na base).

### T3 — Timeout de portal = erro retryável (S)
**Como** operação, **quero** falha de carga tratada como erro, **para** retry automático e
não parecer "Sem Guias".

- `ImportBaseGuias.py:395-402` (e caminhos equivalentes de popup): ao estourar timeout,
  retornar `{"status": "error", "message": "Timeout carregando tabela de guias"}` — o
  `server.py` deve propagar para o dispatcher marcar `job.status=error` (D4), ativando o
  retry existente (`dispatcher.py:76-93`).
- Investigar D6 (prefixo `0000`): após `Button_Consulta`, aguardar/verificar tela
  intermediária (wait explícito por `s_NR_GUIA` **ou** por indicador de "validade da
  carteira") antes do timeout fatal; documentar achado no log.

**AC:** job com timeout fica `error` com mensagem clara e reprocessa (attempts+1); os 4
casos reais (ARTHUR LIMA, MARIA ISIS, PEDRO LUCAS, ISRAEL) reprocessam com sucesso ou
erro explícito; card nunca exibe "Sem Guias" para timeout.

### T4 — Rótulos do card e filtros (S)
**Como** atendente, **quero** distinguir Vencidas/Antigas/Negadas/Sem Guias no card e filtros.

- `backend/routes/jobs.py:100-107`: mapear novos valores (`vencidas`, `antigas270`,
  `negadas`) + compatibilidade com legado `null`.
- `frontend/src/pages/Importacoes.jsx:994-1073` (+ `Manual.jsx` legenda): chips/ícone/cores e
  opções de filtro — sugerido: 🟠 "Antigas (270d)" com tooltip *"guia existente fora da
  janela de 270 dias — PEI sujeito a bloqueio pela Unimed; renovar guia"*; "Timeout" aparece
  via status `error` (sem mudança de tela de erro).
- Atualizar `worker/docs/walkthrough.md` (legenda de status).

**AC:** filtro por cada novo status retorna os jobs esperados; card mostra rótulo correto
nos casos reais; legado (`tipo_json` null histórico) segue exibindo "Sem Guias".

### T5 — Observabilidade do processamento de guias (S)
**Como** PO/ops, **quero** rastreabilidade por guia, **para** auditoria e detecção de
mudança no portal.

- Log estruturado por guia no formato existente: `Guia {nr} solic={dt} validade={dt}
  acao={coletada|vencida|antiga270|negada|bloqueada|ignorada}
  origem={fetch|clique|listagem}` — `antiga270`/`negada` decidem só pela listagem
  (sem fetch); `validade` fica vazia nesses casos.
- Log-resumo por job: contagens por ação + `tipo_json` final (já parcialmente existe —
  `valida_prestador JSON`).
- Query de monitoramento (doc apenas): distribuição diária de `tipo_json` dos lotes 23:01 UTC
  (script de referência já validado na análise — `worker/analise/*.py`).

**AC:** para qualquer job é possível reconstruir a decisão de cada guia a partir dos logs.

---

## 5. Plano de validação — QA Tester (Dev Senior)

**Estratégia**: fixture-driven (HTML real do portal versionado em
`worker/tests/fixtures/`) → unit → integração com carteirinhas-piloto → lote completo
produção assistida. Sem deploy simultâneo de T2 e T4 no mesmo lote sem piloto prévio.

### 5.1 Unit (parse puro, sem rede)
| CT | Entrada | Esperado |
|---|---|---|
| CT-U1 | HTML detalhe anexo (66363404) | validade=01/06/2027, senha=953231282, nr=66363404, autorização=09/10/2025 |
| CT-U2 | HTML listagem anexo | 2 links com CD_GUIA/dynaHash distintos; senha td[7]; status Autorizado |
| CT-U3 | Detalhe com `MSG_BLOQ_BAIXA_CAPTURA_GUIA_PP_TOTAL` | bloqueio detectado, sem validade |
| CT-U4 | Detalhe sem `#CampoValidadeSenha` | validade=None → decisão tratada (nunca crash; segue regra de bloqueio) |
| CT-U5 | Tabela-verdade `tipo_json` (R1 dupla) | elegível (270d ✓ + validade ✓)→All Sucess/Thered; dentro-janela+vencida→"Vencidas"; fora-janela→"Antigas270"; todas Negado→"Negadas"; vazio→"Sem Guias" |
| CT-U6 | Decisão por linha (função pura) | solicitação 12/02/2026 em 10/11/2026 → fora da janela (270d = corte 13/02/2026); validade 01/06/2027 + solicitação recente → elegível; validade 02/11/2026 → vencida |

### 5.2 Integração (carteirinhas-piloto reais — dados da análise)
| CT | Carteirinha (job ref.) | Cenário | Esperado |
|---|---|---|---|
| CT-I1 | 0064.8000.053327.69-5 (186574) | Guia vigente c/ autorização 09/10/2025 (fora da janela) | **Nada novo na base** (663634xx inelegível pelo marco (a)); 753638xx não renovam; job "Sem Guias" → **"Antigas (270d)"** |
| CT-I2 | 0064.8000.423760.00-2 (186162) | 3 guias válidas 2027, autorização 12/02/2026 (1 dia além do corte) | Não entra/renova base; card "Antigas (270d)" — atende INVERTE o dia seguinte (cutoff anda 1/dia): QA valida a fronteira |
| CT-I3 | 0064.0000.011831.96-0 (186546) | Timeout prefixo 0000 | Job `error` + retry; após retry: card correto (1 válida 2029 + 1 bloqueada → Thered) |
| CT-I4 | 0064.9999.047011.10-1 (186396) | Tabela vazia real | Mantém "Sem Guias" (string nova) |
| CT-I5 | um dos 3 "só Negado" (186211/186382/186472) | Somente negadas | Card "Negadas"; nada na base |
| CT-I6 | Carteirinha prefixo 0317 (186208) | 18 guias, misto (dentro/fora janela) | Parada na 1ª linha fora da janela (sem fetch nas demais); tempo por job ≤ baseline atual |

### 5.3 Regressão obrigatória (gates)
1. **PEI (crítico na regra dupla)**: recalcular pacientes-piloto antes/depois — nenhuma guia
   vencida ou fora da janela pode alterar PEI; apenas guias elegíveis (270d ✓ + validade ✓)
   contam; `qtde_solicitada_pei` congela na 1ª inserção (trigger) — conferir. Snapshot de
   PEI dos pilotos antes do deploy.
2. **Cron 24h**: guias não renovadas (vencidas/antigas) continuam indo a `ativo=False`
   normalmente — inclusive as 3 guias ativas de HEITOR MENDES após o corte.
3. **Bloqueadas**: 255 jobs Thered do lote 10/11 permanecem classificados corretamente
   (reprocessar amostra de 10).
4. **Card/filtros**: cada filtro retorna contagens coerentes com a query de monitoramento.
5. **Lote completo**: rodar 1 lote noturno com o novo código em modo shadow-log (sem
   gravar) comparando decisões vs. produção; divergências documentadas antes do go-live.

**Critério de saída QA**: CT-U1..U6 + CT-I1..I6 verdes · gates 1–5 verdes · zero regressão
nas 255 bloqueadas · relatório de divergência do shadow-log assinado.

---

## 6. Entrega lean (ordem, MVP, riscos)

| Ordem | Task | Valor | Risco se falhar |
|---|---|---|---|
| 1º | **T3** (timeout=erro) | Quick win; deixa de mentir no card | Baixo — isolado |
| 2º | **T2** (dois marcos + rótulos no `tipo_json`) | **Núcleo** — card deixa de enganar em 96% dos casos (91 antigas + 4 vencidas/negadas + vazio) usando o fluxo de clique atual | Médio — PEI/base (gate 1) |
| 3º | **T4** (rótulos/filtros do card) | Decisão do atendente com motivo correto | Baixo — compat legado |
| 4º | **T1** (guia_detail via fetch) | Performance/robustez do detalhe dentro da janela | Baixo — fallback clique |
| 5º | **T5** (obs) | Auditoria contínua | Baixo |

*T1 e T5 podem rodar em paralelo a T2/T4 sem conflito (módulos distintos).*

**Fora de escopo (registrado)**: mudança no fluxo de login; captura do
`lista_procedimentos.do` para qtdes via fetch (manter clique/leitura atual — só avaliar se o
gate CT-I6 estourar tempo); migração dos 102 jobs históricos (serão reclassificados
naturalmente nos próximos lotes); buscar detalhe de guias fora da janela para exibir validade
no frame (se o negócio pedir, vira opt-in por env em fase 2).

**Riscos monitorados**: (a) portal mudar layout do detalhe → CT-U fixtures + fallback clique;
(b) CF challenge no fetch → fallback T1; (c) mudança de comportamento do prefixo 0000
persistir → achado do T3 define nova task; (d) guia dentro da janela sem validade legível
(CT-U4) → regra de bloqueio vigente cobre; (e) fronteira do corte anda 1 dia/dia — CT-I2/CT-U6
travam a semântica.

---

## Apêndice A — Dados de referência (produção, lote 10/11 23:01 UTC)
- Jobs "sem guias": 102 (91 corte-270d · 4 timeout · 4 vazio · 3 negadas).
- Pilotos: 186574 MARIA LUISA · 186162 HEITOR MENDES · 186546 ARTHUR LIMA · 186396 NOAH
  FELIPE (vazio) · 186208 ISRAEL · 186211 JOAO ANTONIO (negadas).
- Scripts de análise reutilizáveis pelo QA: `worker/analise/analise_lote_1011.py`,
  `worker/analise/classifica_sem_guias.py`, `worker/analise/analise_historico.py`, `worker/analise/analise_timeouts.py`
  (e `worker/analise/verifica_pos_lote.py`, `worker/analise/diag_pei_qtde.py`).
- HTMLs de referência do portal (listagem + detalhe 63342796/66363404): anexos desta task —
  versionar como fixtures em `worker/tests/fixtures/`.

## Apêndice B — Pós-lote (11-12/11) e desdobramentos (14/11)

**Validação do gate QA**: reclassificação perfeita dos 102 casos (90 Antigas270 · 4 Sem Guias
real · 3 Negadas · 2 viraram Válidas/Bloqueadas · 3 error/timeout). Zero "Sem Guias"
enganoso. Fix pós-lote: robustness sync não renova guias apenas-exibição (`373eb88`).

**Timeouts — causa raiz confirmada pelo diagnóstico do T3**: modal do portal
`modal/mensagens/index.do?msg=Beneficiário local não pode ser cadastrado como beneficiário de
intercâmbio` — o portal recusa carteirinhas de **intercâmbio** (não-0064) no fluxo novo exame.
> **Decisão PO (14/11)**: para carteirinhas com prefixo != 0064 o fluxo muda com frequência;
> o usuário fornecerá o fluxo manual para correção futura (task nova quando vier). Enquanto
> isso os casos ficam em `error` retryável e aparecem no painel de Insights (seção Portal).

**Guias com qtde=0 — diagnóstico fechado**: as 490 ativas com 0 são todas bloqueadas por
design (483 "Prestador principal...", 5 rede, 2 Antigas270); **zero** casos 'Guia Válida' com
0 e zero assinatura de bug de parsing (aut=0 com solic>0). Guia 76788780 (Enzo Gabriel):
bloqueada por design (`Prestador principal não possui itens liberados`).

**PEIs "duplicados" do Enzo**: eram **multi-carteirinhas** (trigger com UNIQUE por
carteirinha está correto). Mantido PEI por carteirinha; afunilamento por id_paciente é
futuro. Painel de Insights lista os 10 pacientes multi-carteirinhas para inativação manual.

**Migrations aplicadas**: 0038 (guard codigo NULL/vazio no calculate_patient_pei + NULLIF no
freeze + backfills defensivos) e 0039 (tabela insight_acoes) — aplicadas em produção 14/11.

**Novo**: painel de Inteligência (`GET /insights/resumo`) no modal de Importações — cards do
lote, lista de renovação de guia com mensagem pronta, carteirinhas duplicadas, anomalias
qtde=0, erros do portal e bloqueios agregados (backend/routes/insights.py + migration 0039).

## Apêndice C — PEI fantasma por troca de código da guia (caso 76788780, 17/11)

**Caso âncora (único no banco — relatório `analise/relatorio_fantasmas_pei.py`)**: guia
76788780 (Enzo Gabriel, 0064.0317.011904.67-8) inserida em 01/11 pelo caminho feliz com o
código da 1ª linha da tabela de terapias (2250005103, qtde 64) gerou PEI 4.0; ao ser
bloqueada e re-codificada pelo hint (2250005260 — INSUFICIÊNCIA DE REDE) no job de 04/11, o
trigger recalculou só o par novo — o par antigo (5103) ficou órfão com PEI ativo/Validado
para sempre. O "qtde=0" investigado em 14/11 nesta guia era sintoma deste bloqueio+troca,
não falha de leitura. Logs da época não existem mais (retenção ~2 dias).

**Correções (17/11)**:
1. **Scraper**: código da guia = HINT da listagem (autoritativo) em todos os caminhos
   (happy path e Em Estudo agora `guide_codigo or cod_terapia`); tabela de terapias segue
   só para quantidades e fallback. Diagnóstico novo: WARNING quando a guia tiver múltiplas
   linhas de terapia (códigos de todas registrados no log).
2. **Migration 0040**: `recalc_patient_pei_pair()` (fonte única) + trigger recalcula o par
   ANTIGO em UPDATE com código/carteirinha alterados (órfão inativa ou promove à próxima
   guia válida do par); backfill recalculou todos os pares — fantasma do Enzo inativado,
   zero fantasmas globais. Semântica: bloqueio → PEI inativo (sem outra guia válida);
   renovação/desbloqueio → reativa; troca de código → recalcula os dois pares.
3. Fixture `tests/fixtures/detalhe_guia_76788780_bloqueada.html` (detalhe bloqueado real).

**Efeito aceito**: guias válidas cujo hint não esteja na tabela `procedimentos` do convênio
passam a ser ignoradas com log "Procedimento não autorizado" (códigos INSUFICIÊNCIA DE
REDE podem ser cadastrados lá se o negócio quiser PEI deles).
