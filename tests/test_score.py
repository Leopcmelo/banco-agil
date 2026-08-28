"""
Testes do motor de score — Banco Ágil.

Os valores esperados foram calculados à mão e conferidos contra a implementação.
Se um teste destes quebrar, a regra de negócio mudou: atualize o ADR-001 no
CLAUDE.md antes de atualizar o número aqui.

Execução:  pytest -v tests/test_score.py
"""

import math

import pytest

from src.core.score import (
    PESO_DEPENDENTES,
    PESO_EMPREGO,
    SCORE_MAX,
    SCORE_MIN,
    TETO_COMPONENTE_RENDA,
    ScoreInputError,
    calcular_score,
    normalizar_dependentes,
    normalizar_dividas,
    normalizar_emprego,
)

# --------------------------------------------------------------------------- #
# 1. Casos numéricos de referência
# --------------------------------------------------------------------------- #

CASOS_REFERENCIA = [
    # (id, renda, despesas, emprego, dependentes, dividas, score_esperado)
    ("perfil_bom_realista", 8000, 3000, "formal", 0, "não", 580),
    ("teto_renda_despesa_zero", 20000, 0, "formal", 0, "não", 1000),
    ("piso_zero_pior_perfil", 0, 2000, "desempregado", 3, "sim", 0),
    ("positivo_baixo", 3000, 1500, "desempregado", 2, "sim", 20),
    ("autonomo_um_dependente", 6000, 2500, "autônomo", 1, "não", 452),
    ("muitos_dependentes", 5000, 2000, "formal", 7, "não", 505),
    ("exatamente_no_teto", 500, 29, "formal", 0, "não", 1000),
    ("logo_abaixo_do_teto", 49000, 2999, "formal", 0, "não", 990),
    ("despesas_maiores_que_renda", 2500, 4000, "formal", 2, "sim", 279),
    ("renda_zero_mas_formal", 0, 0, "formal", 0, "não", 500),
    ("valores_fracionarios", 4567.89, 1234.56, "autônomo", 2, "sim", 271),
]


@pytest.mark.parametrize(
    "renda,despesas,emprego,dependentes,dividas,esperado",
    [c[1:] for c in CASOS_REFERENCIA],
    ids=[c[0] for c in CASOS_REFERENCIA],
)
def test_casos_de_referencia(renda, despesas, emprego, dependentes, dividas, esperado):
    assert (
        calcular_score(renda, despesas, emprego, dependentes, dividas).score == esperado
    )


# --------------------------------------------------------------------------- #
# 2. Invariantes de faixa — o requisito "score de 0 a 1000"
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("renda", [0, 1, 1_000, 50_000, 1_000_000, 10**9])
@pytest.mark.parametrize("despesas", [0, 1, 500, 10_000, 10**9])
@pytest.mark.parametrize("emprego", ["formal", "autônomo", "desempregado"])
@pytest.mark.parametrize("dependentes", [0, 1, 2, 3, 12])
@pytest.mark.parametrize("dividas", ["sim", "não"])
def test_score_sempre_dentro_da_faixa(renda, despesas, emprego, dependentes, dividas):
    score = calcular_score(renda, despesas, emprego, dependentes, dividas).score
    assert SCORE_MIN <= score <= SCORE_MAX
    assert isinstance(score, int)


def test_teto_maximo_e_atingivel():
    """O melhor perfil possível chega exatamente a 1000."""
    r = calcular_score(10**6, 0, "formal", 0, "não")
    assert r.score == SCORE_MAX
    assert r.componente_renda == TETO_COMPONENTE_RENDA


def test_piso_minimo_e_atingivel():
    """O pior perfil possível é limitado em 0, não em número negativo."""
    r = calcular_score(0, 10_000, "desempregado", 5, "sim")
    assert r.score == SCORE_MIN
    assert r.total_bruto < 0
    assert r.clamp_aplicado is True


def test_soma_dos_componentes_fixos_e_500():
    """Justifica a escolha de 500 como teto do componente de renda."""
    maximo_fixo = (
        max(PESO_EMPREGO.values())
        + max(PESO_DEPENDENTES.values())
        + 100  # PESO_DIVIDAS[False]
    )
    assert maximo_fixo == TETO_COMPONENTE_RENDA


# --------------------------------------------------------------------------- #
# 3. Comportamento do teto e do clamp
# --------------------------------------------------------------------------- #


def test_componente_de_renda_satura_em_500():
    r = calcular_score(10**9, 0, "desempregado", 3, "sim")
    assert r.componente_renda == TETO_COMPONENTE_RENDA
    assert r.teto_renda_atingido is True


def test_no_teto_exato_nao_marca_saturacao():
    """500 * 30 / 30 == 500.0 exatamente; a flag só liga acima do teto."""
    r = calcular_score(500, 29, "formal", 0, "não")
    assert r.componente_renda == TETO_COMPONENTE_RENDA
    assert r.teto_renda_atingido is False


def test_clamp_nao_dispara_em_caso_normal():
    r = calcular_score(8000, 3000, "formal", 0, "não")
    assert r.clamp_aplicado is False
    assert r.teto_renda_atingido is False


def test_despesa_zero_nao_divide_por_zero():
    r = calcular_score(5000, 0, "formal", 0, "não")
    assert math.isfinite(r.total_bruto)
    assert r.score == 1000


# --------------------------------------------------------------------------- #
# 4. Monotonicidade — o score precisa "fazer sentido"
# --------------------------------------------------------------------------- #


def _s(renda=6000, despesas=2000, emprego="formal", dep=0, div="não") -> int:
    return calcular_score(renda, despesas, emprego, dep, div).score


def test_mais_renda_nunca_reduz_o_score():
    valores = [0, 500, 1000, 3000, 6000, 12000, 30000]
    scores = [_s(renda=v) for v in valores]
    assert scores == sorted(scores)


def test_mais_despesas_nunca_aumenta_o_score():
    valores = [0, 500, 1000, 3000, 6000, 12000]
    scores = [_s(despesas=v) for v in valores]
    assert scores == sorted(scores, reverse=True)


def test_mais_dependentes_nunca_aumenta_o_score():
    scores = [_s(dep=d) for d in (0, 1, 2, 3, 4, 10)]
    assert scores == sorted(scores, reverse=True)


def test_dividas_reduzem_o_score():
    assert _s(div="sim") < _s(div="não")


def test_ordem_dos_tipos_de_emprego():
    assert _s(emprego="formal") > _s(emprego="autônomo") > _s(emprego="desempregado")


# --------------------------------------------------------------------------- #
# 5. Normalização de entradas vindas da conversa
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("formal", "formal"),
        ("Formal", "formal"),
        ("  FORMAL  ", "formal"),
        ("CLT", "formal"),
        ("carteira assinada", "formal"),
        ("autônomo", "autonomo"),
        ("autonomo", "autonomo"),
        ("AUTÔNOMO", "autonomo"),
        ("freelancer", "autonomo"),
        ("MEI", "autonomo"),
        ("desempregado", "desempregado"),
        ("Desempregado", "desempregado"),
    ],
)
def test_normalizacao_de_emprego(entrada, esperado):
    assert normalizar_emprego(entrada) == esperado


def test_emprego_acentuado_e_sem_acento_geram_o_mesmo_score():
    assert _s(emprego="autônomo") == _s(emprego="autonomo") == _s(emprego="AUTÔNOMO")


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        (0, 0),
        (1, 1),
        (2, 2),
        (3, "3+"),
        (4, "3+"),
        (99, "3+"),
        ("3+", "3+"),
        ("5", "3+"),
        ("2", 2),
    ],
)
def test_normalizacao_de_dependentes(entrada, esperado):
    assert normalizar_dependentes(entrada) == esperado


def test_tres_ou_mais_dependentes_tem_o_mesmo_peso():
    assert _s(dep=3) == _s(dep=4) == _s(dep=50) == _s(dep="3+")


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("sim", True),
        ("Sim", True),
        ("SIM", True),
        ("s", True),
        (True, True),
        (1, True),
        ("não", False),
        ("nao", False),
        ("NÃO", False),
        ("n", False),
        (False, False),
        (0, False),
    ],
)
def test_normalizacao_de_dividas(entrada, esperado):
    assert normalizar_dividas(entrada) is esperado


# --------------------------------------------------------------------------- #
# 6. Entradas inválidas — devem falhar de forma controlada
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "kwargs",
    [
        {"renda_mensal": -1},
        {"despesas_fixas": -0.01},
        {"renda_mensal": "abc"},
        {"renda_mensal": None},
        {"renda_mensal": float("nan")},
        {"renda_mensal": float("inf")},
        {"despesas_fixas": "muitas"},
        {"tipo_emprego": "aposentado"},
        {"tipo_emprego": ""},
        {"tipo_emprego": None},
        {"num_dependentes": -1},
        {"num_dependentes": 2.5},
        {"num_dependentes": "muitos"},
        {"num_dependentes": True},
        {"tem_dividas": "talvez"},
        {"tem_dividas": None},
    ],
)
def test_entradas_invalidas_levantam_score_input_error(kwargs):
    base = {
        "renda_mensal": 5000,
        "despesas_fixas": 2000,
        "tipo_emprego": "formal",
        "num_dependentes": 1,
        "tem_dividas": "não",
    }
    base.update(kwargs)
    with pytest.raises(ScoreInputError):
        calcular_score(**base)


def test_score_input_error_e_um_value_error():
    """Permite que o chamador capture ValueError genérico se preferir."""
    assert issubclass(ScoreInputError, ValueError)


# --------------------------------------------------------------------------- #
# 7. Determinismo
# --------------------------------------------------------------------------- #


def test_mesma_entrada_gera_mesma_saida():
    args = (7321.55, 2810.10, "autônomo", 2, "sim")
    primeiro = calcular_score(*args)
    for _ in range(50):
        assert calcular_score(*args) == primeiro


def test_strings_numericas_equivalem_a_numeros():
    assert calcular_score("8000", "3000", "formal", "0", "não").score == 580


def test_detalhamento_soma_o_total_bruto():
    r = calcular_score(8000, 3000, "formal", 0, "não")
    soma = (
        r.componente_renda
        + r.componente_emprego
        + r.componente_dependentes
        + r.componente_dividas
    )
    assert math.isclose(soma, r.total_bruto, rel_tol=1e-12)


# --------------------------------------------------------------------------- #
# 7. Bordas de tipo — entradas que não são texto nem número
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("entrada", [None, [1], {"n": 1}, (1,), object()])
def test_dependentes_de_tipo_inesperado_e_rejeitado(entrada):
    """int() falha para estes tipos; o erro precisa virar ScoreInputError."""
    with pytest.raises(ScoreInputError, match="inválido"):
        normalizar_dependentes(entrada)


@pytest.mark.parametrize("campo", ["renda_mensal", "despesas_fixas"])
def test_valor_monetario_booleano_e_rejeitado(campo):
    """True vale 1 em Python; aceitar isso silenciosamente seria um bug caro."""
    base = dict(
        renda_mensal=6000,
        despesas_fixas=2000,
        tipo_emprego="formal",
        num_dependentes=0,
        tem_dividas="não",
    )
    base[campo] = True
    with pytest.raises(ScoreInputError, match="booleano"):
        calcular_score(**base)


def test_resultado_serializa_com_o_detalhamento():
    """O agente explica o resultado a partir deste dict, sem recalcular nada."""
    dados = calcular_score(8000, 3000, "formal", 0, "não").as_dict()
    assert dados["score"] == 580
    assert set(dados) == {
        "score",
        "componente_renda",
        "componente_emprego",
        "componente_dependentes",
        "componente_dividas",
        "total_bruto",
        "teto_renda_atingido",
        "clamp_aplicado",
    }
    # A soma dos componentes precisa bater com o bruto informado.
    soma = sum(
        dados[c]
        for c in (
            "componente_renda",
            "componente_emprego",
            "componente_dependentes",
            "componente_dividas",
        )
    )
    assert soma == pytest.approx(dados["total_bruto"])
