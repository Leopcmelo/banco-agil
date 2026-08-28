"""
Testes das faixas de score e da decisão de aumento de limite.

A decisão aprovado/rejeitado é regra de negócio pura: se um destes testes
quebrar, ou o ADR mudou ou existe um bug. Nunca ajuste o número esperado
apenas para o teste passar (seção 7 do CLAUDE.md).
"""

from dataclasses import FrozenInstanceError

import pytest

from src.core.limites import (
    STATUS_APROVADO,
    STATUS_REJEITADO,
    DecisaoLimite,
    FaixaScore,
    ScoreForaDasFaixasError,
    TabelaLimitesError,
    avaliar_solicitacao,
    faixa_do_score,
    limite_permitido,
    validar_tabela,
)

# Espelha data/seed/score_limite.csv.
TABELA = [
    FaixaScore(0, 299, 500.00),
    FaixaScore(300, 499, 2000.00),
    FaixaScore(500, 699, 5000.00),
    FaixaScore(700, 849, 15000.00),
    FaixaScore(850, 1000, 50000.00),
]


# --------------------------------------------------------------------------- #
# 1. Validação da tabela — dados ruins devem falhar cedo
# --------------------------------------------------------------------------- #


def test_tabela_valida_passa_e_sai_ordenada():
    ordenadas = validar_tabela(reversed(TABELA))
    assert [f.score_min for f in ordenadas] == [0, 300, 500, 700, 850]


def test_tabela_vazia_e_rejeitada():
    with pytest.raises(TabelaLimitesError, match="vazia"):
        validar_tabela([])


def test_faixa_invertida_e_rejeitada():
    with pytest.raises(TabelaLimitesError, match="invertida"):
        validar_tabela([FaixaScore(0, 1000, 10.0), FaixaScore(600, 500, 20.0)])


def test_limite_negativo_e_rejeitado():
    with pytest.raises(TabelaLimitesError, match="negativo"):
        validar_tabela([FaixaScore(0, 1000, -1.0)])


def test_buraco_entre_faixas_e_rejeitado():
    """Score 300 ficaria sem limite definido — isso é erro de dados."""
    with pytest.raises(TabelaLimitesError, match="Buraco"):
        validar_tabela([FaixaScore(0, 299, 500.0), FaixaScore(301, 1000, 2000.0)])


def test_sobreposicao_entre_faixas_e_rejeitada():
    with pytest.raises(TabelaLimitesError, match="sobrep"):
        validar_tabela([FaixaScore(0, 400, 500.0), FaixaScore(300, 1000, 2000.0)])


def test_tabela_que_nao_comeca_em_zero_e_rejeitada():
    with pytest.raises(TabelaLimitesError, match="começar em 0"):
        validar_tabela([FaixaScore(1, 1000, 500.0)])


def test_tabela_que_nao_termina_em_1000_e_rejeitada():
    with pytest.raises(TabelaLimitesError, match="terminar em 1000"):
        validar_tabela([FaixaScore(0, 999, 500.0)])


# --------------------------------------------------------------------------- #
# 2. Lookup da faixa — bordas inclusivas nos dois lados
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "score,esperado",
    [
        (0, 500.00),
        (299, 500.00),
        (300, 2000.00),  # borda inferior inclusiva
        (499, 2000.00),  # borda superior inclusiva
        (500, 5000.00),
        (699, 5000.00),
        (700, 15000.00),
        (849, 15000.00),
        (850, 50000.00),
        (1000, 50000.00),
    ],
)
def test_limite_por_faixa(score, esperado):
    assert limite_permitido(score, TABELA) == esperado


def test_todo_score_de_0_a_1000_tem_faixa():
    """Nenhum score válido pode cair fora da tabela."""
    for score in range(0, 1001):
        assert faixa_do_score(score, TABELA).contem(score)


@pytest.mark.parametrize("score", [-1, 1001, 5000])
def test_score_fora_do_intervalo_e_erro(score):
    with pytest.raises(TabelaLimitesError, match="fora do intervalo"):
        limite_permitido(score, TABELA)


@pytest.mark.parametrize("score", ["abc", None, 549.7, True])
def test_score_malformado_e_erro(score):
    with pytest.raises(TabelaLimitesError):
        limite_permitido(score, TABELA)


def test_tabela_corrompida_levanta_erro_em_vez_de_aprovar():
    """Uma tabela inutilizável jamais deve virar aprovação silenciosa."""
    with pytest.raises((TabelaLimitesError, ScoreForaDasFaixasError)):
        limite_permitido(500, [])


def test_guarda_de_score_sem_faixa_e_alcancavel():
    """`faixa_do_score` protege contra tabela validada mas não cobrindo o score.

    Só é alcançável com a validação neutralizada, então o teste a neutraliza de
    propósito: a guarda existe para que um dado corrompido em runtime vire
    exceção, e não um teto de crédito arbitrário.
    """
    import src.core.limites as limites

    original = limites.validar_tabela
    limites.validar_tabela = lambda faixas: tuple(faixas)
    try:
        with pytest.raises(ScoreForaDasFaixasError):
            limites.faixa_do_score(500, [FaixaScore(0, 299, 500.0)])
    finally:
        limites.validar_tabela = original


# --------------------------------------------------------------------------- #
# 3. Decisão sobre a solicitação
# --------------------------------------------------------------------------- #


def test_pedido_dentro_do_teto_e_aprovado():
    d = avaliar_solicitacao(720, limite_atual=8000, novo_limite_solicitado=12000,
                            faixas=TABELA)
    assert d.status == STATUS_APROVADO
    assert d.aprovado is True
    assert d.limite_permitido == 15000.00
    assert d.e_aumento is True


def test_pedido_exatamente_no_teto_e_aprovado():
    """O teto da faixa é inclusivo."""
    d = avaliar_solicitacao(720, 8000, 15000.00, TABELA)
    assert d.status == STATUS_APROVADO


def test_um_centavo_acima_do_teto_e_rejeitado():
    d = avaliar_solicitacao(720, 8000, 15000.01, TABELA)
    assert d.status == STATUS_REJEITADO
    assert d.motivo == "valor_acima_do_teto_da_faixa"


def test_score_baixo_rejeita_pedido_alto():
    d = avaliar_solicitacao(150, 300, 5000, TABELA)
    assert d.status == STATUS_REJEITADO
    assert d.limite_permitido == 500.00


def test_pedido_menor_que_o_limite_atual_nao_e_aumento():
    """Informativo apenas: não muda a decisão, que segue a regra do teto."""
    d = avaliar_solicitacao(720, 8000, 5000, TABELA)
    assert d.aprovado is True
    assert d.e_aumento is False


def test_entrevista_que_sobe_o_score_muda_a_decisao():
    """O mesmo pedido rejeitado passa a ser aprovado com score maior.

    É exatamente o fluxo Crédito -> Entrevista -> Crédito do enunciado.
    """
    pedido = dict(limite_atual=1500, novo_limite_solicitado=4000, faixas=TABELA)
    assert avaliar_solicitacao(450, **pedido).status == STATUS_REJEITADO
    assert avaliar_solicitacao(640, **pedido).status == STATUS_APROVADO


def test_decisao_serializa_para_dict():
    d = avaliar_solicitacao(720, 8000, 12000, TABELA)
    dados = d.as_dict()
    assert dados["status"] == STATUS_APROVADO
    assert dados["aprovado"] is True
    assert set(dados) >= {"status", "limite_permitido", "score", "motivo"}


@pytest.mark.parametrize(
    "kwargs",
    [
        {"limite_atual": -1},
        {"novo_limite_solicitado": -0.01},
        {"novo_limite_solicitado": "muito"},
        {"novo_limite_solicitado": None},
        {"novo_limite_solicitado": float("inf")},
        {"limite_atual": True},
    ],
)
def test_valores_monetarios_invalidos_sao_rejeitados(kwargs):
    base = dict(score=720, limite_atual=8000, novo_limite_solicitado=12000,
                faixas=TABELA)
    base.update(kwargs)
    with pytest.raises(TabelaLimitesError):
        avaliar_solicitacao(**base)


def test_decisao_e_imutavel():
    d = avaliar_solicitacao(720, 8000, 12000, TABELA)
    assert isinstance(d, DecisaoLimite)
    with pytest.raises(FrozenInstanceError):
        d.status = STATUS_REJEITADO  # type: ignore[misc]
