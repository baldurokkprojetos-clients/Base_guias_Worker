# -*- coding: utf-8 -*-
"""Extrai logs dos jobs 'sem guias' do lote 10/11 23:01 UTC."""
import sys
import psycopg2

conn = psycopg2.connect(host='aws-1-sa-east-1.pooler.supabase.com', port=6543, dbname='postgres',
    user='postgres.sutmrxreyfgsgofslrjj', password='KiM1JXGKyPAy1mVQ', sslmode='require')
cur = conn.cursor()

# casos com guias na base + amostra de casos sem nenhuma guia
com_guias = [186064, 186162, 186191, 186208, 186340, 186408, 186440, 186457, 186464,
             186533, 186546, 186560, 186574, 186589, 186604]
sem_nenhuma = [185922, 185939, 185951, 186010, 186027, 186091, 186122, 186227, 186245,
               186326, 186339, 186352, 186364, 186366, 186411]

grupos = [("COM GUIAS NA BASE (suspeitos)", com_guias), ("SEM NENHUMA GUIA NA BASE (amostra)", sem_nenhuma)]
for titulo, ids in grupos:
    print(f"\n{'='*100}\n### {titulo}\n{'='*100}")
    for jid in ids:
        cur.execute("""
            SELECT c.paciente, l.created_at, l.level, l.message
            FROM jobs j JOIN carteirinhas c ON c.id=j.carteirinha_id
            LEFT JOIN logs l ON l.job_id = j.id
            WHERE j.id = %s ORDER BY l.created_at NULLS FIRST, l.id""", (jid,))
        rows = cur.fetchall()
        paciente = rows[0][0] if rows else '?'
        print(f"\n----- job {jid} | {paciente} | {len(rows)-1 if rows[0][1] is None else len(rows)} logs -----")
        for r in rows:
            if r[1] is None:
                continue
            msg = (r[3] or '').replace('\n', ' | ')[:180]
            print(f"  [{r[2]}] {msg}")

cur.close(); conn.close()
