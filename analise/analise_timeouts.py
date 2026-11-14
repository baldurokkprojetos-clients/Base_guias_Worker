# -*- coding: utf-8 -*-
"""Timeouts diários + prefixos das carteirinhas dos casos."""
import psycopg2
from collections import Counter

conn = psycopg2.connect(host='aws-1-sa-east-1.pooler.supabase.com', port=6543, dbname='postgres',
    user='postgres.sutmrxreyfgsgofslrjj', password='KiM1JXGKyPAy1mVQ', sslmode='require')
cur = conn.cursor()

# 1. Timeouts por dia (jobs criados ~23:01 UTC, últimos 15 dias)
print("=== TIMEOUTS 'Timeout waiting for results table' POR DIA (lote 22:50-23:10 UTC) ===")
cur.execute("""
    SELECT (j.created_at AT TIME ZONE 'America/Sao_Paulo')::date AS dia, COUNT(DISTINCT j.id)
    FROM jobs j JOIN logs l ON l.job_id = j.id
    WHERE j.created_at >= '2026-10-27'
      AND j.created_at::time BETWEEN '22:50:00' AND '23:10:00'
      AND POSITION('Timeout waiting for results table' IN l.message) > 0
    GROUP BY 1 ORDER BY 1""")
for r in cur.fetchall():
    print(f"  {r[0]}: {r[1]} jobs com timeout")

# 2. Prefixos (2º bloco) das carteirinhas dos 4 timeout vs geral do lote
print("\n=== PREFIXO CARTEIRINHA ===")
print("Timeouts:")
for jid in [186208, 186340, 186408, 186546]:
    cur.execute("""SELECT c.carteirinha FROM jobs j JOIN carteirinhas c ON c.id=j.carteirinha_id WHERE j.id=%s""", (jid,))
    print(f"  job {jid}: {cur.fetchone()[0]}")

print("\nPrefixos do lote 10/11 23:01 (todos 795):")
cur.execute("""
    SELECT SUBSTRING(c.carteirinha FROM 6 FOR 4) AS prefixo,
           COALESCE(j.valida_prestador->>'tipo_json','SEM_GUIAS') AS tipo, COUNT(*)
    FROM jobs j JOIN carteirinhas c ON c.id=j.carteirinha_id
    WHERE j.created_at >= '2026-11-10 23:01:00+00' AND j.created_at < '2026-11-10 23:02:00+00'
    GROUP BY 1,2 ORDER BY 1,2""")
pref = {}
for p, tipo, n in cur.fetchall():
    pref.setdefault(p, {})[tipo] = n
for p in sorted(pref):
    d = pref[p]
    print(f"  {p}: AllSucess={d.get('All Sucess',0)} Thered={d.get('Thered',0)} SEM={d.get('SEM_GUIAS',0)}")

# 3. Histórico do job 185541 (MARIA ISIS 11-09) — confirmar timeout no dia da virada
print("\n=== LOGS job 185541 (MARIA ISIS, lote 09/11) ===")
cur.execute("SELECT l.message FROM logs l WHERE l.job_id=185541 ORDER BY l.id")
for r in cur.fetchall():
    m = (r[0] or '')
    if any(k in m for k in ['Timeout', 'rows', 'older', 'valida_prestador', 'Negado', 'Error']):
        print("  ", m[:140].replace('\n',' | '))

cur.close(); conn.close()
