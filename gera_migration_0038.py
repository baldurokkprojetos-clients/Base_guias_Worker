# -*- coding: utf-8 -*-
"""Gera backend/migrations/0038_pei_codigo_null_qtde_zero.sql a partir do
prosrc REAL do banco (paridade garantida com o trigger vigente)."""
import psycopg2

conn = psycopg2.connect(host='aws-1-sa-east-1.pooler.supabase.com', port=6543, dbname='postgres',
    user='postgres.sutmrxreyfgsgofslrjj', password='KiM1JXGKyPAy1mVQ', sslmode='require')
cur = conn.cursor()
cur.execute("SELECT prosrc FROM pg_proc WHERE proname='calculate_patient_pei'")
body = cur.fetchone()[0]
conn.close()

ANCHOR = "    -- 2. Find Latest VALID and ACTIVE Guia for this Context"
assert ANCHOR in body, "anchor não encontrado no prosrc real"

GUARD = """    -- 0038: sem codigo_procedimento (NULL/vazio) nao ha PEI — guia bloqueada
    -- sem hint entra com codigo vazio e nao pode criar linha fantasma.
    -- (UNIQUE usa NULLS DISTINCT: com NULL o ON CONFLICT nunca conflitaria e
    -- cada update criaria linha nova.)
    IF target_codigo_procedimento IS NULL OR btrim(target_codigo_procedimento) = '' THEN
        RETURN NEW;
    END IF;

"""
body_new = body.replace(ANCHOR, GUARD + ANCHOR, 1)
assert body_new != body

sql = f"""-- Migration 0038: PEI sem codigo fantasma + nao congelar qtde zero
--
-- Diagnostico 14/11 (Parte 0): zero codigo NULL/vazio em patient_pei e
-- base_guias, e zero assinatura de bug de parsing (aut=0 com solic>0) —
-- as 490 guias ativas com qtde=0 sao bloqueadas (por design). Esta migration
-- e DEFENSIVA: trava as duas causas de divergencia apontadas pela analise.
--
-- 1) calculate_patient_pei: guard de codigo NULL/vazio (com NULLS DISTINCT
--    da UNIQUE, o ON CONFLICT nunca conflitaria e cada update criaria linha
--    nova para o mesmo par "nulo").
-- 2) freeze_guia_pei_base: NULLIF(qtde_solicitada, 0) — nao congela zero.
--    Guia Autorizada sempre traz qtde >0; se a leitura falhar (0) e o scrape
--    seguinte trouxer o valor real, o COALESCE do calculo passa a usa-lo.
-- 3) Backfill: inativa patient_pei sem codigo e descongela qtde_solicitada_pei
--    congelada em 0 (volta a seguir a qtde corrente da guia).

CREATE OR REPLACE FUNCTION calculate_patient_pei() RETURNS TRIGGER AS $fn$
{body_new}
$fn$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION freeze_guia_pei_base() RETURNS TRIGGER AS $fn$
BEGIN
    -- Valor usado pelo calculo do PEI: capturado uma unica vez, no INSERT da
    -- guia. Em updates o campo permanece intocado (preserva o valor original).
    -- 0038: NULLIF evita congelar zero (qtde ainda nao lida) — o COALESCE do
    -- calculate_patient_pei usara a qtde corrente quando ela chegar.
    IF TG_OP = 'INSERT' THEN
        NEW.qtde_solicitada_pei := NULLIF(NEW.qtde_solicitada, 0);
    END IF;
    RETURN NEW;
END;
$fn$ LANGUAGE plpgsql;

-- Backfill 1: PEI fantasma sem codigo (hoje: 0 linhas; defensivo)
UPDATE patient_pei SET ativo = FALSE, updated_at = NOW()
WHERE codigo_procedimento IS NULL OR btrim(codigo_procedimento) = '';

-- Backfill 2: descongela zeros (hoje: conferido no diagnostico; defensivo)
UPDATE base_guias SET qtde_solicitada_pei = NULL
WHERE qtde_solicitada_pei = 0;
"""

out = r"C:\dev\clmf_hub_basic\backend\migrations\0038_pei_codigo_null_qtde_zero.sql"
with open(out, "w", encoding="utf-8", newline="\n") as f:
    f.write(sql)
print(f"OK escrito: {out} ({len(sql)} bytes)")
