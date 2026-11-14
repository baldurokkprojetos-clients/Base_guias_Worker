"""Read-only: full valida_prestador of jobs 162421/162419 + history of guias 75063971/75063970/75063972/75062313."""
import os, json
from dotenv import load_dotenv

load_dotenv(os.path.join(os.path.dirname(__file__), '.env'))
load_dotenv(os.path.join(os.path.dirname(__file__), '..', 'backend', '.env'))

DB_USER = os.getenv("SUPABASE_DB_USER", "postgres")
DB_PASSWORD = os.getenv("SUPABASE_PASSWORD", "")
DB_HOST = os.getenv("SUPABASE_DB_HOST", "")
DB_PORT = os.getenv("SUPABASE_DB_PORT", "5432")
DB_NAME = os.getenv("SUPABASE_DB_NAME", "postgres")

import psycopg2
import psycopg2.extras

conn = psycopg2.connect(
    host=DB_HOST, port=DB_PORT, user=DB_USER, password=DB_PASSWORD, dbname=DB_NAME,
    connect_timeout=15
)
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

print("### FULL valida_prestador JSON logs (job 162421) ###")
cur.execute(
    "SELECT message FROM logs WHERE job_id=162421 AND message LIKE 'valida_prestador JSON%' ORDER BY id DESC LIMIT 1"
)
row = cur.fetchone()
if row:
    print(row['message'])

print()
print("### Full log message of Layer2/valida for guias 7506397x (any job, last 7 days) ###")
cur.execute("""
    SELECT job_id, created_at, message FROM logs
    WHERE message LIKE '%75063971%' AND created_at > now() - interval '7 days'
    ORDER BY id ASC LIMIT 30
""")
for r in cur.fetchall():
    print(f"[job {r['job_id']} {r['created_at']}] {r['message'][:500]}")

print()
print("### BaseGuias rows for carteirinha of these jobs (via job logs) ###")
cur.execute("""
    SELECT c.carteirinha FROM jobs j JOIN carteirinhas c ON c.id = j.carteirinha_id
    WHERE j.id = 162421
""")
cart = cur.fetchone()['carteirinha']
print("carteirinha:", cart)
cur.execute("""
    SELECT c.id FROM carteirinhas c WHERE c.carteirinha = %s ORDER BY c.id
""", (cart,))
for r in cur.fetchall():
    cid = r['id']
    cur.execute("""
        SELECT guia, codigo_procedimento, qtde_solicitada, sessoes_autorizadas, valida_prestador
        FROM base_guias WHERE carteirinha_id = %s ORDER BY guia
    """, (cid,))
    rows = cur.fetchall()
    print(f"carteirinha_id={cid}: {len(rows)} guias in base_guias")
    for g in rows:
        vp = g['valida_prestador']
        vp_s = json.dumps(vp, ensure_ascii=False)[:200] if vp else None
        print(f"  guia={g['guia']} proc={g['codigo_procedimento']} qtd_sol={g['qtde_solicitada']} sess_aut={g['sessoes_autorizadas']} valida={vp_s}")

print()
print("### Jobs for this carteirinha in last 7 days ###")
cur.execute("""
    SELECT j.id, j.status, j.created_at FROM jobs j JOIN carteirinhas c ON c.id = j.carteirinha_id
    WHERE c.carteirinha = %s AND j.created_at > now() - interval '7 days'
    ORDER BY j.id ASC
""", (cart,))
for r in cur.fetchall():
    print(dict(r))

conn.close()
