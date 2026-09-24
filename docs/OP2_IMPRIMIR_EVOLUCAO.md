# OP2 — ImprimirEvolucao (rotina `clmf_imprimir_evolucao`)

Plano completo: `PLANO_OP2_IMPRIMIR_EVOLUCAO.md` (raiz do projeto). Resumo operacional.

## Fluxo
1. Upload da planilha modelo (menu **Importações → Evoluções CLMF**) → `POST /evolucoes/upload`.
   Cabeçalhos: `idPaciente, nomePaciente, Guia, DataExec, Profissional, CodTerapia, Terapia,
   Sessões, HoraInicial, Lote, status, tipo, ID_prof`. Criado **1 job por (idPaciente, DataExec)**,
   janela pré-calculada `dataInicial/dataFinal = dataExec ± 30d`, itens `[guia/ID_prof](horas)`
   (`HoraInicial` "07:00:00" → "0700"). `CodTerapia`/`Profissional` não vão ao job.
2. Dispatcher (inalterado) entrega o job a qualquer server → `server.py` rotinha
   `clmf_imprimir_evolucao` → `CLMFScraper.imprimir_evolucao(params)`.
3. OP0 login (reuso de sessão) → `GET /reports/answers_treatment/{id}` (selects
   `profissional_id`/`profissao_id`, cache 30 min) → `POST` filtro (±30d) → por item:
   seleção em camadas (data+terapia+profissional → data+terapia → data → outras datas;
   campos vazios primeiro) + **reserva atômica** em `evolucao_claims` →
   `Aba.ajax.php gravarNovaData/gravarHorario I/F` (horaFinal = horaInicial+1) →
   `ReportsPDF filled_pdf` → download em `C:\EVOLUCOES\{guia}-{dd-mm-aaaa}-{nome}-{profissao_id}.pdf`.
   Insuficiente → fallback `/evolution/pacient/{id}` (`evolution_single`, `Evolution.ajax.php`,
   `filled_pdf_month_evolution`). Insuficiente nos dois → item `PENDENTE` (sem gravações).
4. Status por item persistido em `evolucao_itens` (OK/PENDENTE/ERRO + motivo + pdf_path);
   export: botão **Exportar Status** (`GET /evolucoes/export`) ou CLI
   `backend/scripts/export_evolucoes.py`.

## Afinidade por paciente (`EVOLUCAO_AFINIDADE`, default ON)
Ao iniciar um job, o server reivindica até `EVOLUCAO_AFINIDADE_K` (15) jobs irmãos pendentes do
mesmo `idPaciente` (`FOR UPDATE SKIP LOCKED` via `params->>'idPaciente'`) e os processa no mesmo
request, reaproveitando selects e filtro (cache com união de janelas), dentro de um orçamento de
240s (< timeout 300s do dispatcher). Excedentes voltam a `pending`; crash no meio do lote é
auto-curado pelo `recover_stuck_jobs` (15 min). Desligar: `EVOLUCAO_AFINIDADE=false`.

## Credenciais
`params.login/senha` do job (quando o backend tem `CLMF_LOGIN/CLMF_PASSWORD` no env) → senão env
do worker `CLMF_LOGIN/CLMF_PASSWORD`. Recomendação futura: migrar para `user_convenios`.

## Premissas a validar em homologação (QA-H do plano)
(a) resposta do POST do filtro é HTML com `aba_atividade_single`; (b) select `profissao_id` cobre
as 7 terapias; (c) filtro do fallback = POST em `/evolution/pacient/{id}` com os mesmos campos;
(d) `filled_pdf js=true` retorna JSON com path/URL (ou URL `.pdf` no corpo); (e) `gravarNovaData`/
`gravarHorario` sobrescrevem valores existentes.

## Tabelas (migration 0032)
- `evolucao_itens` — resultado por linha (fonte do export); unique
  `(job_id, guia, data_exec, profissional_id, hora_inicial)` para idempotência de retry.
- `evolucao_claims` — reserva de candidatos (unique `fluxo+portal_item_id`).
- Carteirinha âncora `EVOLUCOES-CLMF` — exigência da fila (dispatcher lê
  `job.carteirinha_rel.carteirinha`); nenhum dado de paciente é criado.

## Arquivos tocados (tudo aditivo)
`clmf_scraper.py` (OP2), `server.py` (branch de rotina), `worker/Worker/models.py` (espelhos).
**Intocados**: `dispatcher.py`, `gui.py`, `ImportBaseGuias.py`, OP0/OP1.
