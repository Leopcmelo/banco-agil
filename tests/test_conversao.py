"""
Testes da conversão de moeda.

A aritmética que o agente não pode fazer vive aqui, então é aqui que ela
precisa estar coberta em caminho feliz, borda e entrada inválida.
"""

import pytest

from src.core.conversao import (
    Conversao,
    ConversaoError,
    converter,
    formatar_valor_br,
)

# --------------------------------------------------------------------------- #
# 1. Casos de referência
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "valor,cotacao,esperado",
    [
        (100, 5.2244, 522.44),  # 100 dólares em reais
        (8000, 0.1914, 1531.20),  # limite de crédito em dólares
        (1, 5.2244, 5.22),
        (0, 5.2244, 0.00),  # zero é valor legítimo
        (2500.50, 6.0555, 15141.78),  # 15141,7775 arredondado
        (1_000_000, 5.2244, 5224400.00),
    ],
)
def test_conversao_de_referencia(valor, cotacao, esperado):
    assert converter(valor, cotacao).valor_convertido == esperado


def test_resultado_traz_as_parcelas_do_calculo():
    """O agente explica o número sem recalcular nada."""
    c = converter(100, 5.2244, "USD", "BRL")
    assert isinstance(c, Conversao)
    assert c.valor_origem == 100.00
    assert c.moeda_origem == "USD"
    assert c.valor_convertido == 522.44
    assert c.moeda_destino == "BRL"
    assert c.cotacao == 5.2244


def test_moedas_sao_normalizadas_para_maiusculas():
    c = converter(10, 5.0, "usd", "brl")
    assert (c.moeda_origem, c.moeda_destino) == ("USD", "BRL")


def test_serializa_para_dict():
    dados = converter(100, 5.2244, "USD", "BRL").as_dict()
    assert set(dados) == {
        "valor_origem",
        "moeda_origem",
        "valor_convertido",
        "moeda_destino",
        "cotacao",
    }


# --------------------------------------------------------------------------- #
# 2. Arredondamento — dinheiro não usa banker's rounding
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "valor,cotacao,esperado",
    [
        (1, 0.125, 0.13),  # round() nativo daria 0.12
        (1, 0.135, 0.14),  # round() nativo daria 0.14
        (1, 0.005, 0.01),  # round() nativo daria 0.00
        (1, 0.015, 0.02),
        (1, 0.004, 0.00),  # abaixo da metade desce
    ],
)
def test_arredondamento_e_meio_para_cima(valor, cotacao, esperado):
    assert converter(valor, cotacao).valor_convertido == esperado


def test_meio_para_cima_diverge_do_round_nativo():
    """Prova que a escolha importa: o nativo erraria este caso."""
    assert converter(1, 0.125).valor_convertido == 0.13
    assert round(0.125, 2) == 0.12


def test_resultado_sempre_tem_no_maximo_duas_casas():
    c = converter(3333.333, 3.333333)
    assert c.valor_convertido == round(c.valor_convertido, 2)


# --------------------------------------------------------------------------- #
# 3. Entradas inválidas
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "valor,trecho",
    [
        (-1, "não pode ser negativo"),
        ("abc", "inválido"),
        (None, "inválido"),
        (float("nan"), "finito"),
        (float("inf"), "finito"),
        (True, "booleano"),
        (10**10, "excede o máximo"),
    ],
)
def test_valor_invalido(valor, trecho):
    with pytest.raises(ConversaoError, match=trecho):
        converter(valor, 5.0)


@pytest.mark.parametrize(
    "cotacao,trecho",
    [
        (0, "maior que zero"),  # cotação zero zeraria qualquer montante
        (-5, "não pode ser negativo"),
        ("abc", "inválido"),
        (None, "inválido"),
        (float("nan"), "finito"),
        (True, "booleano"),
    ],
)
def test_cotacao_invalida(cotacao, trecho):
    with pytest.raises(ConversaoError, match=trecho):
        converter(100, cotacao)


def test_cotacao_zero_nao_vira_montante_zero_silencioso():
    """Zerar o valor do cliente por dado ruim seria pior que falhar."""
    with pytest.raises(ConversaoError):
        converter(8000, 0)


# --------------------------------------------------------------------------- #
# 4. Formatação brasileira
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "valor,esperado",
    [
        (1234.5, "1.234,50"),
        (0, "0,00"),
        (5.2244, "5,22"),
        (1000000, "1.000.000,00"),
        (999.999, "1.000,00"),
        (522.44, "522,44"),
    ],
)
def test_formatacao_brasileira(valor, esperado):
    assert formatar_valor_br(valor) == esperado


def test_formatacao_aceita_mais_casas():
    assert formatar_valor_br(5.2244, casas=4) == "5,2244"


def test_formatacao_rejeita_entrada_invalida():
    with pytest.raises(ConversaoError):
        formatar_valor_br("abc")
