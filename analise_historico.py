# -*- coding: utf-8 -*-
"""Histórico: evolução diária do lote cron + casos timeout."""
import psycopg2
from collections import Counter

conn = psycopg2.connect(host='aws-1-sa-east-1.pooler.supabase.com', port=6543, dbname='postgres',
    user='postgres.sutmrxreyfgsgofslrjj', password='KiM1JXGKyPAy1mVQ', sslmode='require')
cur = conn.cursor()

# 1. Evolução diária (últimos 15 dias) do tipo_json nos jobs de cron (lotes noturnos ~23:01 UTC)
print("=== EVOLUÇÃO DIÁRIA tipo_json (jobs criados 22:50-23:10 UTC) ===")
cur.execute("""
    SELECT (created_at AT TIME ZONE 'America/Sao_Paulo')::date AS dia_local,
           COALESCE(valida_prestador->>'tipo_json','SEM_GUIAS') AS tipo, COUNT(*)
    FROM jobs
    WHERE created_at >= '2026-10-27 00:00:00+00'
      AND created_at::time BETWEEN '22:50:00' AND '23:10:00'
    GROUP BY 1,2 ORDER BY 1,2""")
dias = {}
for dia, tipo, n in cur.fetchall():
    dias.setdefault(str(dia), {})[tipo] = n
print(f"{'dia_local':12s} {'All Sucess':>10s} {'Thered':>7s} {'SEM_GUIAS':>9s} {'total':>6s} {'%sem':>6s}")
for dia in sorted(dias):
    d = dias[dia]
    tot = sum(d.values())
    print(f"{dia:12s} {d.get('All Sucess',0):>10d} {d.get('Thered',0):>7d} {d.get('SEM_GUIAS',0):>9d} {tot:>6d} {100*d.get('SEM_GUIAS',0)/tot:>5.1f}%")

# 2. Histórico dos 4 TIMEOUT — jobs anteriores das mesmas carteirinhas
print("\n=== HISTÓRICO RECENTE DOS CASOS TIMEOUT ===")
for jid in [186208, 186340, 186408, 186546]:
    cur.execute("""
        SELECT j2.id, j2.created_at::date, j2.status, COALESCE(j2.valida_prestador->>'tipo_json','SEM_GUIAS'),
               (SELECT COUNT(*) FROM logs l WHERE l.job_id=j2.id AND POSITION('older than limit' IN l.message) > 0) AS bateu_limite
        FROM jobs j JOIN jobs j2 ON j2.carteirinha_id = j.carteirinha_id
        WHERE j.id = %s AND j2.created_at >= '2026-10-28'
        ORDER BY j2.created_at""", (jid,))
    rows = cur.fetchall()
    cur.execute("SELECT paciente FROM carteirinhas c JOIN jobs j ON j.carteirinha_id=c.id WHERE j.id=%s", (jid,))
    pac = cur.fetchone()[0]
    print(f"\n-- job {jid} {pac}")
    for r in rows:
        print(f"   job2={r[0]} dia={r[1]} status={r[2]} tipo={r[3]} bateu_limite_270={bool(r[4])}")

# 3. Casos 'Found 3 rows' — logs completos
print("\n=== LOGS DOS CASOS 'Found 3 rows' (186396) ===")
cur.execute("""
    SELECT l.message FROM logs l WHERE l.job_id=186396 ORDER BY l.id""")
for r in cur.fetchall():
    print("  ", (r[0] or '')[:140])

# 4. Dos 91 LIMITE_270: quantas carteirinhas têm guia ATIVA na base agora?
print("\n=== LIMITE_270: guia ativa na base? ===")
cur.execute("""
    SELECT COUNT(DISTINCT j.carteirinha_id)
    FROM jobs j WHERE j.id = ANY(%s)""", ([[185922]],))
sem_ids = [185922,185939,185940,185949,185951,185960,185974,185986,186010,186020,186026,186027,186049,186059,186064,186065,186083,186091,186092,186099,186108,186110,186112,186118,186122,186139,186145,186161,186162,186163,186164,186188,186191,186215,186227,186243,186245,186251,186263,186267,186268,186276,186286,186308,186311,186317,186326,186339,186352,186360,186364,186366,186373,186388,186392,186397,186399,186400,186411,186420,186434,186440,186456,186457,186460,186464,186469,186472,186478,186482,186484,186488,186495,186496,186503,186506,186507,186529,186530,186533,186535,186555,186558,186560,186561,186574,186582,186585,186586,186589,186592,186604]
# nota: 186211, 186382, 186472 reclassificados p/ SO_STATUS_IGNORADO; 186396/552/566/672 p/ vazio; 4 timeout fora
cur.execute("""
    SELECT j.id,
           (SELECT COUNT(*) FROM base_guias g WHERE g.carteirinha_id=j.carteirinha_id) AS total_guias,
           (SELECT COUNT(*) FROM base_guias g WHERE g.carteirinha_id=j.carteirinha_id AND g.ativo) AS ativas,
           (SELECT COUNT(*) FROM base_guias g WHERE g.carteirinha_id=j.carteirinha_id AND g.validade >= '2026-11-10') AS validas_hoje,
           (SELECT MAX(g.validade) FROM base_guias g WHERE g.carteirinha_id=j.carteirinha_id) AS max_validade
    FROM jobs j WHERE j.id = ANY(%s) ORDER BY j.id""", (sem_ids,))
com_ativa = com_valida = sem_guia_nenhuma = 0
for jid, tot, ativas, validas_hoje, maxval in cur.fetchall():
    if tot == 0: sem_guia_nenhuma += 1
    if ativas: com_ativa += 1
    if validas_hoje: com_valida += 1
print(f"  carteirinhas c/ guia na base (qualquer estado): {len(sem_ids)-sem_guia_nenhuma}")
print(f"  ...com guia ainda VÁLIDA hoje (validade>=10/11): {com_valida}")
print(f"  ...sem nenhuma guia na base: {sem_guia_nenhuma}")

cur.close(); conn.close()
