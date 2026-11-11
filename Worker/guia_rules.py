# -*- coding: utf-8 -*-
"""Regras de elegibilidade de guias Unimed Goiânia — R1 (dois marcos).

Funções PURAS (sem Selenium/DB) para serem a fonte única das decisões de
coleta (DRY): o scraper ImportBaseGuias consulta este módulo e não decide
datas isoladamente. Testes unitários em worker/tests/test_guia_rules.py.

R1 (PLANO_CORRECAO_GUIAS_VALIDADE.md §3, rev. 11/11):
    Elegível p/ base_guias/PEI  <=>  (a) autorização ≤ 270 dias
                                     E (b) validade da senha ≥ hoje
    - Fora da janela (a)  → exibição "Antigas (270d)"; sem fetch do detalhe.
    - Vencida (b)         → exibição "Vencida"; não grava/renova a base.
    - Validade ausente    → mantém coleta vigente (regra de bloqueio Thered).
"""
import datetime

# ── Marcos e rótulos (contrato único scraper ↔ dispatcher ↔ backend/frontend) ──
JANELA_AUTORIZACAO_DIAS = 270   # marco (a): crivo da data de autorização/solicitação

TIPO_VALIDAS = "All Sucess"     # legado — mantido (compat)
TIPO_BLOQUEADAS = "Thered"      # legado — mantido (compat)
TIPO_VENCIDAS = "Vencidas"
TIPO_ANTIGAS = "Antigas270"
TIPO_NEGADAS = "Negadas"
TIPO_SEM_GUIAS = "Sem Guias"    # explícito; null/'Null' seguem como legado

STATUS_GUIA_VENCIDA = "Vencida"


def parse_data_brasileira(texto):
    """Converte 'DD/MM/AAAA' (possivelmente com espaços/&nbsp;) em date; senão None."""
    if not texto or not isinstance(texto, str):
        return None
    try:
        return datetime.datetime.strptime(texto.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None


def data_corte_janela(hoje=None):
    """Marco (a): limite inferior da janela de autorização (hoje − 270d)."""
    hoje = hoje or datetime.date.today()
    return hoje - datetime.timedelta(days=JANELA_AUTORIZACAO_DIAS)


def esta_na_janela(data_solicitacao, hoje=None):
    """Marco (a): autorização dentro da janela de 270 dias.

    Data ilegível/ausente não derruba a guia (fallback conservador: dentro).
    """
    if data_solicitacao is None:
        return True
    hoje = hoje or datetime.date.today()
    return data_solicitacao >= data_corte_janela(hoje)


def validade_vigente(validade, hoje=None):
    """Marco (b): validade da senha ≥ hoje. None ⇒ sem decisão (ausente)."""
    if validade is None:
        return None
    hoje = hoje or datetime.date.today()
    return validade >= hoje


def decidir_guia_autorizada(data_solicitacao, validade, hoje=None):
    """Decisão completa de uma guia 'Autorizado' (R1 dupla).

    Retorna uma tupla (acao, motivo):
      ('coletar',  'elegivel')            — janela ✓ e validade ✓
      ('coletar',  'validade_ausente')    — janela ✓, validade ilegível (regra vigente)
      ('vencida',  'validade_expirada')   — janela ✓, validade < hoje
      ('antiga',   'fora_janela_270')     — marco (a) reprovado (crivo mantido)
    """
    hoje = hoje or datetime.date.today()
    if not esta_na_janela(data_solicitacao, hoje):
        return ("antiga", "fora_janela_270")
    vigente = validade_vigente(validade, hoje)
    if vigente is None:
        return ("coletar", "validade_ausente")
    if not vigente:
        return ("vencida", "validade_expirada")
    return ("coletar", "elegivel")


def classificar_tipo_json(valida_guias, fora_janela=False, algum_negado=False,
                          alguma_vencida=False, hoje=None):
    """Truth table final do job (R2). Precedência quando nada elegível:

        Thered/All Sucess (elegíveis) > Vencidas > Antigas270 > Negadas > Sem Guias

    - valida_guias: dict nr→info das guias ELEGÍVEIS (inclui bloqueadas/Em
      Estudo dentro da janela — mesma semântica legada de Vinculo_prestador).
    - alguma_vencida: guia dentro da janela com validade < hoje.
    - fora_janela: houve linha Autorizado/Em Estudo com autorização > 270d.
    - algum_negado: houve linha com status 'Negado' ignorada.
    """
    del hoje  # reserva de assinatura; classificação não depende da data
    if valida_guias:
        if any(val.get("Vinculo_prestador") != "Guia Válida"
               for val in valida_guias.values()):
            return TIPO_BLOQUEADAS
        return TIPO_VALIDAS
    if alguma_vencida:
        return TIPO_VENCIDAS
    if fora_janela:
        return TIPO_ANTIGAS
    if algum_negado:
        return TIPO_NEGADAS
    return TIPO_SEM_GUIAS
