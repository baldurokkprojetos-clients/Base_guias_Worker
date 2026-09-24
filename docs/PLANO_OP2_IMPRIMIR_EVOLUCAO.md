# Plano de Implantação — OP2 `clmf_imprimir_evolucao` (ImprimirEvolucao) — **v2**

**Papel:** Dev Senior PO · **Data:** 2026-09-23 (rev. 2 pós-feedback) · **Projeto:** `C:\dev\clmf_hub_basic`
**Referência estrutural:** `C:\dev\Agenda_hub_MultiConv\Local_worker\Worker\101-aba_clmf` (padrão OPs + login OP0 + híbrido Selenium/requests)

> **STATUS DE IMPLANTAÇÃO (2026-09-23):** T1–T7 implementados e validados localmente
> (27/27 casos QA-A em `backend/scripts/qa_op2_unit.py`, sintaxe OK, build frontend OK,
> migration 0032 aplicada e validada no banco: tabelas + âncora id 1588 + índice de afinidade + uniques).
> Pendente: homologação QA-B/C/D/H contra o portal real com o arquivo de teste (2 pacientes / 37 jobs),
> depois liberar o arquivo completo (20.047 jobs). Nota: corrigido bug pré-existente em
> `backend/routes/jobs.py` (`current_user` sem `=` na assinatura de `list_jobs` — SyntaxError).

> **Mudanças da v2** (feedback do PO): colunas da planilha revisadas (`idPaciente`, `HoraInicial`);
> granularidade de job = **(idPaciente, DataExec)**; janela do filtro = **±30 dias da data do job**;
> `profissao_id` mapeado pela coluna **Terapia** (CodTerapia fora do JSON); nome do profissional obtido
> do select via **ID_prof**; texto de "marcar checkbox/selects" oficialmente removido da spec;
> **sem entidade carteirinha** no domínio (linha âncora única apenas por exigência de infraestrutura);
> data no nome do PDF em **dd-mm-aaaa**; estratégia de **afinidade por paciente** com impacto mínimo
> no dispatcher/Unimed.

---

## 1. Contexto e objetivo

Ampliar o leque de OPs do worker CLMF com a operação **ImprimirEvolucao**: a partir do upload da
planilha `evolucoes\evolucoes_2259525.xlsx` (50.167 linhas, 684 pacientes, 45.497 grupos
Guia/DataExec/ID_prof), o backend cria jobs **por (idPaciente, DataExec)** — 20.047 jobs —, cada um
encadeando os itens `[guia/ID_prof](horários)` daquela data. O worker processa cada item conciliando
os horários do job com os atendimentos do portal (`aba_atividade_single`), grava data/hora
inicial/hora final (horaInicial+1) via AJAX e baixa o PDF gerado para `C:\EVOLUCOES`, com fallback
para `evolution/pacient` (`evolution_single`). Resultados por item (OK / PENDENTE / ERRO) são
persistidos e exportáveis em Excel pelo menu **Importações**.

### Restrições explícitas (fora de escopo)
- **Não modificar a estrutura do worker**: `dispatcher.py`, `gui.py`, `ImportBaseGuias.py` e as OPs
  existentes (OP0 login, OP1 atualizar_rc) permanecem intactos. Exceções *aditivas* mínimas:
  `server.py` (branch de roteamento) e `worker\Worker\models.py` (classes espelho). A estratégia de
  afinidade (D14) foi desenhada para **não tocar o dispatcher**.
- Fila/claim multi-servidor existente (SKIP LOCKED, heartbeat, retry ≤3, recover >15 min) reaproveitada como está.

### Planilha modelo (reanalisada em 2026-09-23 22:01 — colunas alteradas)
Cabeçalhos exatos da aba `Planilha1`:
`idPaciente, nomePaciente, Guia, DataExec, Profissional, CodTerapia, Terapia, Sessões, HoraInicial, Lote, status, tipo, ID_prof`

| v1 | v2 (atual) | uso no job |
|---|---|---|
| `id` | **`idPaciente`** | chave do job + `client_id` das requisições |
| `Hora inicial` | **`HoraInicial`** | "07:00:00" → "0700" no JSON; "07:00" nas requisições |
| `Guia, DataExec, Profissional, CodTerapia, Terapia, Sessões, Lote, status, tipo, ID_prof` | idem | ver §3.1 |

- **Entram no JSON do job**: `idPaciente, nomePaciente, Guia, DataExec, Terapia, ID_prof, HoraInicial(+login/senha)`.
- **Não entram**: `CodTerapia` (código de procedimento, não é `profissao_id` do portal), `Profissional`
  (nome resolvido no portal via select por `ID_prof` — D6), `Sessões, Lote, status, tipo` (sem uso no fluxo).

---

## 2. Decisões de arquitetura e premissas (D1–D15)

| # | Decisão | Justificativa / evidência |
|---|---------|---------------------------|
| D1 | OP implementada **dentro de `clmf_scraper.py`** como método `imprimir_evolucao(params)` (OP2), com bloco de constantes e helpers privados seccionados no estilo do arquivo atual, espelhando o padrão 101-aba_clmf (constantes no topo, OPs como funções, híbrido Selenium→login / requests→dados). | Diretriz "apenas estruturar clmf_scraper.py como nova OP"; OP0 já existe e **é mantida** (`clmf_scraper.py:204`). |
| D2 | Nova `rotina` = **`clmf_imprimir_evolucao`**; roteamento aditivo em `server.py:157` → handler `_process_job_clmf_evolucao` (mesmo molde de `_process_job_clmf`). | Padrão atual `rotina == "clmf_atualizar_rc"`. |
| D3 | **1 job por (idPaciente, DataExec)** — 20.047 jobs na planilha real; `params` encadeia `itens[{guia, ID_prof, terapia, horas[]}]` daquela data. Itens processados **sequencialmente**; seleção de candidatos **atômica por item** (só grava quando fecha N=N horários do item). | Decisão do PO pós-análise de timeout: máx. 7 itens/38 requisições/10 horas por job (mediana 9 requisições) — tudo dentro dos 300s do dispatcher (`dispatcher.py:240`). |
| D4 | **Janela do filtro = espaçamento ±30 dias da data do job**: `dataInicial = DataExec − 30d`, `dataFinal = DataExec + 30d` (os valores "2026-01-01/2026-08-28" da spec original eram apenas exemplo de formato). No modo afinidade (D14), um único fetch por lote com `[min(DataExec do lote) − 30d, max(DataExec do lote) + 30d]`. | PO: "manter apenas a questão do espaçamento 30 dias a menos / 30 dias a mais". |
| D5 | `profissao_id` do portal obtido por **mapa nome→id extraído do `<select name="profissao_id">`**, casando com a coluna **`Terapia`** da planilha (nome da terapia). `CodTerapia` não participa. | PO: "a extração de profissao_id … é pela coluna Terapia que contém nome das terapias". |
| D6 | **Nome do profissional resolvido via `ID_prof`**: extraído o select `profissional_id` (id→nome), obtém-se o nome canônico com `ID_prof` do job; esse nome (normalizado) é o critério da camada 1 da conciliação e a validação do `profissional_id` enviado no PDF. A coluna `Profissional` da planilha não vai ao job. | PO: "a coluna ID_prof é enviada nos parâmetros do job para mapear o nome do profissional que será extraído da listagem, comparando ao ID_prof do job". |
| D7 | O trecho da spec original ("marcar checkbox… apagar campos… no select profissional enviar… no select profissao_id enviar") está **oficialmente removido** — substituído integralmente pelas requisições AJAX (`gravarNovaData`/`gravarHorario` sobrescrevem; `filled_pdf` recebe os ids explicitamente). Nenhuma manipulação de DOM além do login. | PO: "foram substituições pelas requisições e esqueci de excluir do prompt". Mesmo padrão HTTP puro da OP1. |
| D8 | **Isolamento por requisição HTTP**: o "monitorar carregamento" é resolvido com requests diretos (a resposta HTTP é da própria requisição — sem contaminação entre requisições), timeouts longos (filtro 120s, AJAX 60s, PDF 120s, download 180s), retry com backoff e validação de marcadores na resposta. Sessão expirada (form de login na resposta) → re-login OP0 e 1 retry. | O risco "processar itens de outra requisição" só existe lendo DOM compartilhado; a OP2 não navega o modal via Selenium. |
| D9 | **Sem entidade carteirinha no domínio de evoluções.** Porém a infraestrutura atual exige `carteirinha_id` inteiro válido: `dispatcher.py:560` lê `job.carteirinha_rel.carteirinha` sem guard (NULL quebraria o loop) e `JobRequest.carteirinha_id` é `int`. Solução: **uma única linha âncora** criada na migration (`carteirinha='EVOLUCOES-CLMF'`, `paciente='IMPORTACAO EVOLUCOES'`, `status='ativo'`), referenciada por todos os jobs da rotina. Zero mudança em dispatcher/server. | O prompt nunca solicitou carteirinha (D9 da v1 extrapolava); a âncora é dependência técnica mínima documentada. |
| D10 | **Self-persistência** (padrão 101-aba_clmf): a OP2 grava o status de cada item na tabela `evolucao_itens`; o dispatcher **não é alterado** (payload sem `guias_scraped` já é ignorado — job só vira `success`). A tabela é a fonte da verdade do export. | Evita mexer em `call_server`; dá progresso incremental e retomada em retry. |
| D11 | **Reserva atômica de candidatos entre servidores**: tabela `evolucao_claims` com `UNIQUE(fluxo, portal_item_id)`; `INSERT … ON CONFLICT` decide na hora se o candidato está livre (se conflito pertence ao próprio job, reutiliza). Protege o pool de candidatos mesmo com jobs do mesmo paciente em servidores diferentes. | Com 20k jobs, jobs do mesmo paciente podem rodar concorrentes em servers distintos; as camadas 1–3 (data do job) quase não colidem, mas a camada 4 ("outras datas") sim. |
| D12 | `HoraInicial` "07:00:00" → **"0700"** no JSON do job; conversão para "07:00" no momento das requisições; `horaFinal = horaInicial + 1h` (edge 23:00 → WARN + "00:00"). | Spec v1 (o "1700" era typo). |
| D13 | PDF por **item (guia/data/profissional)**, salvo em `C:\EVOLUCOES\{guia}-{dd-mm-aaaa}-{nomePaciente}-{profissao_id}.pdf` (ex.: `67575935-22-10-2026-ACSA CAMPOS GARCIA-2.pdf`); criar pasta sob demanda; sanitizar `\ / : * ? " < > |`. Sem link/path na resposta → item `ERRO` (motivo) e **segue o fluxo** ("se não abrir a ABA apenas ir para o próximo item"). | PO: data dd-mm-aaaa. |
| D14 | **Afinidade por paciente (reaproveitamento da URL lenta) com impacto zero no dispatcher**: ao iniciar um job, o próprio server **reivindica** (UPDATE atômico `FOR UPDATE SKIP LOCKED`) até `K` jobs irmãos pendentes do mesmo `idPaciente` (query via `params->>'idPaciente'`, índice parcial na migration), processa-os no mesmo request HTTP dentro de um **orçamento de tempo (~240s < timeout 300s)**, marca os processados diretamente no banco (padrão self-persisted do 101-aba_clmf) e **devolve os excedentes a `pending`** antes de responder. Caches por processo: mapa de profissionais/profissões (global, TTL) e candidatos do paciente (1 POST de filtro por lote com janela do D14/D4). Feature flag `EVOLUCAO_AFINIDADE` (default **ON**, degradável a OFF sem mudar código). | PO: "um server ao iniciar processamento capturar para si todos os jobs do idPaciente… mínimo impacto na estrutura do scraper da unimed". Dispatcher segue enxergando apenas jobs pending/processing normalmente; recover_stuck (>15 min) auto-cura claims órfãos de crash. |
| D15 | `login`/`senha` entram em `params` (spec) com **override** no scraper: `params` → senão `CLMF_LOGIN/CLMF_PASSWORD` (env). Recomendação registrada: migrar para `user_convenios` no futuro; hoje JSONB texto plano. | Spec exige; scraper já tem fallback de env. |

**Premissas a validar em homologação (bloqueantes — ver QA-H):** (a) resposta do POST do filtro é HTML
com `aba_atividade_single` (ou JSON embutindo HTML); (b) existe `select[name=profissao_id]` cobrindo as
7 terapias da planilha; (c) no fallback, o POST de filtro é para `/evolution/pacient/{id}` com os mesmos
campos; (d) `ReportsPDF … js=true` retorna JSON com path/URL do PDF (como `gerarRelatorio` retorna
`caminho`); (e) `gravarNovaData`/`gravarHorario` **sobrescrevem** valores existentes.

---

## 3. Contratos

### 3.1 `params` do job (JSONB, tabela `jobs`, rotina `clmf_imprimir_evolucao`)
```json
{
  "idPaciente": 4251,
  "nomePaciente": "ACSA CAMPOS GARCIA",
  "dataExec": "2026-01-12",
  "login": "REC2209525",
  "senha": "***",
  "dataInicial": "2025-12-13",
  "dataFinal": "2026-02-11",
  "itens": [
    {"guia": "67575935", "ID_prof": 3227, "terapia": "Psicomotricidade", "horas": ["0700"]},
    {"guia": "67575935", "ID_prof": 193,  "terapia": "Psicomotricidade", "horas": ["0800"]}
  ]
}
```
- `dataInicial/dataFinal` = `dataExec ± 30d` (D4), pré-calculadas no upload.
- `CodTerapia`, `Profissional`, `Sessões`, `Lote`, `status`, `tipo` **não** entram (§1).

### 3.2 Novas tabelas (migration `0032_create_evolucao_itens.sql`)

**`evolucao_itens`** (resultado por linha conciliada — fonte do export):
| coluna | tipo | obs |
|---|---|---|
| id | serial PK | |
| job_id | int FK jobs ON DELETE CASCADE | index |
| lote | text | arquivo+ts do upload (filtro no export) |
| idPaciente | int | index |
| nomePaciente | text | |
| guia | text | |
| data_exec | date | index |
| profissional_id | int | `ID_prof` da planilha |
| terapia | text | nome (coluna Terapia) |
| profissao_id | int nullable | preenchido na conciliação (mapa do select) |
| hora_inicial | text | "0700" |
| status | text | `PENDENTE` → `PROCESSANDO` → `OK` \| `PENDENTE` (itens insuficientes) \| `ERRO` (index) |
| motivo | text nullable | ex.: "sem candidatos suficientes (primário+fallback)", "PDF sem link" |
| ids_conciliados | jsonb nullable | ids do portal usados |
| pdf_path | text nullable | caminho salvo |
| created_at / updated_at | timestamptz | default now / onupdate |
| **unique** | (`job_id`,`guia`,`data_exec`,`profissional_id`,`hora_inicial`) | idempotência de retry |

**`evolucao_claims`** (reserva atômica de candidatos — D11):
`id serial PK · fluxo text ('aba'\|'evolution') · portal_item_id bigint · job_id int FK jobs ON DELETE CASCADE · created_at`
**UNIQUE(`fluxo`,`portal_item_id`)** — INSERT ON CONFLICT DO NOTHING decide a reserva.

**Ajustes na mesma migration:**
- `INSERT ... ON CONFLICT DO NOTHING` da **carteirinha âncora** `EVOLUCOES-CLMF` (D9).
- Índice parcial expression: `CREATE INDEX idx_jobs_evolucao_paciente ON jobs ((params->>'idPaciente')) WHERE rotina='clmf_imprimir_evolucao';` (query de afinidade D14).

### 3.3 Endpoints backend (`backend\routes\evolucoes.py`, prefixo `/evolucoes`, auth `api_key`)
- `POST /evolucoes/upload` (multipart `file` xlsx) → valida cabeçalhos (§1), agrupa por
  **(idPaciente, DataExec)**, cria jobs (`status=pending`, todos apontando para a âncora) **ordenados
  por paciente e depois data** (FIFO favorece afinidade), bulk-insert itens `PENDENTE`;
  retorna `{lote, pacientes, datas, jobs, itens}`.
- `GET /evolucoes/jobs` → jobs da rotina com contadores agregados de `evolucao_itens`
  (`total/ok/pendente/erro`) para o painel (agregado por paciente para não paginar 20k linhas).
- `GET /evolucoes/export?lote=&job_id=&status=` → `StreamingResponse` xlsx (openpyxl write_only,
  padrão `routes/guias.py:45`), colunas: **idPaciente, guia, data, profissional_id, horaInicial, status**
  (+ `motivo`, `pdf_path` como extras).
- `GET /jobs?rotina=` (backend existente) → ganha filtro aditivo por rotina para a lista de Importações
  não ser inundada pelos 20k jobs novos.
- Script CLI `backend\scripts\export_evolucoes.py` (mesma função de serviço — DRY).

### 3.4 Requisições ao portal (POST form-encoded, cookies da sessão OP0, `X-Requested-With: XMLHttpRequest`)
| etapa | URL | payload |
|---|---|---|
| Modal (etapa 0, 1× por processo/refresh) | `GET /reports/answers_treatment/{idPaciente}` | — (parse dos selects `profissional_id` e `profissao_id`) |
| Filtro | `POST /reports/answers_treatment/{idPaciente}` | `profissional_id=&profissao_id=&data_inicial={dataInicial}&data_final={dataFinal}&public=` |
| Nova data | `POST /_ajax/Aba.ajax.php` | `callback=Aba&callback_action=gravarNovaData&data={dataExec ISO}&pergunta={id_item}` |
| Hora inicial | `POST /_ajax/Aba.ajax.php` | `callback=Aba&callback_action=gravarHorario&hora=HH:MM&pergunta={id_item}&tipo=I` |
| Hora final | `POST /_ajax/Aba.ajax.php` | `callback=Aba&callback_action=gravarHorario&hora=(HH+1):MM&pergunta={id_item}&tipo=F` |
| PDF primário | `POST /_ajax/ReportsPDF.ajax.php` | `callback=ReportsPDF&callback_action=filled_pdf&atendimento_id[]={id1}&atendimento_id[]={id2}&client_id={idPaciente}&js=true&profissional_id={ID_prof}&tipoArquivo=pdf&profissao_id={mapa Terapia}` |
| Fallback página | `GET /evolution/pacient/{idPaciente}` | parse `evolution_single` (+ filtro análogo — premissa c) |
| Fallback gravações | `POST /_ajax/Evolution.ajax.php` | mesmas ações `gravarNovaData` / `gravarHorario(I/F)`, `callback=Evolution` |
| PDF fallback | `POST /_ajax/ReportsPDF.ajax.php` | `callback=ReportsPDF&callback_action=filled_pdf_month_evolution&registro_id[]={id1}&...&client_id={idPaciente}&js=true&profissional_id={ID_prof}&tipoArquivo=pdf` |

### 3.5 Retorno da OP2 (resultado do job)
```json
{
  "status": "success",
  "op": "clmf_imprimir_evolucao",
  "idPaciente": 4251,
  "dataExec": "2026-01-12",
  "resumo": {"itens": 2, "ok": 2, "pendente": 0, "erro": 0},
  "itens": [{"guia":"67575935","ID_prof":3227,"hora_inicial":"0700","status":"OK",
             "ids_conciliados":[1059571],"pdf_path":"C:\\EVOLUCOES\\67575935-12-01-2026-ACSA CAMPOS GARCIA-2.pdf"}],
  "afinidade": {"jobs_irmaos_processados": 3, "liberados": 0},
  "self_persisted": true
}
```
Job fica `success` ao final (significa "processado"); **status por item vive em `evolucao_itens`**.

---

## 4. Algoritmo de conciliação (por item do job; N = len(horas))

Candidato (linha `tr.aba_atividade_single` / `tr.evolution_single`): `{id, data(dd/MM/yyyy), hora,
terapia, profissional, novaData, horaInicial, horaFinal}` (extras dos inputs `#novaData_{id}`,
`#horaInicial_{id}`, `#horaFinal_{id}`). Normalização de nomes: `strip + upper + collapse espaços`
(opções do select vêm com `&raquo; ` — remover entidades).

**Pré-requisitos do job:** nome do profissional canônico = `select_profissionais[ID_prof]` (D6);
`profissao_id` = `select_profissoes[terapia_normalizada]` (D5); candidatos do filtro já carregados
(fetch próprio ou cache do lote de afinidade — D14).

Camadas de prioridade (ordem de preenchimento até somar N; dentro de cada camada, **primeiro os de
`novaData`+`horaInicial`+`horaFinal` vazios**, depois os demais, ordem estável por id):

1. `data == dataExec` **e** `terapia == terapia_do_item` **e** `profissional == nome_canônico(ID_prof)`
2. `data == dataExec` **e** `terapia == terapia_do_item` (outros profissionais)
3. `data == dataExec` (outras terapias)
4. quaisquer outras datas

Regras transversais:
- **Reserva atômica (D11):** antes de gravar, cada candidato selecionado é reservado em
  `evolucao_claims` (`INSERT ON CONFLICT`); candidato tomado por outro job → substituir e repetir a
  seleção (o claim do próprio job em retry é reutilizado).
- Seleção atômica: sem N itens no fluxo primário → tenta o **fallback inteiro** para o item; sem N no
  fallback → status `PENDENTE` (motivo), **nenhuma requisição de gravação enviada**.
- Selecionados = N → para cada candidato: `gravarNovaData(dataExec)` + `gravarHorario(I, hora)` +
  `gravarHorario(F, hora+1)`; atualizar o cache local de candidatos com os valores gravados
  (invalidação pós-escrita — D14); depois 1 chamada de PDF com todos os ids; download → `OK`.
- PDF sem link/path → `ERRO` (motivo) e segue o fluxo (D13).

Pseudocódigo:
```
afinidade: reivindicar até K jobs irmãos pendentes do idPaciente (SKIP LOCKED, params->>'idPaciente')
           → janela do lote = [min(dataExec)−30d, max(dataExec)+30d]; 1 POST de filtro por fluxo
sem afinidade: janela = dataExec±30d; 1 POST de filtro por fluxo
para cada job (o despachado + irmãos dentro do orçamento de 240s):
    para cada item em params.itens (guia/ID_prof/terapia/horas):
        se evolucao_itens.status == OK: pular (retomada)
        sel = reservar(selecionar(candidatos_cache, usados, item, N))   # camadas 1→4
        se len(sel) < N (após fallback): persistir PENDENTE(motivo); continuar
        para c in sel: gravar_data + gravar_hora_I + gravar_hora_F (fluxo correspondente)
        pdf = gerar_pdf(fluxo, sel, client_id=idPaciente, profissional_id=ID_prof, profissao_id)
        baixar → persistir OK(pdf_path) | persistir ERRO(motivo)
irmãos não processados (estouro de orçamento): UPDATE de volta para pending/locked_by=NULL
```

---

## 5. Tasks para o Dev Senior Fullstack

> Dependências: T1 → T3 → T4 → (T2 paralelo) → T5 → T5b → T6 → T8.

### T1 — Migration `0032_create_evolucao_itens.sql` (1h)
- [ ] `evolucao_itens` / `evolucao_claims` conforme §3.2 + índices + uniques.
- [ ] INSERT idempotente da carteirinha âncora `EVOLUCOES-CLMF`.
- [ ] Índice parcial expression em `jobs(params->>'idPaciente') WHERE rotina='clmf_imprimir_evolucao'`.
- **ACEITE:** uniques impedem duplicata de item e de claim; âncora existe; EXPLAIN da query de afinidade usa o índice.

### T2 — Backend: service + rotas + script de export (4h)
- [ ] `backend/models.py`: `EvolucaoItem`, `EvolucaoClaim`.
- [ ] `backend/services/evolucao_service.py` (DRY, funções puras testáveis):
  - `parse_planilha(file)`: openpyxl `read_only` (padrão `routes/carteirinhas.py:99`); cabeçalhos
    exatos de §1 (normalizar maiúsculas/espaços); rejeitar linhas sem
    `idPaciente/Guia/DataExec/HoraInicial/ID_prof/Terapia` com relatório de erros por linha;
    `HoraInicial` "07:00:00"→"0700"; datas → ISO; **não** propagar `CodTerapia/Profissional`.
  - `criar_jobs_evolucao(db, file, user)`: agrupa por `(idPaciente, DataExec)`; `dataInicial/Final =
    dataExec±30d`; jobs criados **ordenados por paciente, depois data**, todos com a âncora;
    bulk-insert itens `PENDENTE`; transação por job (job inválido não derruba o lote).
  - `gerar_xlsx_status(db, lote=None, job_id=None, status=None) -> BytesIO`: colunas §3.3.
- [ ] `backend/routes/evolucoes.py` (3 endpoints §3.3) + filtro `rotina` no `GET /jobs` existente + registro em `main.py`.
- [ ] `backend/scripts/export_evolucoes.py` (CLI, usa `gerar_xlsx_status`).
- **ACEITE:** upload da planilha real cria **20.047 jobs / 45.497 itens PENDENTE** (jobs do mesmo
paciente com ids consecutivos); export devolve xlsx íntegro; `GET /jobs?rotina=clmf_imprimir_evolucao` filtra.

### T3 — Worker: espelhos de model (0.5h)
- [ ] `worker\Worker\models.py`: adicionar `EvolucaoItem` e `EvolucaoClaim` idênticos ao backend.
- **ACEITE:** imports funcionam no contexto do worker (mesmo DB Supabase).

### T4 — `clmf_scraper.py`: OP2 `imprimir_evolucao` (12h) — *núcleo*
- **T4.1 Constantes** (bloco novo no topo, estilo 101-aba_clmf `config/constants.py`):
  `ANSWERS_TREATMENT_URL_TEMPLATE`, `EVOLUTION_PACIENT_URL_TEMPLATE`, `AJAX_ABA_URL`,
  `AJAX_EVOLUTION_URL`, `AJAX_REPORTS_PDF_URL`, `EVOLUCOES_DIR=r"C:\EVOLUCOES"`, timeouts
  (`FILTER_TIMEOUT=120`, `AJAX_TIMEOUT=60`, `PDF_TIMEOUT=120`, `DOWNLOAD_TIMEOUT=180`), ações
  (`ACTION_GRAVAR_NOVA_DATA`, `ACTION_GRAVAR_HORARIO`, `ACTION_FILLED_PDF`,
  `ACTION_FILLED_PDF_MONTH_EVOLUTION`), regexes de parse (`RE_SELECT_OPTION`, `RE_ATIVIDADE_ROW` para
  `aba_atividade_single` e `evolution_single` com grupos nomeados), `RE_LOGIN_FORM` (sessão expirada),
  `AFINIDADE_K=15`, `AFINIDADE_BUDGET_S=240`.
- **T4.2 Sessão HTTP**: `_evolucao_session()` (cookies `_session_cookies`, headers); 
  `_request_com_retry(method,url,data,timeout,validadores)` — 3 tentativas, backoff 5s, detecção de
  sessão expirada → `self.login()` + 1 retry.
- **T4.3 Etapa 0 + caches**: `_carregar_modal(idPaciente)` → GET página (1× por processo/refresh de
  TTL); `_extrair_profissionais(html)` → dict id→nome; `_extrair_profissoes(html)` → dict
  terapia_norm→profissao_id. Cache de candidatos por paciente (chave: idPaciente+janela), com
  **invalidação local pós-escrita** (valores gravados refletem no cache).
- **T4.4 Filtro + parse**: `_post_filtro(url, data_inicial, data_final)` (payload §3.4, campos vazios);
  `_parse_candidatos(html)` → lista de dicts do §4 (validar marcador da resposta).
- **T4.5 Seleção em camadas**: `_selecionar_candidatos(candidatos, usados, item, nome_prof_canonico)`
  — §4 exato (função pura, sem I/O → testável).
- **T4.6 Reserva**: `_reservar_candidatos(fluxo, ids, job_id)` — `INSERT ON CONFLICT` + re-seleção em
  loop até fechar N ou esgotar pool.
- **T4.7 Gravações**: `_gravar_data_e_horarios(fluxo, item_id, data_iso, hora_ini, hora_final)` —
  3 POSTs; validar HTTP 200 + resposta sem `trigger` de erro (heurística como op3 do 101-aba_clmf).
- **T4.8 PDF + download**: `_gerar_pdf(fluxo, ids, idPaciente, ID_prof, profissao_id)` → extrair
  path/URL do JSON (fallback: varrer resposta por `.pdf`); `_baixar_pdf_evolucao(url, nome)` — padrão
  `_download_pdf` da classe com `EVOLUCOES_DIR` + nome `{guia}-{dd-mm-aaaa}-{nomePaciente}-{profissao_id}.pdf` (D13).
- **T4.9 Orquestração + self-persist**: `imprimir_evolucao(params)` — fluxo §4; override de credenciais
  (D15); persistir transições de status em `evolucao_itens` (commit por item); try/except por item
  (falha → `ERRO`+motivo, fluxo segue); retorno §3.5. Sem alterar OP0/OP1.
- **ACEITE (unit/DEV):** camadas cobertas por testes com HTML fixture das duas tabelas; dry-run de 1
  paciente pequeno em homologação gera itens OK/PENDENTE coerentes e PDFs em `C:\EVOLUCOES`.

### T5 — `server.py`: roteamento aditivo (1h)
- [ ] Em `process_job` (`server.py:156`): `elif job.rotina == "clmf_imprimir_evolucao": return _process_job_clmf_evolucao(job)`.
- [ ] `_process_job_clmf_evolucao`: molde de `_process_job_clmf` (driver_lock, `_ensure_driver`,
  `last_activity_time`), chamando `clmf_scraper.imprimir_evolucao(job.params)`. JobRequest segue
  intacto (âncora fornece `carteirinha_id` válido — D9). **Não tocar** em dispatcher/gui/Unimed.
- **ACEITE:** job manual de teste é consumido e chega à OP2 (log).

### T5b — Afinidade por paciente (flag `EVOLUCAO_AFINIDADE`, default ON) (3h)
- [ ] No início da OP2: `_reivindicar_irmaos(db, idPaciente, server_url, K)` — `UPDATE ... WHERE id IN
  (SELECT ... WHERE rotina='clmf_imprimir_evolucao' AND status='pending' AND locked_by IS NULL AND
  params->>'idPaciente'= :pid ORDER BY id LIMIT :k FOR UPDATE SKIP LOCKED)`.
- [ ] Processar irmãos no mesmo request dentro do orçamento de 240s; ao final: processados →
  `success/error` + `locked_by=NULL` gravados pelo próprio server (self-persisted); excedentes →
  devolver a `pending`. Jobs do próprio job despachado seguem fluxo normal do dispatcher.
- [ ] Crash no meio do lote → irmãos ficam `processing` → `recover_stuck_jobs` (>15 min) reenfileira (auto-cura).
- **ACEITE:** com 2 servers locais e 10 jobs de 1 paciente: 1 server processa o lote (1 fetch de
  filtro por fluxo), o outro não pega os mesmos jobs; sem afinidade (flag OFF) o fluxo permanece correto.

### T6 — Frontend: painel em Importações (3h)
- [ ] `frontend/src/services/api.js`: `uploadEvolucoes(file)`, `getEvolucoesJobs()`, `exportEvolucoes(params)` (blob).
- [ ] `frontend/src/pages/Importacoes.jsx`: card "Evoluções CLMF — Imprimir Evolução" com `input file`
  (.xlsx), botão **Importar** (resumo `pacientes/datas/jobs/itens`), painel agregado por paciente com
  polling 5s (`OK/PENDENTE/ERRO/total`), botão **Exportar Status** → download
  `evolucoes_status_{yyyyMMdd_HHmm}.xlsx` (padrão `BaseGuias.jsx:126`).
- [ ] Lista existente de jobs: filtro por rotina (usa `GET /jobs?rotina=`) para não ser inundada.
- **ACEITE:** upload via UI cria os jobs; export baixa xlsx com as 6 colunas obrigatórias; lista
  legacy de Importações não mostra os 20k jobs de evoluções (ou mostra só sob filtro).

### T7 — Documentação operacional (1h)
- [ ] Docstring do `clmf_scraper.py` (OP2 no cabeçalho) + `worker/docs`: fluxo, endpoints internos,
  premissas (a)–(e), flag de afinidade, throughput esperado e como operar o export.

### T8 — QA (seção 6) + homologação com 5–10 pacientes reais antes do lote completo.

---

## 6. Checklist QA (Dev QA Tester)

### QA-A — Unitários (automação, pytest)
- [ ] `parse_planilha`: header v2 (`idPaciente`, `HoraInicial`); "07:00:00"→"0700"; datetime→ISO;
  linha inválida reportada sem abortar; agrupamento `(idPaciente,DataExec)` com itens
  `{guia,ID_prof,terapia,horas}` (casos 1..5 horas); `CodTerapia`/`Profissional` ausentes do payload.
- [ ] `criar_jobs_evolucao`: `dataInicial/Final = dataExec±30d` (testar mês curto/ano bissexto);
  jobs do mesmo paciente com ids consecutivos; todos apontam a âncora.
- [ ] `_selecionar_candidatos` (tabela de decisão — cada linha um caso):
  - [ ] N itens na camada 1 → exatamente eles; campos vazios priorizados dentro da camada;
  - [ ] camada 1 insuficiente → completa com camada 2 (mesma terapia, outro profissional);
  - [ ] 1+2 insuficientes → camada 3 (outras terapias, mesma data);
  - [ ] mesma data esgotada → camada 4 (outras datas), vazios primeiro;
  - [ ] total < N em ambos os fluxos → nenhuma gravação enviada (mock das requests recebe 0 calls);
  - [ ] candidato já reservado (`evolucao_claims`) nunca é selecionado; claim do próprio job é reutilizado em retry.
- [ ] `horaFinal = horaInicial + 1` (inclui edge 23:00 → WARN + "00:00").
- [ ] Nome do PDF: `{guia}-{dd-mm-aaaa}-{nomePaciente}-{profissao_id}.pdf` sanitizado (acento/espaço ok).
- [ ] Mapas dos selects: `&raquo;  DANIELLA  SANDES PEREIRA` → id 1643; `ID_prof`→nome canônico;
  `Terapia`→`profissao_id` (as 7 terapias da planilha resolvem; terapia sem correspondência → item ERRO com motivo claro).

### QA-B — Contrato de fila (integração local)
- [ ] `POST /process_job` com `rotina="clmf_imprimir_evolucao"` retorna envelope `{status,data,carteirinha_id}`.
- [ ] Job com erro na OP2 → `error` + Log ERROR; retry reprocessa **pulando itens já OK**.
- [ ] Dois servers não pegam o mesmo job (SKIP LOCKED); heartbeat segue funcionando.
- [ ] Dispatcher **não alterado** (QA-G) e `dispatcher.py:560` segue funcionando com a âncora.

### QA-C — Fluxo primário (homologação, 1 paciente pequeno)
- [ ] Login OP0 reutilizado entre itens/jobs; cookies propagados nas AJAX.
- [ ] POST do filtro usa `dataExec±30d` e campos vazios `profissional_id/profissao_id/public`.
- [ ] Por item conciliado: exatamente `gravarNovaData` + `gravarHorario I` + `gravarHorario F` com
  `pergunta=id` correto; conferir no portal que data/horários gravados (e **sobrescrevem** valores pré-existentes).
- [ ] `filled_pdf` contém todos os `atendimento_id[]`, `client_id=idPaciente`, `profissional_id=ID_prof`,
  `profissao_id` mapeado por Terapia; PDF salvo com nome D13 em `C:\EVOLUCOES`.
- [ ] Resposta sem link de PDF → `ERRO` + motivo, fluxo segue.

### QA-D — Fallback e insuficiência
- [ ] Sem `aba_atividade_single` suficiente → `/evolution/pacient/{id}`; gravações `Evolution.ajax.php`;
  PDF `filled_pdf_month_evolution` com `registro_id[]`.
- [ ] Insuficiente nos dois fluxos → `PENDENTE` com motivo; **nenhuma** gravação enviada.
- [ ] Retomada: reiniciar job no meio → continua do primeiro item não-OK.

### QA-E — Persistência, export e afinidade
- [ ] `evolucao_itens` reflete o resultado (`resumo` == contadores da tabela).
- [ ] `GET /evolucoes/export`: colunas `idPaciente, guia, data, profissional_id, horaInicial, status`
  (+motivo/pdf_path); filtros lote/job/status; abre no Excel. Script CLI gera arquivo idêntico (mesma função).
- [ ] Afinidade ON: lote de N jobs do mesmo paciente processado com **1 POST de filtro por fluxo**
  (contar em log); excedentes devolvidos a `pending` antes de 300s; jobs irmãos marcados pelo server.
- [ ] Afinidade OFF: fluxo correto (1 filtro por job); flag lida por env sem recompilar.
- [ ] Crash simulado no meio do lote → irmãos `processing` reenfileirados por recover_stuck (≤15 min).

### QA-F — Upload/frontend
- [ ] Upload da planilha real: 20.047 jobs / 45.497 itens; resumo correto; header errado → 422 claro.
- [ ] Painel agregado por paciente com polling; Exportar Status baixa arquivo; lista legacy filtrável por rotina.
- [ ] Importações existentes (Unimed) e OP1 CLMF sem regressão.

### QA-G — Não-regressão / restrições
- [ ] `git diff` não altera: `dispatcher.py`, `gui.py`, `ImportBaseGuias.py`, OP0/OP1 (métodos `login`,
  `atualizar_rc` e helpers RC intactos), rotas existentes (exceto filtro aditivo `rotina`).
- [ ] `C:\EVOLUCOES` criada sob demanda; locks de driver preservados (jobs de evoluções não derrubam
  driver de job Unimed/RC no mesmo server — driver_lock serializa).

### QA-H — Homologação das premissas (bloqueantes, DevTools no portal)
- [ ] (a) resposta do POST filtro é HTML com `aba_atividade_single` (ou JSON c/ HTML) — ajustar parser se preciso;
- [ ] (b) `select[name=profissao_id]` existe e cobre as 7 terapias;
- [ ] (c) URL/campos do POST de filtro do fallback confirmados;
- [ ] (d) `filled_pdf` com `js=true` retorna JSON com path/URL;
- [ ] (e) `gravarNovaData`/`gravarHorario` sobrescrevem valores existentes.

---

## 7. Riscos e mitigações

| # | Risco | Evidência | Mitigação |
|---|---|---|---|
| R1 | ~~Timeout 300s do dispatcher~~ | v1 (jobs por paciente, máx. 604 grupos) | **Resolvido pela granularidade v2**: máx. 38 requisições/job (~60–90s pior caso) — dentro de 300s e do watchdog de 10 min. Afinidade respeita orçamento de 240s e devolve excedentes. |
| R2 | Premissas (a)–(e) do portal divergem | Spec descreve fluxo manual | QA-H antes do lote; parsers isolados em regexes/constantes de fácil ajuste (T4.1). |
| R3 | Gravações com conciliação incorreta | Escrita em produção | Seleção atômica + funções puras testadas (QA-A); piloto 5–10 pacientes; log de cada POST (padrão `[DEBUG URL ENCODED PAYLOAD]` da OP1). |
| R4 | Sessão expirar entre jobs | Sessão longa | Detecção de form de login na resposta + re-login OP0 automático (T4.2). |
| R5 | Credenciais em texto plano no JSONB | D15 | Recomendação registrada; migração futura para `user_convenios` (como 101-aba_clmf). |
| R6 | Mesmo candidato consumido por jobs concorrentes (camada 4) | 20k jobs, vários servers | `evolucao_claims` com UNIQUE atômico (D11/T4.6). |
| R7 | Cache de candidatos defasado após gravações (afinidade) | Escrita muda estado do portal | Invalidação local pós-escrita (T4.3); refresh do pool ao trocar de paciente. |
| R8 | 20k jobs inundarem UI/fila e demorar dias | 20.047 jobs × ~12s mediana ≈ 67h em 1 server | Filtro por rotina na lista (T6); afinidade reduz POSTs de filtro (~1 por paciente/fluxo/server); escalar N servers (claim SKIP LOCKED já suporta); prioridade FIFO por paciente preservada pela ordenação do upload. |
| R9 | Afinidade interferir no dispatcher | Claim de irmãos muda status de jobs | Desenhada para o contrato existente: irmãos ficam `processing/locked_by` (invisíveis ao claim do dispatcher), são finalizados pelo server (self-persisted) ou devolvidos a `pending`; crash auto-cura via recover_stuck; flag OFF desativa sem código. |

---

## 8. Ordem de execução e marcos

1. **Sprint 1 (dev):** T1 → T3 → T4.1–T4.6 (fixtures) → QA-A.
2. **Sprint 2 (dev):** T4.7–T4.9 → T5 → QA-B/QA-C/QA-D/QA-H em homologação (ajustes de parser).
3. **Sprint 3 (dev):** T5b → QA-E (afinidade ON/OFF) → T2 → T6 → T7 → QA-F/QA-G; piloto 5–10
   pacientes; depois lote completo.
4. **Critério de aceite final:** planilha real processada end-to-end (20.047 jobs), PDFs em
   `C:\EVOLUCOES` no padrão `{guia}-{dd-mm-aaaa}-{nome}-{profissao_id}.pdf`, export fiel, afinidade
   estável, zero alteração estrutural no worker (dispatcher/gui/Unimed/OP0/OP1 intocados).
