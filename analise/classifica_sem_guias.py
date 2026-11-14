# -*- coding: utf-8 -*-
"""Classifica os 102 jobs 'sem guias' do lote 10/11 23:01 UTC por causa raiz."""
import re
import psycopg2
from collections import Counter

conn = psycopg2.connect(host='aws-1-sa-east-1.pooler.supabase.com', port=6543, dbname='postgres',
    user='postgres.sutmrxreyfgsgofslrjj', password='KiM1JXGKyPAy1mVQ', sslmode='require')
cur = conn.cursor()

sem = [185922,185939,185940,185949,185951,185960,185974,185986,186010,186020,186026,186027,186049,186059,186064,186065,186083,186091,186092,186099,186108,186110,186112,186118,186122,186139,186145,186161,186162,186163,186164,186188,186191,186208,186211,186215,186227,186243,186245,186251,186263,186267,186268,186276,186286,186308,186311,186317,186326,186339,186340,186352,186360,186364,186366,186373,186382,186388,186392,186396,186397,186399,186400,186408,186411,186420,186434,186440,186456,186457,186460,186464,186469,186472,186478,186482,186484,186488,186495,186496,186503,186506,186507,186529,186530,186533,186535,186546,186552,186555,186558,186560,186561,186566,186574,186582,186585,186586,186589,186592,186604,186672]

cur.execute("""
    SELECT l.job_id, l.message, c.paciente
    FROM logs l JOIN jobs j ON j.id=l.job_id JOIN carteirinhas c ON c.id=l.carteirinha_id
    WHERE l.job_id = ANY(%s) ORDER BY l.job_id, l.id""", (sem,))
rows = cur.fetchall()

por_job = {}
for jid, msg, pac in rows:
    por_job.setdefault(jid, {'paciente': pac, 'msgs': []})['msgs'].append(msg or '')

cat = Counter()
detalhe = {'LIMITE_270': [], 'TIMEOUT_TABELA': [], 'SEM_TABELA_VAZIA': [], 'SO_STATUS_IGNORADO': [], 'OUTRO': []}
datas_limite = []
for jid, info in por_job.items():
    msgs = info['msgs']
    txt = ' || '.join(msgs)
    m270 = re.findall(r"Guia date (\d{2}/\d{2}/\d{4}) is older than limit", txt)
    negados = len(re.findall(r"status 'Negado' ignorado", txt))
    outros_ign = re.findall(r"com status '([^']+)' ignorado", txt)
    ign_counter = Counter(outros_ign)
    ign_counter.pop('Negado', None)
    timeout = "Timeout waiting for results table" in txt
    found_rows = re.findall(r"Found (\d+) rows", txt)

    if timeout:
        cat['TIMEOUT_TABELA'] += 1
        detalhe['TIMEOUT_TABELA'].append((jid, info['paciente']))
    elif m270:
        cat['LIMITE_270'] += 1
        detalhe['LIMITE_270'].append((jid, info['paciente'], m270[0], negados, dict(ign_counter)))
        datas_limite.append(m270[0])
    elif outros_ign and not m270:
        cat['SO_STATUS_IGNORADO'] += 1
        detalhe['SO_STATUS_IGNORADO'].append((jid, info['paciente'], dict(ign_counter)))
    elif found_rows:
        cat['SEM_TABELA_VAZIA'] += 1
        detalhe['SEM_TABELA_VAZIA'].append((jid, info['paciente'], found_rows))
    else:
        cat['OUTRO'] += 1
        detalhe['OUTRO'].append((jid, info['paciente'], txt[-200:]))

print("=== CLASSIFICAÇÃO DOS 102 JOBS 'SEM GUIAS' ===")
for k, v in cat.most_common():
    print(f"  {k}: {v}")
print(f"\nTotal classificado: {sum(cat.values())}")

print("\n--- LIMITE_270 (tinha guias, filtro de data parou) ---")
for d in detalhe['LIMITE_270']:
    print(f"  job {d[0]} {d[1][:35]:35s} 1a_guia_apos_corte={d[2]} negados={d[3]} outros_ignorados={d[4]}")
print("\nDatas de corte encontradas:", Counter(datas_limite))

print("\n--- TIMEOUT_TABELA ---")
for d in detalhe['TIMEOUT_TABELA']:
    print(f"  job {d[0]} {d[1]}")

print("\n--- SO_STATUS_IGNORADO ---")
for d in detalhe['SO_STATUS_IGNORADO']:
    print(f"  job {d[0]} {d[1]} {d[2]}")

print("\n--- SEM_TABELA_VAZIA (tabela com 0 linhas = realmente sem guia) ---")
for d in detalhe['SEM_TABELA_VAZIA']:
    print(f"  job {d[0]} {d[1][:35]:35s} rows={d[2]}")

print("\n--- OUTRO ---")
for d in detalhe['OUTRO']:
    print(f"  job {d[0]} {d[1]}: {d[2][:150]}")

cur.close(); conn.close()
