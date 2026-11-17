# -*- coding: utf-8 -*-
"""Relatório READ-ONLY — PEIs fantasmas (classe do caso 76788780/Enzo).

Classes:
  A  código divergente  — PEI com código X apontando para guia que hoje tem código Y
  B  guia bloqueada     — PEI ativo cujo vínculo atual != 'Guia Válida'
  C  guia inativa       — PEI ativo em guia inativa
  D  par sem guia       — PEI ativo cujo par (cart, código) não tem mais nenhuma guia válida
Forense 76788780: PEIs/guias do Enzo, jobs/tipo_json desde 03/11, logs por nr e
CD_GUIA interno 71028971, janela de retenção de logs.
"""
import psycopg2

conn = psycopg2.connect(host='aws-1-sa-east-1.pooler.supabase.com', port=6543, dbname='postgres',
    user='postgres.sutmrxreyfgsgofslrjj', password='KiM1JXGKyPAy1mVQ', sslmode='require')
conn.set_session(readonly=True, autocommit=True)
cur = conn.cursor()

def q(sql, label, lim=60):
    print('=' * 110)
    print(f'### {label}')
    cur.execute(sql)
    if cur.description is None:
        return []
    cols = [d[0] for d in cur.description]
    print(' | '.join(cols))
    rows = cur.fetchall()
    for r in rows[:lim]:
        print(' | '.join('' if v is None else str(v) for v in r))
    if len(rows) > lim:
        print(f'... (+{len(rows) - lim} linhas)')
    print(f'({len(rows)} rows)')
    return rows

# ── CLASSE A: código divergente (caso Enzo) ─────────────────────────────────
q("""SELECT c.paciente, c.carteirinha, pp.codigo_procedimento AS cod_pei,
       bg.codigo_procedimento AS cod_guia_hoje, bg.guia,
       coalesce(bg.valida_prestador->>'Vinculo_prestador','-') AS vinculo,
       bg.qtde_solicitada_pei AS qtde_congel, bg.qtde_solicitada AS qtde_hoje,
       pp.pei_semanal, pp.status, pp.ativo,
       to_char(pp.updated_at,'DD/MM HH24:MI') AS pei_upd
    FROM patient_pei pp JOIN base_guias bg ON bg.id=pp.base_guia_id
    JOIN carteirinhas c ON c.id=pp.carteirinha_id
    WHERE bg.codigo_procedimento IS DISTINCT FROM pp.codigo_procedimento
    ORDER BY pp.ativo DESC, c.paciente""",
  'CLASSE A — código divergente (ativos primeiro)')

q("""SELECT pp.codigo_procedimento AS cod_pei, bg.codigo_procedimento AS cod_guia_hoje,
       count(*) AS casos, count(*) FILTER (WHERE pp.ativo) AS ativos
    FROM patient_pei pp JOIN base_guias bg ON bg.id=pp.base_guia_id
    WHERE bg.codigo_procedimento IS DISTINCT FROM pp.codigo_procedimento
    GROUP BY 1,2 ORDER BY casos DESC""",
  'CLASSE A — distribuição por par de códigos (antigo → atual)')

# ── CLASSE B: PEI ativo em guia bloqueada ───────────────────────────────────
q("""SELECT c.paciente, c.carteirinha, pp.codigo_procedimento, bg.guia,
       coalesce(bg.valida_prestador->>'Vinculo_prestador','-') AS vinculo,
       pp.pei_semanal, pp.status
    FROM patient_pei pp JOIN base_guias bg ON bg.id=pp.base_guia_id
    JOIN carteirinhas c ON c.id=pp.carteirinha_id
    WHERE pp.ativo AND coalesce(bg.valida_prestador->>'Vinculo_prestador','') NOT LIKE 'Guia V%lida'
    ORDER BY c.paciente LIMIT 40""",
  'CLASSE B — PEI ativo cuja guia vinculada está BLOQUEADA')

# ── CLASSE C: PEI ativo em guia inativa ─────────────────────────────────────
q("""SELECT count(*) AS total FROM patient_pei pp JOIN base_guias bg ON bg.id=pp.base_guia_id
    WHERE pp.ativo AND NOT bg.ativo""",
  'CLASSE C — PEI ativo em guia inativa (contagem)')

# ── CLASSE D: par sem nenhuma guia válida/ativa com aquele código ────────────
q("""SELECT c.paciente, c.carteirinha, pp.codigo_procedimento AS cod_sem_guia,
       bg.guia AS ultima_guia_vinculada, pp.pei_semanal, pp.status, pp.ativo,
       to_char(pp.updated_at,'DD/MM HH24:MI') AS pei_upd
    FROM patient_pei pp JOIN base_guias bg ON bg.id=pp.base_guia_id
    JOIN carteirinhas c ON c.id=pp.carteirinha_id
    WHERE pp.ativo AND NOT EXISTS (
        SELECT 1 FROM base_guias b2
        WHERE b2.carteirinha_id = pp.carteirinha_id
          AND b2.codigo_procedimento = pp.codigo_procedimento
          AND b2.ativo AND b2.valida_prestador->>'Vinculo_prestador' LIKE 'Guia V%lida')
    ORDER BY c.paciente""",
  'CLASSE D — PEI ativo cujo par não tem mais guia válida/ativa (backfill inativará)')

# ── FORENSE 76788780 / ENZO ──────────────────────────────────────────────────
q("""SELECT bg.guia, bg.codigo_procedimento, bg.qtde_solicitada, bg.qtde_solicitada_pei,
       bg.sessoes_autorizadas, to_char(bg.data_autorizacao,'DD/MM/YYYY') aut,
       coalesce(bg.valida_prestador->>'Vinculo_prestador','-') vinculo, bg.ativo,
       to_char(bg.created_at,'DD/MM HH24:MI') criada, to_char(bg.updated_at,'DD/MM HH24:MI') upd
    FROM base_guias bg WHERE bg.carteirinha_id =
      (SELECT id FROM carteirinhas WHERE carteirinha='0064.0317.011904.67-8')
    ORDER BY bg.data_autorizacao DESC, bg.id DESC""",
  'ENZO — todas as guias da carteirinha (criada/upd mostram a história)')

q("""SELECT pp.codigo_procedimento, bg.guia, bg.codigo_procedimento AS cod_guia_hoje,
       pp.pei_semanal, pp.status, pp.ativo,
       to_char(pp.validade,'DD/MM/YYYY') validade, to_char(pp.updated_at,'DD/MM HH24:MI') pei_upd
    FROM patient_pei pp LEFT JOIN base_guias bg ON bg.id=pp.base_guia_id
    WHERE pp.carteirinha_id = (SELECT id FROM carteirinhas WHERE carteirinha='0064.0317.011904.67-8')
    ORDER BY pp.codigo_procedimento""",
  'ENZO — PEIs (updated = quando nasceu/foi tocado pela última vez)')

q("""SELECT j.id, to_char(j.created_at,'DD/MM') dia, j.status,
       coalesce(j.valida_prestador->>'tipo_json','-') tipo
    FROM jobs j WHERE j.carteirinha_id =
      (SELECT id FROM carteirinhas WHERE carteirinha='0064.0317.011904.67-8')
      AND j.created_at >= '2026-11-03'
    ORDER BY j.created_at""",
  'ENZO — jobs desde 03/11 (All Sucess→Thered marca quando o bloqueio começou)')

q("""SELECT job_id, level, to_char(created_at,'DD/MM HH24:MI') ts, left(message,150) msg
    FROM logs WHERE message LIKE '%71028971%' OR message LIKE '%Scraped Guia 76788780%'
       OR message LIKE '%Layer 2 check for Guia 76788780%'
    ORDER BY created_at LIMIT 15""",
  'LOGS — guia 76788780 por nr e CD_GUIA interno (insert original se existir)')

q("SELECT to_char(min(created_at),'DD/MM/YYYY') mais_antigo, to_char(max(created_at),'DD/MM/YYYY') mais_recente, count(*) FROM logs",
  'RETENÇÃO DE LOGS — janela existente')

conn.close()
print('\nOK')
