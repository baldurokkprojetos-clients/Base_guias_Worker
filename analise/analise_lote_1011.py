# -*- coding: utf-8 -*-
"""Análise do lote cron 10/11/2026 23:01 UTC — jobs 'sem guias'."""
import psycopg2

conn = psycopg2.connect(host='aws-1-sa-east-1.pooler.supabase.com', port=6543, dbname='postgres',
    user='postgres.sutmrxreyfgsgofslrjj', password='KiM1JXGKyPAy1mVQ', sslmode='require')
cur = conn.cursor()

# 1. Lote completo
cur.execute("""
    SELECT j.id, j.status, j.valida_prestador->>'tipo_json', c.paciente,
           EXTRACT(EPOCH FROM (j.updated_at - j.created_at))::int AS dur_s
    FROM jobs j JOIN carteirinhas c ON c.id=j.carteirinha_id
    WHERE j.created_at >= '2026-11-10 23:01:00+00' AND j.created_at < '2026-11-10 23:02:00+00'
    ORDER BY j.id""")
lote = cur.fetchall()
from collections import Counter
tipos = Counter((r[1], r[2] or 'SEM_GUIAS') for r in lote)
print(f"=== LOTE 23:01 UTC: {len(lote)} jobs ===")
for k, v in sorted(tipos.items()):
    print(f"  status={k[0]} tipo={k[1]}: {v}")

sem = [r[0] for r in lote if not r[2]]
print(f"\njobs sem_guias no lote ({len(sem)}): {sem}")

# 2. base_guias histórica das carteirinhas dos jobs 'sem guias'
print("\n=== BASE_GUIAS HISTÓRICA (carteirinhas dos jobs sem_guias) ===")
cur.execute("""
    SELECT j.id, c.paciente, g.guia, g.validade, g.ativo,
           g.valida_prestador->>'Vinculo_prestador' AS vinculo,
           g.updated_at::date
    FROM jobs j
    JOIN carteirinhas c ON c.id=j.carteirinha_id
    LEFT JOIN base_guias g ON g.carteirinha_id=c.id
    WHERE j.id = ANY(%s)
    ORDER BY j.id, g.guia NULLS LAST""", (sem,))
cur_rows = cur.fetchall()
from itertools import groupby
for jid, group in groupby(cur_rows, key=lambda r: r[0]):
    g_list = list(group)
    print(f"\n-- job {jid} | {g_list[0][1]} | {len(g_list)} guias na base")
    for r in g_list:
        if r[2] is None:
            print(f"   (nenhuma guia na base)")
        else:
            print(f"   guia={r[2]} validade={r[3]} ativo={r[4]} vinculo={r[5]} upd={r[6]}")

cur.close(); conn.close()
