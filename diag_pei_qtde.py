# -*- coding: utf-8 -*-
"""Parte 0 — Diagnóstico readonly: migrations 0031/0035, codigo NULL, caso
76788780, qtde=0 em ativas, pacientes multi-carteirinhas."""
import psycopg2

conn = psycopg2.connect(host='aws-1-sa-east-1.pooler.supabase.com', port=6543, dbname='postgres',
    user='postgres.sutmrxreyfgsgofslrjj', password='KiM1JXGKyPAy1mVQ', sslmode='require')
conn.set_session(readonly=True, autocommit=True)
cur = conn.cursor()

def q(sql, label, limit=30):
    print('=' * 100)
    print(f'QUERY: {label}')
    cur.execute(sql)
    if cur.description is None:
        return
    print(' | '.join(d[0] for d in cur.description))
    rows = cur.fetchall()
    for r in rows[:limit]:
        print(' | '.join('' if v is None else str(v) for v in r))
    if len(rows) > limit:
        print(f'... ({len(rows) - limit} mais)')
    print(f'({len(rows)} rows)')

q("SELECT indexname FROM pg_indexes WHERE schemaname='public' AND indexname IN "
  "('uq_patient_pei_carteirinha_procedimento','uq_base_guias_cart_guia')", '0031/0035 unique indexes aplicadas?')
q("SELECT position('ON CONFLICT' in prosrc) > 0 AS tem_on_conflict, "
  "position('COALESCE(qtde_solicitada_pei' in prosrc) > 0 AS tem_coalesce_pei, "
  "position('NULLIF' in prosrc) > 0 AS tem_nullif "
  "FROM pg_proc WHERE proname='calculate_patient_pei'", 'Trigger atual: 0031+ aplicada?')
q("SELECT tgname FROM pg_trigger WHERE tgrelid='base_guias'::regclass AND NOT tgisinternal", 'Triggers em base_guias')

q("SELECT count(*) FILTER (WHERE codigo_procedimento IS NULL) AS nulos, "
  "count(*) FILTER (WHERE codigo_procedimento='') AS vazios, count(*) AS total "
  "FROM patient_pei", 'patient_pei codigo NULL/vazio')
q("SELECT count(*) FILTER (WHERE codigo_procedimento IS NULL) AS nulos, "
  "count(*) FILTER (WHERE codigo_procedimento='') AS vazios, count(*) AS total "
  "FROM base_guias", 'base_guias codigo NULL/vazio')
q("SELECT p.id, p.carteirinha_id, p.base_guia_id, p.pei_semanal, p.status, p.ativo, "
  "to_char(p.updated_at,'DD/MM HH24:MI') up FROM patient_pei p "
  "WHERE p.codigo_procedimento IS NULL OR p.codigo_procedimento='' ORDER BY p.id DESC LIMIT 15",
  'patient_pei NULL/vazio (amostra)')

q("SELECT bg.guia, c.paciente, c.carteirinha, bg.codigo_procedimento, bg.qtde_solicitada, "
  "bg.qtde_solicitada_pei, bg.sessoes_autorizadas, bg.validade, bg.ativo, "
  "bg.valida_prestador->>'Vinculo_prestador' AS vinculo, bg.valida_prestador->>'status_guia' AS st_guia, "
  "to_char(bg.updated_at,'DD/MM HH24:MI') up "
  "FROM base_guias bg JOIN carteirinhas c ON c.id=bg.carteirinha_id WHERE bg.guia='76788780'",
  'CASO: guia 76788780 completa')
q("SELECT job_id, level, to_char(created_at,'DD/MM HH24:MI') ts, message FROM logs "
  "WHERE message LIKE '%76788780%' ORDER BY created_at LIMIT 20", 'CASO: logs da guia 76788780')

q("SELECT count(*) FROM base_guias WHERE sessoes_autorizadas=0 AND ativo=true", 'Prevalência qtde=0 ativas')
q("SELECT coalesce(valida_prestador->>'Vinculo_prestador','(null)') vinculo, count(*) "
  "FROM base_guias WHERE sessoes_autorizadas=0 AND ativo=true GROUP BY 1 ORDER BY 2 DESC",
  'qtde=0 ativas por vínculo')
q("SELECT count(*) FROM base_guias WHERE sessoes_autorizadas=0 AND qtde_solicitada>0 AND ativo=true",
  'Assinatura bug scraping (só autorizada=0, solic>0)')

q("""SELECT c.id_paciente, max(c.paciente) AS paciente, count(DISTINCT c.id) AS n_cart,
       string_agg(DISTINCT c.carteirinha, ' / ' ORDER BY c.carteirinha) AS carteirinhas
    FROM carteirinhas c
    WHERE c.id_paciente IS NOT NULL AND c.status='ativo'
    GROUP BY c.id_paciente HAVING count(DISTINCT c.id) > 1
    ORDER BY n_cart DESC, c.id_paciente LIMIT 20""",
  'Pacientes com 2+ carteirinhas ATIVAS (insumo insight)')

conn.close()
print('\nOK')
