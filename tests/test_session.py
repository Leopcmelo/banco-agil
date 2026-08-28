"""
Testes do estado de sessão.

O ponto central: autenticação e contagem de tentativas são estado de CÓDIGO
(regra inviolável nº 6 e ADR-003). Nenhum caminho aqui depende do LLM.
"""

import pytest

from src.session import MAX_TENTATIVAS_AUTH, DadosEntrevista, SessionState

CPF = "00553479326"


# --------------------------------------------------------------------------- #
# 1. Estado inicial
# --------------------------------------------------------------------------- #


def test_sessao_nasce_nao_autenticada():
    s = SessionState()
    assert s.autenticado is False
    assert s.pode_operar is False
    assert s.cpf is None
    assert s.tentativas_auth == 0
    assert s.tentativas_restantes == MAX_TENTATIVAS_AUTH


def test_autenticar_libera_a_operacao():
    s = SessionState()
    s.autenticar(CPF, "Ana Beatriz Cardoso")
    assert s.pode_operar is True
    assert s.cpf == CPF
    assert s.nome_cliente == "Ana Beatriz Cardoso"


# --------------------------------------------------------------------------- #
# 2. Tentativas de autenticação — ADR-003
# --------------------------------------------------------------------------- #


def test_maximo_de_tres_tentativas_no_total():
    """A inicial + 2 novas, conforme o texto do enunciado."""
    assert MAX_TENTATIVAS_AUTH == 3


def test_contador_decrementa_as_tentativas_restantes():
    s = SessionState()
    assert s.registrar_tentativa() == 2
    assert s.registrar_tentativa() == 1
    assert s.registrar_tentativa() == 0


def test_terceira_falha_bloqueia_a_sessao():
    s = SessionState()
    for _ in range(3):
        s.registrar_tentativa()
    assert s.bloqueado is True
    assert s.pode_operar is False


def test_duas_falhas_ainda_nao_bloqueiam():
    s = SessionState()
    s.registrar_tentativa()
    s.registrar_tentativa()
    assert s.bloqueado is False


def test_tentativa_bem_sucedida_na_terceira_nao_bloqueia():
    """Errar duas vezes e acertar na terceira precisa funcionar."""
    s = SessionState()
    s.registrar_tentativa()
    s.registrar_tentativa()
    s.autenticar(CPF, "Ana")
    s.registrar_tentativa()
    assert s.bloqueado is False
    assert s.pode_operar is True


def test_sessao_bloqueada_recusa_autenticacao_posterior():
    s = SessionState()
    for _ in range(3):
        s.registrar_tentativa()
    with pytest.raises(PermissionError):
        s.autenticar(CPF, "Ana")
    assert s.autenticado is False


def test_tentativas_restantes_nunca_fica_negativo():
    s = SessionState()
    for _ in range(10):
        s.registrar_tentativa()
    assert s.tentativas_restantes == 0


# --------------------------------------------------------------------------- #
# 3. Encerramento
# --------------------------------------------------------------------------- #


def test_encerrar_impede_novas_operacoes():
    s = SessionState()
    s.autenticar(CPF, "Ana")
    s.encerrar("cliente pediu para encerrar")
    assert s.encerrado is True
    assert s.pode_operar is False
    assert s.motivo_encerramento == "cliente pediu para encerrar"


# --------------------------------------------------------------------------- #
# 4. Dados da entrevista
# --------------------------------------------------------------------------- #


def test_entrevista_comeca_incompleta():
    e = DadosEntrevista()
    assert e.completa is False
    assert set(e.faltando) == set(DadosEntrevista.CAMPOS)


def test_entrevista_fica_completa_com_os_cinco_campos():
    e = DadosEntrevista(
        renda_mensal=8000,
        tipo_emprego="formal",
        despesas_fixas=3000,
        num_dependentes=0,
        tem_dividas="não",
    )
    assert e.completa is True
    assert e.faltando == []


def test_entrevista_aponta_exatamente_o_que_falta():
    e = DadosEntrevista(renda_mensal=8000, tipo_emprego="formal")
    assert set(e.faltando) == {"despesas_fixas", "num_dependentes", "tem_dividas"}


def test_zero_e_resposta_valida_nao_campo_vazio():
    """0 dependentes e renda 0 são respostas legítimas — só None falta."""
    e = DadosEntrevista(
        renda_mensal=0,
        tipo_emprego="desempregado",
        despesas_fixas=0,
        num_dependentes=0,
        tem_dividas=False,
    )
    assert e.completa is True


def test_limpar_entrevista_zera_os_campos():
    e = DadosEntrevista(renda_mensal=8000, tipo_emprego="formal")
    e.limpar()
    assert e.completa is False
    assert e.renda_mensal is None


def test_cada_sessao_tem_sua_propria_entrevista():
    """Um `field(default_factory=...)` mal feito compartilharia o objeto."""
    a, b = SessionState(), SessionState()
    a.entrevista.renda_mensal = 9000
    assert b.entrevista.renda_mensal is None


# --------------------------------------------------------------------------- #
# 5. Resumo seguro
# --------------------------------------------------------------------------- #


def test_resumo_seguro_mascara_o_cpf():
    s = SessionState()
    s.autenticar(CPF, "Ana Beatriz Cardoso")
    resumo = s.resumo_seguro()
    assert CPF not in str(resumo)
    assert resumo["cpf"] == "***.***.793-26"


def test_resumo_seguro_nao_contem_data_de_nascimento():
    s = SessionState()
    s.autenticar(CPF, "Ana")
    assert "data_nascimento" not in s.resumo_seguro()
