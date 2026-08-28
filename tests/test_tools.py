"""
Testes da camada de tools.

O que mais importa aqui não é o caminho feliz, e sim as recusas: nenhuma tool
que exponha dado de cliente pode responder sem `session.autenticado`, e uma
sessão bloqueada precisa recusar tudo (regra inviolável nº 6 e ADR-003).
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.core.limites import STATUS_APROVADO, STATUS_REJEITADO
from src.data.repositories import RepositorioBancoAgil
from src.services import cambio_api
from src.session import SessionState
from src.tools import (
    STATUS_BLOQUEADO,
    STATUS_ERRO,
    STATUS_OK,
    ContextoAtendimento,
    autenticar_cliente,
    consultar_cotacao,
    consultar_historico_solicitacoes,
    consultar_limite,
    consultar_progresso_entrevista,
    converter_valor,
    encerrar_atendimento,
    finalizar_entrevista,
    registrar_resposta_entrevista,
    solicitar_aumento_limite,
)

RAIZ = Path(__file__).resolve().parents[1]
SEED = RAIZ / "data" / "seed"

# Ana Beatriz Cardoso: score 720, limite 8000, teto da faixa 15000.
CPF_ANA = "00553479326"
NASC_ANA = "1988-03-14"
# Giovana Sarti: score 150, limite 300, teto da faixa 500.
CPF_GIOVANA = "97552487739"
NASC_GIOVANA = "1984-12-17"


@pytest.fixture()
def contexto(tmp_path: Path) -> ContextoAtendimento:
    destino = tmp_path / "data"
    destino.mkdir()
    for nome in (
        "clientes.csv",
        "score_limite.csv",
        "solicitacoes_aumento_limite.csv",
    ):
        (destino / nome).write_bytes((SEED / nome).read_bytes())
    return ContextoAtendimento(
        sessao=SessionState(), repositorio=RepositorioBancoAgil(destino)
    )


@pytest.fixture()
def autenticado(contexto: ContextoAtendimento) -> ContextoAtendimento:
    resposta = autenticar_cliente(contexto, CPF_ANA, NASC_ANA)
    assert resposta["status"] == STATUS_OK
    return contexto


# Toda tool que expõe dado de cliente. Usada nos testes de recusa em massa.
TOOLS_PROTEGIDAS = [
    (consultar_limite, ()),
    (solicitar_aumento_limite, (12000,)),
    (consultar_historico_solicitacoes, ()),
    (finalizar_entrevista, ()),
    (consultar_progresso_entrevista, ()),
]


# --------------------------------------------------------------------------- #
# 1. Contrato de resposta
# --------------------------------------------------------------------------- #


def test_toda_tool_devolve_dict_serializavel(autenticado):
    import json

    for tool, args in TOOLS_PROTEGIDAS:
        resposta = tool(autenticado, *args)
        assert set(resposta) == {"status", "dados", "mensagem"}
        assert resposta["status"] in {STATUS_OK, STATUS_ERRO, STATUS_BLOQUEADO}
        json.dumps(resposta)  # não pode conter objeto de domínio


# --------------------------------------------------------------------------- #
# 2. Autenticação — ADR-003
# --------------------------------------------------------------------------- #


def test_autenticacao_bem_sucedida(contexto):
    r = autenticar_cliente(contexto, CPF_ANA, NASC_ANA)
    assert r["status"] == STATUS_OK
    assert r["dados"]["primeiro_nome"] == "Ana"
    assert contexto.sessao.autenticado is True
    assert contexto.sessao.cpf == CPF_ANA


def test_autenticacao_aceita_cpf_pontuado_e_data_brasileira(contexto):
    r = autenticar_cliente(contexto, "005.534.793-26", "14/03/1988")
    assert r["status"] == STATUS_OK


def test_data_errada_falha_e_consome_tentativa(contexto):
    r = autenticar_cliente(contexto, CPF_ANA, "01/01/1990")
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["tentativas_restantes"] == 2
    assert contexto.sessao.autenticado is False


def test_cpf_inexistente_falha(contexto):
    r = autenticar_cliente(contexto, "52998224725", NASC_ANA)
    assert r["status"] == STATUS_ERRO


def test_cpf_malformado_consome_tentativa(contexto):
    r = autenticar_cliente(contexto, "123", NASC_ANA)
    assert r["status"] == STATUS_ERRO
    assert contexto.sessao.tentativas_auth == 1


def test_mensagem_de_falha_nao_revela_qual_campo_errou(contexto):
    """Dizer "o CPF existe mas a data não confere" entregaria a validade do CPF."""
    com_cpf_valido = autenticar_cliente(contexto, CPF_ANA, "01/01/1990")
    contexto.sessao.tentativas_auth = 0
    contexto.sessao.bloqueado = False
    com_cpf_invalido = autenticar_cliente(contexto, "52998224725", NASC_ANA)
    assert com_cpf_valido["mensagem"] == com_cpf_invalido["mensagem"]


def test_terceira_falha_bloqueia_e_muda_o_status(contexto):
    for _ in range(2):
        assert autenticar_cliente(contexto, CPF_ANA, "01/01/1990")["status"] == (
            STATUS_ERRO
        )
    terceira = autenticar_cliente(contexto, CPF_ANA, "01/01/1990")
    assert terceira["status"] == STATUS_BLOQUEADO
    assert contexto.sessao.bloqueado is True


def test_acerto_na_terceira_tentativa_funciona(contexto):
    autenticar_cliente(contexto, CPF_ANA, "01/01/1990")
    autenticar_cliente(contexto, CPF_ANA, "02/01/1990")
    r = autenticar_cliente(contexto, CPF_ANA, NASC_ANA)
    assert r["status"] == STATUS_OK
    assert contexto.sessao.pode_operar is True


def test_reautenticar_e_idempotente_e_nao_consome_tentativa(autenticado):
    r = autenticar_cliente(autenticado, CPF_ANA, NASC_ANA)
    assert r["status"] == STATUS_OK
    assert r["dados"]["ja_autenticado"] is True
    assert autenticado.sessao.tentativas_auth == 0


def test_sessao_bloqueada_nao_autentica_nem_com_dados_certos(contexto):
    for _ in range(3):
        autenticar_cliente(contexto, CPF_ANA, "01/01/1990")
    r = autenticar_cliente(contexto, CPF_ANA, NASC_ANA)
    assert r["status"] == STATUS_BLOQUEADO
    assert contexto.sessao.autenticado is False


# --------------------------------------------------------------------------- #
# 3. Recusas — o coração da regra inviolável nº 6
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("tool,args", TOOLS_PROTEGIDAS)
def test_tool_protegida_recusa_sem_autenticacao(contexto, tool, args):
    r = tool(contexto, *args)
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["motivo"] == "nao_autenticado"


@pytest.mark.parametrize("tool,args", TOOLS_PROTEGIDAS)
def test_tool_protegida_recusa_com_sessao_bloqueada(contexto, tool, args):
    for _ in range(3):
        autenticar_cliente(contexto, CPF_ANA, "01/01/1990")
    r = tool(contexto, *args)
    assert r["status"] == STATUS_BLOQUEADO


@pytest.mark.parametrize("tool,args", TOOLS_PROTEGIDAS)
def test_tool_protegida_recusa_apos_encerramento(autenticado, tool, args):
    encerrar_atendimento(autenticado)
    r = tool(autenticado, *args)
    assert r["status"] == STATUS_BLOQUEADO


def test_nao_da_para_forjar_autenticacao_por_atribuicao(contexto):
    """`autenticado` é somente leitura — achado da auditoria de segurança.

    Antes era um campo comum: `sessao.autenticado = True` junto com um CPF
    qualquer devolvia o limite daquele cliente. Nenhum caminho de produção
    fazia isso, mas a invariante mais importante do sistema não podia
    depender de convenção.
    """
    with pytest.raises(AttributeError):
        contexto.sessao.autenticado = True
    assert consultar_limite(contexto)["dados"]["motivo"] == "nao_autenticado"


def test_sessao_autenticada_sem_cpf_falha_controlada(contexto):
    """Estado inconsistente não pode virar dado de outro cliente."""
    contexto.sessao.autenticar("00553479326", "Ana")
    contexto.sessao.cpf = None
    r = consultar_limite(contexto)
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["motivo"] == "falha_interna"


def test_reautenticar_nao_troca_de_cliente(contexto):
    """Sequestro de sessão: credenciais VÁLIDAS de outro cliente no meio da
    conversa não podem trocar o titular."""
    autenticar_cliente(contexto, CPF_ANA, NASC_ANA)
    r = autenticar_cliente(contexto, "39819391903", "1979-07-30")  # Carla
    assert r["dados"]["nome_cliente"] == "Ana Beatriz Cardoso"
    assert contexto.sessao.cpf == CPF_ANA
    assert consultar_limite(contexto)["dados"]["limite_atual"] == 8000.00


def test_nenhum_dado_de_cliente_vaza_na_recusa(contexto):
    r = consultar_limite(contexto)
    assert "limite_atual" not in r["dados"]
    assert "score" not in r["dados"]


# --------------------------------------------------------------------------- #
# 4. Crédito
# --------------------------------------------------------------------------- #


def test_consultar_limite(autenticado):
    r = consultar_limite(autenticado)
    assert r["dados"]["limite_atual"] == 8000.00
    assert r["dados"]["score"] == 720
    assert r["dados"]["limite_maximo_para_o_score"] == 15000.00


def test_aumento_dentro_do_teto_e_aprovado(autenticado):
    r = solicitar_aumento_limite(autenticado, 12000)
    assert r["dados"]["status"] == STATUS_APROVADO
    assert r["dados"]["limite_vigente"] == 12000.00
    assert autenticado.repositorio.obter_cliente(CPF_ANA).limite_atual == 12000.00


def test_aumento_acima_do_teto_e_rejeitado(autenticado):
    r = solicitar_aumento_limite(autenticado, 20000)
    assert r["dados"]["status"] == STATUS_REJEITADO
    assert r["dados"]["pode_oferecer_entrevista"] is True
    # Rejeitado não altera o limite vigente.
    assert autenticado.repositorio.obter_cliente(CPF_ANA).limite_atual == 8000.00


def test_pedido_e_gravado_como_pendente_antes_de_decidir(autenticado):
    """A trilha de auditoria pedida no enunciado."""
    solicitar_aumento_limite(autenticado, 20000)
    solicitacoes = autenticado.repositorio.listar_solicitacoes()
    assert len(solicitacoes) == 1
    assert solicitacoes[0].status_pedido == STATUS_REJEITADO
    assert solicitacoes[0].novo_limite_solicitado == 20000.00


def test_valor_em_linguagem_natural_e_aceito(autenticado):
    r = solicitar_aumento_limite(autenticado, "R$ 12 mil")
    assert r["dados"]["status"] == STATUS_APROVADO
    assert r["dados"]["novo_limite_solicitado"] == 12000.00


@pytest.mark.parametrize("valor", ["muito", None, -100, 0])
def test_valor_invalido_nao_grava_solicitacao(autenticado, valor):
    r = solicitar_aumento_limite(autenticado, valor)
    assert r["status"] == STATUS_ERRO
    assert autenticado.repositorio.listar_solicitacoes() == []


def test_historico_lista_do_mais_recente_ao_mais_antigo(autenticado):
    solicitar_aumento_limite(autenticado, 9000)
    solicitar_aumento_limite(autenticado, 20000)
    r = consultar_historico_solicitacoes(autenticado)
    assert r["dados"]["total"] == 2
    assert r["dados"]["solicitacoes"][0]["novo_limite_solicitado"] == 20000.00


# --------------------------------------------------------------------------- #
# 5. Entrevista
# --------------------------------------------------------------------------- #


def test_respostas_parciais_sao_acumuladas(autenticado):
    r = registrar_resposta_entrevista(autenticado, renda_mensal=8000)
    assert r["status"] == STATUS_OK
    assert r["dados"]["entrevista_completa"] is False
    assert "tipo_emprego" in r["dados"]["faltando"]

    r = registrar_resposta_entrevista(autenticado, tipo_emprego="CLT")
    assert set(r["dados"]["faltando"]) == {
        "despesas_fixas",
        "num_dependentes",
        "tem_dividas",
    }


def test_todas_as_respostas_de_uma_vez(autenticado):
    r = registrar_resposta_entrevista(
        autenticado,
        renda_mensal="R$ 8.000,00",
        tipo_emprego="carteira assinada",
        despesas_fixas=3000,
        num_dependentes=0,
        tem_dividas="não",
    )
    assert r["dados"]["entrevista_completa"] is True


def test_resposta_invalida_e_recusada_sem_perder_as_anteriores(autenticado):
    registrar_resposta_entrevista(autenticado, renda_mensal=8000)
    r = registrar_resposta_entrevista(autenticado, tipo_emprego="aposentado")
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["campo"] == "tipo_emprego"
    assert autenticado.sessao.entrevista.renda_mensal == 8000


def test_finalizar_sem_todas_as_respostas_e_recusado(autenticado):
    registrar_resposta_entrevista(autenticado, renda_mensal=8000)
    r = finalizar_entrevista(autenticado)
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["motivo"] == "entrevista_incompleta"


def test_entrevista_completa_recalcula_e_persiste_o_score(autenticado):
    registrar_resposta_entrevista(
        autenticado,
        renda_mensal=8000,
        tipo_emprego="formal",
        despesas_fixas=3000,
        num_dependentes=0,
        tem_dividas="não",
    )
    r = finalizar_entrevista(autenticado)
    assert r["status"] == STATUS_OK
    # 8000*30/3001 = 79.97 + 300 + 100 + 100 = 580 (mesmo caso de test_score).
    assert r["dados"]["score_novo"] == 580
    assert r["dados"]["score_anterior"] == 720
    assert autenticado.repositorio.obter_cliente(CPF_ANA).score == 580


def test_entrevista_que_melhora_o_score_destrava_o_aumento(contexto):
    """O fluxo Crédito -> Entrevista -> Crédito de ponta a ponta."""
    autenticar_cliente(contexto, CPF_GIOVANA, NASC_GIOVANA)

    # Score 150, teto 500: um pedido de 3000 é rejeitado.
    primeiro = solicitar_aumento_limite(contexto, 3000)
    assert primeiro["dados"]["status"] == STATUS_REJEITADO

    registrar_resposta_entrevista(
        contexto,
        renda_mensal=9000,
        tipo_emprego="formal",
        despesas_fixas=1000,
        num_dependentes=0,
        tem_dividas="não",
    )
    resultado = finalizar_entrevista(contexto)
    assert resultado["dados"]["melhorou"] is True
    assert resultado["dados"]["score_novo"] > 700

    # Mesmo pedido, agora aprovado.
    segundo = solicitar_aumento_limite(contexto, 3000)
    assert segundo["dados"]["status"] == STATUS_APROVADO

    # E as duas solicitações ficaram registradas, com desfechos diferentes.
    assert [s.status_pedido for s in contexto.repositorio.listar_solicitacoes()] == [
        STATUS_REJEITADO,
        STATUS_APROVADO,
    ]


def test_progresso_da_entrevista(autenticado):
    registrar_resposta_entrevista(autenticado, renda_mensal=8000, num_dependentes=2)
    r = consultar_progresso_entrevista(autenticado)
    assert r["dados"]["respondido"] == {"renda_mensal": 8000.0, "num_dependentes": 2}
    assert len(r["dados"]["faltando"]) == 3


def test_score_e_calculado_pelo_nucleo_nao_pela_tool(autenticado):
    """Confere o resultado contra o módulo de score diretamente."""
    from src.core.score import calcular_score

    dados = dict(
        renda_mensal=6000,
        tipo_emprego="autônomo",
        despesas_fixas=2500,
        num_dependentes=1,
        tem_dividas="não",
    )
    registrar_resposta_entrevista(autenticado, **dados)
    r = finalizar_entrevista(autenticado)
    assert r["dados"]["score_novo"] == calcular_score(**dados).score


# --------------------------------------------------------------------------- #
# 6. Câmbio — não exige autenticação, mas respeita o bloqueio
# --------------------------------------------------------------------------- #


class _CotacaoFalsa:
    moeda_origem = "USD"
    moeda_destino = "BRL"
    valor = 5.4210
    descricao = "1 dólar americano = 5,4210 BRL"
    variacao_pct = -0.25
    atualizado_em = "2026-08-28 10:30:00"
    fonte = "AwesomeAPI"


def test_cotacao_nao_exige_autenticacao(contexto, monkeypatch):
    """Cotação é informação pública; exigir login seria atrito sem ganho."""
    monkeypatch.setattr(
        "src.tools.cambio.obter_cotacao", lambda *a, **k: _CotacaoFalsa()
    )
    r = consultar_cotacao(contexto, "dólar")
    assert r["status"] == STATUS_OK
    assert r["dados"]["valor"] == 5.4210


def test_cotacao_recusa_em_sessao_bloqueada(contexto, monkeypatch):
    monkeypatch.setattr(
        "src.tools.cambio.obter_cotacao", lambda *a, **k: _CotacaoFalsa()
    )
    for _ in range(3):
        autenticar_cliente(contexto, CPF_ANA, "01/01/1990")
    assert consultar_cotacao(contexto)["status"] == STATUS_BLOQUEADO


def test_moeda_desconhecida_devolve_erro_amigavel(contexto):
    r = consultar_cotacao(contexto, "batata")
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["motivo"] == "moeda_nao_suportada"
    assert "dólar" in r["mensagem"]


def test_fontes_fora_do_ar_oferecem_alternativa(contexto, monkeypatch):
    def falhar(*args, **kwargs):
        raise cambio_api.CotacaoIndisponivelError("as duas fontes caíram")

    monkeypatch.setattr("src.tools.cambio.obter_cotacao", falhar)
    r = consultar_cotacao(contexto, "dólar")
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["motivo"] == "cotacao_indisponivel"
    # O enunciado pede alternativa, não interrupção abrupta.
    assert "outro assunto" in r["mensagem"]


def test_erro_inesperado_no_cambio_nao_derruba_o_atendimento(contexto, monkeypatch):
    def explodir(*args, **kwargs):
        raise RuntimeError("erro não previsto")

    monkeypatch.setattr("src.tools.cambio.obter_cotacao", explodir)
    r = consultar_cotacao(contexto, "dólar")
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["motivo"] == "falha_interna"


# --------------------------------------------------------------------------- #
# 7. Encerramento
# --------------------------------------------------------------------------- #


def test_encerrar_marca_a_sessao(autenticado):
    r = encerrar_atendimento(autenticado)
    assert r["status"] == STATUS_OK
    assert autenticado.sessao.encerrado is True


def test_encerrar_nao_exige_autenticacao(contexto):
    """O cliente pode desistir antes de se identificar."""
    assert encerrar_atendimento(contexto)["status"] == STATUS_OK


def test_encerrar_duas_vezes_e_idempotente(autenticado):
    encerrar_atendimento(autenticado)
    r = encerrar_atendimento(autenticado)
    assert r["dados"]["ja_encerrado"] is True


# --------------------------------------------------------------------------- #
# 8. Conversão de valor — a aritmética que o agente não pode fazer
# --------------------------------------------------------------------------- #


class _CotacaoBRLUSD:
    moeda_origem = "BRL"
    moeda_destino = "USD"
    valor = 0.1914
    descricao = "1 real = 0,1914 USD"
    variacao_pct = None
    atualizado_em = "2026-08-28 12:00:00"
    fonte = "AwesomeAPI"


def test_converte_limite_para_dolar(contexto, monkeypatch):
    """O caso que motivou a tool: o cliente pede o limite em dólar."""
    monkeypatch.setattr(
        "src.tools.cambio.obter_cotacao", lambda *a, **k: _CotacaoBRLUSD()
    )
    r = converter_valor(contexto, "8000", "BRL", "USD")
    assert r["status"] == STATUS_OK
    assert r["dados"]["valor_convertido"] == 1531.20
    assert r["dados"]["moeda_destino"] == "USD"


def test_descricao_traz_o_total_e_nao_so_a_cotacao(contexto, monkeypatch):
    """A queixa original era receber só o preço unitário."""
    monkeypatch.setattr(
        "src.tools.cambio.obter_cotacao", lambda *a, **k: _CotacaoBRLUSD()
    )
    descricao = converter_valor(contexto, "8000", "BRL", "USD")["dados"]["descricao"]
    assert descricao == "R$ 8.000,00 = US$ 1.531,20"


def test_conversao_aceita_valor_em_linguagem_natural(contexto, monkeypatch):
    monkeypatch.setattr(
        "src.tools.cambio.obter_cotacao", lambda *a, **k: _CotacaoBRLUSD()
    )
    r = converter_valor(contexto, "R$ 8 mil", "BRL", "USD")
    assert r["dados"]["valor_convertido"] == 1531.20


def test_conversao_nao_exige_autenticacao(contexto, monkeypatch):
    """Converter um número informado não expõe dado de conta."""
    monkeypatch.setattr(
        "src.tools.cambio.obter_cotacao", lambda *a, **k: _CotacaoBRLUSD()
    )
    assert contexto.sessao.autenticado is False
    assert converter_valor(contexto, "100")["status"] == STATUS_OK


def test_conversao_recusa_em_sessao_bloqueada(contexto):
    for _ in range(3):
        autenticar_cliente(contexto, CPF_ANA, "01/01/1990")
    assert converter_valor(contexto, "100")["status"] == STATUS_BLOQUEADO


@pytest.mark.parametrize("valor", ["muito", None, -50])
def test_valor_invalido_para_conversao(contexto, valor):
    r = converter_valor(contexto, valor)
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["motivo"] == "valor_invalido"


def test_moedas_iguais_e_recusado(contexto):
    r = converter_valor(contexto, "100", "BRL", "BRL")
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["motivo"] == "moedas_iguais"


def test_moeda_desconhecida_na_conversao(contexto):
    r = converter_valor(contexto, "100", "BRL", "batata")
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["motivo"] == "moeda_nao_suportada"


def test_cotacao_indisponivel_na_conversao(contexto, monkeypatch):
    def falhar(*args, **kwargs):
        raise cambio_api.CotacaoIndisponivelError("as duas fontes caíram")

    monkeypatch.setattr("src.tools.cambio.obter_cotacao", falhar)
    r = converter_valor(contexto, "100")
    assert r["dados"]["motivo"] == "cotacao_indisponivel"
    assert "outro assunto" in r["mensagem"]


def test_cotacao_zero_nao_zera_o_dinheiro_do_cliente(contexto, monkeypatch):
    """Dado ruim da fonte deve falhar, não devolver montante zero."""

    class _CotacaoZerada(_CotacaoBRLUSD):
        valor = 0.0

    monkeypatch.setattr(
        "src.tools.cambio.obter_cotacao", lambda *a, **k: _CotacaoZerada()
    )
    r = converter_valor(contexto, "8000")
    assert r["status"] == STATUS_ERRO
    assert r["dados"]["motivo"] == "cotacao_invalida"


def test_cambio_recusa_por_decorator_e_deixa_rastro(contexto, caplog):
    """A checagem do câmbio virou decorator: antes era copiada à mão em cada
    tool e não registrava a recusa em log."""
    import logging

    for _ in range(3):
        autenticar_cliente(contexto, CPF_ANA, "01/01/1990")

    with caplog.at_level(logging.WARNING, logger="src.tools.base"):
        assert consultar_cotacao(contexto)["status"] == STATUS_BLOQUEADO
        assert converter_valor(contexto, "100")["status"] == STATUS_BLOQUEADO

    assert "consultar_cotacao" in caplog.text
    assert "converter_valor" in caplog.text


# --------------------------------------------------------------------------- #
# 9. Trilha de auditoria — ADR-007
# --------------------------------------------------------------------------- #


def test_pedido_grava_o_score_que_embasou_a_decisao(autenticado):
    """A base do julgamento entra junto com o pedido pendente."""
    solicitar_aumento_limite(autenticado, 12000)
    registro = autenticado.repositorio.listar_solicitacoes()[0]
    assert registro.score_na_decisao == 720  # score da Ana na base semente


def test_pedidos_iguais_com_desfechos_opostos_sao_explicaveis(contexto):
    """O caso que motivou o ADR-007, de ponta a ponta.

    Mesmo CPF, mesmo limite atual, mesmo valor pedido, desfechos opostos. O
    que distingue as duas linhas — e explica a diferença — é o score.
    """
    autenticar_cliente(contexto, CPF_GIOVANA, NASC_GIOVANA)
    solicitar_aumento_limite(contexto, 3000)  # score 150, teto 500 -> rejeitado

    registrar_resposta_entrevista(
        contexto,
        renda_mensal=9000,
        tipo_emprego="formal",
        despesas_fixas=1000,
        num_dependentes=0,
        tem_dividas="não",
    )
    finalizar_entrevista(contexto)
    solicitar_aumento_limite(contexto, 3000)  # score 770, teto 15000 -> aprovado

    primeiro, segundo = contexto.repositorio.listar_solicitacoes()

    # Idênticos em tudo o que o enunciado prescreve...
    assert primeiro.limite_atual == segundo.limite_atual
    assert primeiro.novo_limite_solicitado == segundo.novo_limite_solicitado
    # ...e ainda assim com desfechos opostos, explicados pelo score.
    assert (primeiro.status_pedido, segundo.status_pedido) == (
        STATUS_REJEITADO,
        STATUS_APROVADO,
    )
    assert primeiro.score_na_decisao == 150
    assert segundo.score_na_decisao == 770


def test_score_invalido_na_trilha_e_recusado():
    """Um score fora da faixa tornaria a linha inauditável."""
    from src.data.models import DadosInvalidosError, Solicitacao

    with pytest.raises(DadosInvalidosError, match="score_na_decisao"):
        Solicitacao.nova(CPF_ANA, 8000, 12000, score_na_decisao=1500)
