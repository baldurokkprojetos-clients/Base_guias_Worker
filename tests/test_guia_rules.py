# -*- coding: utf-8 -*-
"""Testes unitários — guia_rules (R1: dois marcos). QA CT-U5/CT-U6 do
PLANO_CORRECAO_GUIAS_VALIDADE.md §5.1.

Rodar (zero dependências, stdlib unittest):
    python -m unittest discover -s worker/tests -v
ou, a partir de worker/:
    python -m unittest discover -s tests -v
"""
import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "Worker"))

from guia_rules import (  # noqa: E402
    JANELA_AUTORIZACAO_DIAS,
    TIPO_ANTIGAS,
    TIPO_BLOQUEADAS,
    TIPO_NEGADAS,
    TIPO_SEM_GUIAS,
    TIPO_VALIDAS,
    TIPO_VENCIDAS,
    classificar_tipo_json,
    data_corte_janela,
    decidir_guia_autorizada,
    esta_na_janela,
    parse_data_brasileira,
    validade_vigente,
)

HOJE_LOTE_1011 = datetime.date(2026, 11, 10)  # lote analisado (23:01 UTC)
GUIA_VALIDA = {"66363404": {"Vinculo_prestador": "Guia Válida"}}


class TestParseData(unittest.TestCase):
    def test_formato_portal(self):
        self.assertEqual(parse_data_brasileira("01/06/2027"), datetime.date(2027, 6, 1))

    def test_espacos_e_nbsp(self):
        self.assertEqual(parse_data_brasileira(" 02/11/2026 "), datetime.date(2026, 11, 2))

    def test_invalidos_retornam_none(self):
        for vazio in ("", None, "abc", "31/02/2027", "2027-06-01", 0):
            self.assertIsNone(parse_data_brasileira(vazio), msg=f"esperado None para {vazio!r}")


class TestJanela270(unittest.TestCase):
    def test_corte_do_lote_1011(self):
        # 270 dias antes de 10/11/2026 => 13/02/2026 (caso HEITOR MENDES:
        # guia de 12/02/2026 ficou FORA por 1 dia)
        self.assertEqual(data_corte_janela(HOJE_LOTE_1011), datetime.date(2026, 2, 13))

    def test_fronteira(self):
        self.assertTrue(esta_na_janela(datetime.date(2026, 2, 13), HOJE_LOTE_1011))
        self.assertFalse(esta_na_janela(datetime.date(2026, 2, 12), HOJE_LOTE_1011))

    def test_janela_configurada(self):
        self.assertEqual(JANELA_AUTORIZACAO_DIAS, 270)

    def test_data_ilegivel_mantem_dentro(self):
        # fallback conservador (guia não derruba por data ilegível)
        self.assertTrue(esta_na_janela(None, HOJE_LOTE_1011))


class TestDecisaoGuia(unittest.TestCase):
    """CT-U6 — decisão por linha (função pura)."""

    def test_fora_da_janela_validade_futura(self):
        # MARIA LUISA: autorização 09/10/2025, validade 01/06/2027 (vigente)
        acao = decidir_guia_autorizada(datetime.date(2025, 10, 9),
                                       datetime.date(2027, 6, 1), HOJE_LOTE_1011)
        self.assertEqual(acao, ("antiga", "fora_janela_270"))

    def test_dentro_validade_vigente(self):
        acao = decidir_guia_autorizada(datetime.date(2026, 6, 1),
                                       datetime.date(2027, 6, 1), HOJE_LOTE_1011)
        self.assertEqual(acao, ("coletar", "elegivel"))

    def test_dentro_validade_expirada(self):
        # guias 753638xx: validade 02/11/2026 < 10/11/2026
        acao = decidir_guia_autorizada(datetime.date(2026, 5, 2),
                                       datetime.date(2026, 11, 2), HOJE_LOTE_1011)
        self.assertEqual(acao, ("vencida", "validade_expirada"))

    def test_validade_no_dia_elegivel(self):
        # fronteira inclusiva: validade == hoje ainda vale
        acao = decidir_guia_autorizada(datetime.date(2026, 8, 1),
                                       HOJE_LOTE_1011, HOJE_LOTE_1011)
        self.assertEqual(acao, ("coletar", "elegivel"))

    def test_validade_ausente_mantem_coleta(self):
        # CT-U4: sem #CampoValidadeSenha → regra vigente (nunca crash)
        acao = decidir_guia_autorizada(datetime.date(2026, 8, 1), None, HOJE_LOTE_1011)
        self.assertEqual(acao, ("coletar", "validade_ausente"))

    def test_validade_vigente_helper(self):
        self.assertIsNone(validade_vigente(None, HOJE_LOTE_1011))
        self.assertTrue(validade_vigente(HOJE_LOTE_1011, HOJE_LOTE_1011))
        self.assertFalse(validade_vigente(datetime.date(2026, 11, 9), HOJE_LOTE_1011))


class TestClassificarTipoJson(unittest.TestCase):
    """CT-U5 — truth table do tipo_json (R2)."""

    def test_todas_elegiveis_validas(self):
        self.assertEqual(classificar_tipo_json(GUIA_VALIDA), TIPO_VALIDAS)

    def test_alguma_bloqueada(self):
        guias = {"1": {"Vinculo_prestador": "Guia Válida"},
                 "2": {"Vinculo_prestador": "Prestador principal não possui itens liberados."}}
        self.assertEqual(classificar_tipo_json(guias), TIPO_BLOQUEADAS)

    def test_zero_elegiveis_vencida(self):
        self.assertEqual(classificar_tipo_json({}, alguma_vencida=True), TIPO_VENCIDAS)

    def test_zero_elegiveis_antiga(self):
        self.assertEqual(classificar_tipo_json({}, fora_janela=True), TIPO_ANTIGAS)

    def test_precedencia_vencida_sobre_antiga(self):
        self.assertEqual(classificar_tipo_json({}, fora_janela=True, alguma_vencida=True),
                         TIPO_VENCIDAS)

    def test_zero_elegiveis_negadas(self):
        self.assertEqual(classificar_tipo_json({}, algum_negado=True), TIPO_NEGADAS)

    def test_vazio_real_sem_guias(self):
        self.assertEqual(classificar_tipo_json({}), TIPO_SEM_GUIAS)

    def test_exibicao_nao_contamina_elegiveis(self):
        # entradas apenas-exibição (Vencida) NÃO estão em valida_guias —
        # job com elegível + vencida segue All Sucess
        self.assertEqual(classificar_tipo_json(GUIA_VALIDA, alguma_vencida=True), TIPO_VALIDAS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
