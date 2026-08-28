"""
Testes de validação e normalização de entradas do cliente.

O foco é o que chega da conversa em linguagem natural: CPF pontuado, data no
formato brasileiro, valor com "R$" e "mil".
"""

from datetime import date

import pytest

from src.core.validadores import (
    CPFInvalidoError,
    DataInvalidaError,
    ValorMonetarioInvalidoError,
    calcular_idade,
    datas_conferem,
    formatar_cpf,
    mascarar_cpf,
    normalizar_cpf,
    normalizar_data_nascimento,
    normalizar_valor_monetario,
)

# CPFs válidos presentes em data/seed/clientes.csv.
CPF_COM_ZEROS = "00553479326"
CPF_NORMAL = "39819391903"


# --------------------------------------------------------------------------- #
# 1. CPF
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "entrada",
    [
        CPF_COM_ZEROS,
        "005.534.793-26",
        "005 534 793 26",
        "  005.534.793-26  ",
        "meu CPF é 005.534.793-26",
    ],
)
def test_cpf_valido_em_varios_formatos(entrada):
    assert normalizar_cpf(entrada) == CPF_COM_ZEROS


def test_cpf_preserva_zeros_a_esquerda():
    """Regra inviolável nº 4: o retorno é str e os zeros sobrevivem."""
    resultado = normalizar_cpf(CPF_COM_ZEROS)
    assert isinstance(resultado, str)
    assert resultado.startswith("00")
    assert len(resultado) == 11


def test_cpf_vindo_como_int_perdeu_zeros_e_nao_e_aceito():
    """Se alguém deixar o pandas inferir o tipo, o CPF chega assim — e falha
    de forma barulhenta, em vez de autenticar a pessoa errada."""
    with pytest.raises(CPFInvalidoError, match="11 dígitos"):
        normalizar_cpf(553479326)


@pytest.mark.parametrize(
    "entrada,trecho",
    [
        (None, "não informado"),
        ("", "nenhum dígito"),
        ("abc", "nenhum dígito"),
        ("123", "11 dígitos"),
        ("005534793260", "11 dígitos"),
        ("00000000000", "dígitos iguais"),
        ("11111111111", "dígitos iguais"),
        ("00553479327", "verificadores"),
        ("39819391900", "verificadores"),
        (12345678901.0, "texto ou inteiro"),
        (True, "texto ou inteiro"),
    ],
)
def test_cpf_invalido(entrada, trecho):
    with pytest.raises(CPFInvalidoError, match=trecho):
        normalizar_cpf(entrada)


def test_formatar_cpf():
    assert formatar_cpf(CPF_COM_ZEROS) == "005.534.793-26"


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        (CPF_COM_ZEROS, "***.***.793-26"),
        ("005.534.793-26", "***.***.793-26"),
        ("invalido", "***"),
        (None, "***"),
        ("", "***"),
    ],
)
def test_mascarar_cpf(entrada, esperado):
    assert mascarar_cpf(entrada) == esperado


def test_mascara_nunca_expoe_o_inicio_do_cpf():
    """O log não pode conter o CPF completo (seção 6 do CLAUDE.md)."""
    mascarado = mascarar_cpf(CPF_NORMAL)
    assert CPF_NORMAL not in mascarado
    assert CPF_NORMAL[:6] not in mascarado


def test_mascarar_cpf_nunca_levanta_excecao():
    """É chamada em caminho de log, inclusive ao logar um CPF inválido."""
    for entrada in [None, "", "abc", 123, 1.5, True, object()]:
        assert isinstance(mascarar_cpf(entrada), str)


# --------------------------------------------------------------------------- #
# 2. Data de nascimento
# --------------------------------------------------------------------------- #

HOJE = date(2026, 8, 28)


@pytest.mark.parametrize(
    "entrada",
    ["1988-03-14", "14/03/1988", "14-03-1988", "14.03.1988", "  14/03/1988 "],
)
def test_data_valida_em_varios_formatos(entrada):
    assert normalizar_data_nascimento(entrada, hoje=HOJE) == "1988-03-14"


def test_data_aceita_objeto_date():
    assert normalizar_data_nascimento(date(1988, 3, 14), hoje=HOJE) == "1988-03-14"


@pytest.mark.parametrize(
    "entrada,trecho",
    [
        (None, "não informada"),
        ("", "não informada"),
        ("ontem", "não reconhecida"),
        ("32/01/1990", "não reconhecida"),
        ("1988-02-30", "não reconhecida"),
        ("14/03/88", "não reconhecida"),
        ("2030-01-01", "futuro"),
        ("1800-01-01", "implausível"),
    ],
)
def test_data_invalida(entrada, trecho):
    with pytest.raises(DataInvalidaError, match=trecho):
        normalizar_data_nascimento(entrada, hoje=HOJE)


@pytest.mark.parametrize(
    "nascimento,esperado",
    [
        (date(1988, 3, 14), 38),  # aniversário já passou em 2026
        (date(1988, 12, 31), 37),  # ainda não fez
        (date(1988, 8, 28), 38),  # faz hoje
        (date(1988, 8, 29), 37),  # faz amanhã
    ],
)
def test_calcular_idade(nascimento, esperado):
    assert calcular_idade(nascimento, hoje=HOJE) == esperado


def test_datas_conferem_entre_formatos_diferentes():
    """O cliente digita 14/03/1988; o CSV guarda 1988-03-14."""
    assert datas_conferem("14/03/1988", "1988-03-14") is True


def test_datas_diferentes_nao_conferem():
    assert datas_conferem("15/03/1988", "1988-03-14") is False


def test_data_malformada_e_falha_de_autenticacao_nao_excecao():
    assert datas_conferem("qualquer coisa", "1988-03-14") is False


# --------------------------------------------------------------------------- #
# 3. Valor monetário
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        (5000, 5000.00),
        (5000.5, 5000.50),
        ("5000", 5000.00),
        ("R$ 5000", 5000.00),
        ("r$5000", 5000.00),
        ("5000 reais", 5000.00),
        ("12.500,00", 12500.00),  # formato brasileiro
        ("12,500.00", 12500.00),  # formato americano
        ("12.500", 12500.00),  # milhar brasileiro sem centavos
        ("1.234,56", 1234.56),
        ("1,5", 1.50),
        ("12 mil", 12000.00),
        ("12mil", 12000.00),
        ("5k", 5000.00),
        ("1.5 milhao", 1500000.00),
        ("2 milhoes", 2000000.00),
        ("2 milhões", 2000000.00),
        ("R$ 12 mil", 12000.00),
        ("quero 8000 de limite", 8000.00),
        (0, 0.00),
    ],
)
def test_valor_monetario_valido(entrada, esperado):
    assert normalizar_valor_monetario(entrada) == esperado


def test_valor_monetario_arredonda_para_centavos():
    assert normalizar_valor_monetario(1234.5678) == 1234.57


@pytest.mark.parametrize(
    "entrada,trecho",
    [
        (None, "não informado"),
        ("", "não informado"),
        ("muito dinheiro", "não reconhecido"),
        ("abc", "não reconhecido"),
        (-1, "não pode ser negativo"),
        ("-500", "não pode ser negativo"),
        ("R$ -500,00", "não pode ser negativo"),
        (float("nan"), "número finito"),
        (float("inf"), "número finito"),
        (True, "booleano"),
        (10**10, "excede o máximo"),
    ],
)
def test_valor_monetario_invalido(entrada, trecho):
    with pytest.raises(ValorMonetarioInvalidoError, match=trecho):
        normalizar_valor_monetario(entrada)


def test_mensagem_de_erro_usa_o_nome_do_campo():
    with pytest.raises(ValorMonetarioInvalidoError, match="Renda mensal"):
        normalizar_valor_monetario("abc", nome="Renda mensal")
