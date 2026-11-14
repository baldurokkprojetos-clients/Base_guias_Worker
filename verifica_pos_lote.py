# -*- coding: utf-8 -*-
"""Verificação pós-lote da correção R1 (dois marcos) — QA gate §5.3.

Compara os lotes noturnos (23:01 UTC) de 09/11..12/11: distribuição de
tipo_json, reclassificação dos 102 'sem guias' de 10/11, timeouts (error+retry),
renovação de base_guias e presença dos logs estruturados novos.
"""
import psycopg2
from collections import Counter

conn = psycopg2.connect(host='aws-1-sa-east-1.pooler.supabase.com', port=6543, dbname='postgres',
    user='postgres.sutmrxreyfgsgofslrjj', password='KiM1JXGKyPAy1mVQ', sslmode='require')
cur = conn.cursor()

# ── 1. Distribuição diária (pré vs pós deploy) ─────────────────────────────
print("=" * 78)
print("1. DISTRIBUIÇÃO tipo_json POR LOTE (22:50-23:10 UTC)")
print("=" * 78)
cur.execute("""
    SELECT (created_at AT TIME ZONE 'America/Sao_Paulo')::date AS dia,
           COALESCE(valida_prestador->>'tipo_json', '(null)') AS tipo, COUNT(*)
    FROM jobs
    WHERE created_at >= '2026-11-08 00:00:00+00'
      AND created_at::time BETWEEN '22:50:00' AND '23:10:00'
    GROUP BY 1, 2 ORDER BY 1, 2""")
dias = {}
for dia, tipo, n in cur.fetchall():
    dias.setdefault(str(dia), Counter())[tipo] = n
tipos_todos = sorted({t for c in dias.values() for t in c})
header = f"{'tipo':<14}" + "".join(f"{d[-5:]:>10}" for d in sorted(dias))
print(header)
for t in tipos_todos:
    print(f"{t:<14}" + "".join(f"{dias[d].get(t, 0):>10}" for d in sorted(dias)))
print(f"{'TOTAL':<14}" + "".join(f"{sum(dias[d].values()):>10}" for d in sorted(dias)))

# ── 2. Reclassificação dos 102 'sem guias' do lote 10/11 ────────────────────
print()
print("=" * 78)
print("2. RECLASSIFICAÇÃO DOS 102 'SEM GUIAS' DO LOTE 10/11 (no lote de 12/11)")
print("=" * 78)
cur.execute("""
    WITH alvo AS (
        SELECT DISTINCT j.carteirinha_id, c.paciente
        FROM jobs j JOIN carteirinhas c ON c.id=j.carteirinha_id
        WHERE j.created_at >= '2026-11-10 23:01:00+00' AND j.created_at < '2026-11-10 23:02:00+00'
          AND COALESCE(j.valida_prestador->>'tipo_json','') IN ('', 'Null')
          AND j.valida_prestador->>'tipo_json' IS NULL OR (
              j.created_at >= '2026-11-10 23:01:00+00' AND j.created_at < '2026-11-10 23:02:00+00'
              AND j.valida_prestador->>'tipo_json' IS NULL)
    )
    SELECT COALESCE(j2.valida_prestador->>'tipo_json','(null)') AS novo_tipo,
           j2.status, COUNT(*)
    FROM alvo
    JOIN jobs j2 ON j2.carteirinha_id = alvo.carteirinha_id
                AND j2.created_at >= '2026-11-12 22:50:00+00'
                AND j2.created_at < '2026-11-12 23:30:00+00'
    GROUP BY 1, 2 ORDER BY 3 DESC""")
rows = cur.fetchall()
if not rows:
    print("(lote de 12/11 ainda não rodou — usar 11/11)")
    cur.execute("""
        WITH alvo AS (
            SELECT DISTINCT j.carteirinha_id
            FROM jobs j
            WHERE j.created_at >= '2026-11-10 23:01:00+00' AND j.created_at < '2026-11-10 23:02:00+00'
              AND (j.valida_prestador IS NULL OR j.valida_prestador->>'tipo_json' IS NULL))
        SELECT COALESCE(j2.valida_prestador->>'tipo_json','(null)'), j2.status, COUNT(*)
        FROM alvo JOIN jobs j2 ON j2.carteirinha_id = alvo.carteirinha_id
                  AND j2.created_at >= '2026-11-11 22:50:00+00'
                  AND j2.created_at < '2026-11-11 23:30:00+00'
        GROUP BY 1,2 ORDER BY 3 DESC""")
    rows = cur.fetchall()
total_alvo = sum(r[2] for r in rows)
for tipo, status, n in rows:
    print(f"  {tipo:<12} job={status:<8} {n:>4}  ({100*n/max(total_alvo,1):.1f}%)")
print(f"  (pacientes do lote 10/11 com job no lote seguinte: {total_alvo})")

# ── 3. Timeouts: error + retry? ─────────────────────────────────────────────
print()
print("=" * 78)
print("3. CASOS TIMEOUT (ARTHUR LIMA 186546, MARIA ISIS 186340, PEDRO LUCAS 186408, ISRAEL 186208)")
print("=" * 78)
for jid in [186546, 186340, 186408, 186208]:
    cur.execute("""
        SELECT j2.id, to_char(j2.created_at,'DD/MM HH24:MI'), j2.status, j2.attempts,
               COALESCE(j2.valida_prestador->>'tipo_json','-')
        FROM jobs j JOIN jobs j2 ON j2.carteirinha_id=j.carteirinha_id
        WHERE j.id=%s AND j2.created_at >= '2026-11-11 00:00:00+00'
        ORDER BY j2.created_at""", (jid,))
    cur.execute("SELECT c.paciente FROM jobs j JOIN carteirinhas c ON c.id=j.carteirinha_id WHERE j.id=%s", (jid,))
    pac = cur.fetchone()[0]
    print(f"\n-- {pac}")
    cur.execute("""
        SELECT j2.id, to_char(j2.created_at,'DD/MM HH24:MI'), j2.status, j2.attempts,
               COALESCE(j2.valida_prestador->>'tipo_json','-')
        FROM jobs j JOIN jobs j2 ON j2.carteirinha_id=j.carteirinha_id
        WHERE j.id=%s AND j2.created_at >= '2026-11-11 00:00:00+00'
        ORDER BY j2.created_at""", (jid,))
    for r in cur.fetchall():
        print(f"   job={r[0]} {r[1]} status={r[2]} attempts={r[3]} tipo={r[4]}")

# ── 4. Logs novos: diagnóstico de timeout + acao estruturada ────────────────
print()
print("=" * 78)
print("4. LOGS NOVOS (últimos 2 dias)")
print("=" * 78)
cur.execute("""
    SELECT COUNT(*) FROM logs l
    WHERE l.created_at >= '2026-11-11 00:00:00+00'
      AND POSITION('acao=antiga270' IN l.message) > 0""")
print(f"  logs acao=antiga270        : {cur.fetchone()[0]}")
cur.execute("""
    SELECT COUNT(*) FROM logs l
    WHERE l.created_at >= '2026-11-11 00:00:00+00'
      AND POSITION('acao=vencida' IN l.message) > 0""")
print(f"  logs acao=vencida          : {cur.fetchone()[0]}")
cur.execute("""
    SELECT COUNT(*) FROM logs l
    WHERE l.created_at >= '2026-11-11 00:00:00+00'
      AND POSITION('acao=coletada' IN l.message) > 0""")
print(f"  logs acao=coletada         : {cur.fetchone()[0]}")
cur.execute("""
    SELECT COUNT(*) FROM logs l
    WHERE l.created_at >= '2026-11-11 00:00:00+00'
      AND POSITION('Resultado guias: tipo=' IN l.message) > 0""")
print(f"  logs resumo por job        : {cur.fetchone()[0]}")
cur.execute("""
    SELECT l.job_id, l.message FROM logs l
    WHERE l.created_at >= '2026-11-11 00:00:00+00'
      AND POSITION('Timeout waiting for results table. url=' IN l.message) > 0
    ORDER BY l.created_at DESC LIMIT 3""")
to_logs = cur.fetchall()
print(f"  timeouts c/ diagnóstico    : {len(to_logs)} (mostrando até 3)")
for jid, msg in to_logs:
    print(f"    job {jid}: {msg[:220]}")

# ── 5. Gate PEI/base: guias dos pilotos ─────────────────────────────────────
print()
print("=" * 78)
print("5. GATE BASE/PEI — PILOTOS (guias ativas vs inativas hoje)")
print("=" * 78)
pilotos = [(186574, 'MARIA LUISA (antigas)'), (186162, 'HEITOR MENDES (antigas)'),
           (186457, 'SAMUEL NUNES (antigas)'), (186339, 'MARIA FERNANDA (sem base)'),
           (186533, 'GAEL (antigas)'), (186589, 'SAMUEL HENRIQUE (antigas)')]
for jid, rotulo in pilotos:
    cur.execute("""
        SELECT c.paciente,
               count(*) FILTER (WHERE g.ativo) AS ativas,
               count(*) FILTER (WHERE NOT g.ativo) AS inativas,
               max(g.updated_at)::date AS ultima_atualizacao
        FROM jobs j JOIN carteirinhas c ON c.id=j.carteirinha_id
        LEFT JOIN base_guias g ON g.carteirinha_id=c.id
        WHERE j.id=%s GROUP BY c.paciente""", (jid,))
    pac, at, ina, upd = cur.fetchone()
    print(f"  {pac[:38]:<38} ativas={at or 0} inativas={ina or 0} upd={upd}")

# guias 'Vencida' registradas no frame (exibição) em jobs novos
cur.execute("""
    SELECT COUNT(*) FROM jobs j, jsonb_each(j.valida_prestador->'guias') g
    WHERE j.created_at >= '2026-11-11 22:50:00+00'
      AND g.value->>'status_guia' = 'Vencida'""")
print(f"\n  guias 'Vencida' (só exibição) nos lotes novos: {cur.fetchone()[0]}")
cur.execute("""
    SELECT COUNT(*) FROM jobs j, jsonb_each(j.valida_prestador->'guias') g
    WHERE j.created_at >= '2026-11-11 22:50:00+00'
      AND g.value->>'status_guia' = 'Antiga270'""")
print(f"  guias 'Antiga270' (só exibição) nos lotes novos: {cur.fetchone()[0]}")

# ── 6. Sanidade geral: erros nos lotes novos ────────────────────────────────
print()
print("=" * 78)
print("6. ERROS NOS LOTES NOVOS (11/11 e 12/12 23:01 UTC)")
print("=" * 78)
cur.execute("""
    SELECT (created_at AT TIME ZONE 'America/Sao_Paulo')::date, status, COUNT(*)
    FROM jobs
    WHERE created_at >= '2026-11-11 22:50:00+00' AND created_at::time BETWEEN '22:50:00' AND '23:10:00'
    GROUP BY 1,2 ORDER BY 1,2""")
for r in cur.fetchall():
    print(f"  {r[0]} status={r[1]}: {r[2]}")
cur.execute("""
    SELECT j.id, j.attempts, left(l.message, 130)
    FROM jobs j JOIN logs l ON l.job_id=j.id
    WHERE j.created_at >= '2026-11-11 22:50:00+00'
      AND j.created_at::time BETWEEN '22:50:00' AND '23:10:00'
      AND l.level='ERROR' AND j.status='error'
    ORDER BY j.id LIMIT 10""")
errs = cur.fetchall()
print(f"  jobs error com log ERROR: {len(errs)} (até 10)")
for e in errs:
    print(f"    job={e[0]} attempts={e[1]}: {e[2]}")

cur.close(); conn.close()
print("\nOK")
