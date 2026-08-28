"""
Tools de crédito: consulta de limite e solicitação de aumento.

A decisão aprovado/rejeitado vem inteira de `src/core/limites.py`. Aqui só há
orquestração: validar entrada, gravar o pedido como `pendente`, pedir a decisão
ao núcleo, transicionar o status e devolver `dict`.

A ordem gravar-depois-decidir é deliberada (seção 4 do CLAUDE.md): o enunciado
pede o registro do pedido formal ANTES da checagem de score. Gravar já decidido
perderia a trilha de auditoria.
"""

from __future__ import annotations

import logging

from src.core.limites import avaliar_solicitacao, limite_permitido
from src.core.validadores import (
    ValorMonetarioInvalidoError,
    mascarar_cpf,
    normalizar_valor_monetario,
)
from src.data.models import Solicitacao
from src.tools.base import (
    ContextoAtendimento,
    Resposta,
    erro,
    exige_sessao_ativa,
    ok,
    tratar_falhas,
)

logger = logging.getLogger(__name__)


@tratar_falhas
@exige_sessao_ativa
def consultar_limite(contexto: ContextoAtendimento) -> Resposta:
    """Limite atual do cliente e o teto permitido para o score dele."""
    cliente = contexto.repositorio.obter_cliente(contexto.sessao.cpf)
    faixas = contexto.repositorio.carregar_faixas_score()
    teto = limite_permitido(cliente.score, faixas)

    return ok(
        "Limite consultado.",
        limite_atual=cliente.limite_atual,
        score=cliente.score,
        limite_maximo_para_o_score=teto,
        ja_esta_no_teto=cliente.limite_atual >= teto,
    )


@tratar_falhas
@exige_sessao_ativa
def solicitar_aumento_limite(
    contexto: ContextoAtendimento, novo_limite: object
) -> Resposta:
    """Registra o pedido formal e devolve a decisão.

    Fluxo: valida o valor -> grava `pendente` -> decide via núcleo ->
    transiciona o status -> aplica o novo limite se aprovado.
    """
    sessao = contexto.sessao
    repositorio = contexto.repositorio

    # --- 1. validação da entrada ----------------------------------------- #
    try:
        valor = normalizar_valor_monetario(novo_limite, nome="Novo limite")
    except ValorMonetarioInvalidoError as exc:
        return erro(
            "Não consegui entender o valor solicitado. "
            "Pode informar apenas o número, por exemplo 12000?",
            motivo="valor_invalido",
            detalhe=str(exc),
        )

    if valor <= 0:
        return erro(
            "O novo limite precisa ser um valor maior que zero.",
            motivo="valor_invalido",
        )

    cliente = repositorio.obter_cliente(sessao.cpf)

    # --- 2. pedido formal, sempre pendente primeiro ----------------------- #
    solicitacao = repositorio.registrar_solicitacao(
        Solicitacao.nova(
            cpf_cliente=cliente.cpf,
            limite_atual=cliente.limite_atual,
            novo_limite_solicitado=valor,
        )
    )
    sessao.ultima_solicitacao_timestamp = solicitacao.data_hora_solicitacao

    # --- 3. decisão (regra de negócio pura) ------------------------------- #
    faixas = repositorio.carregar_faixas_score()
    decisao = avaliar_solicitacao(
        score=cliente.score,
        limite_atual=cliente.limite_atual,
        novo_limite_solicitado=valor,
        faixas=faixas,
    )

    # --- 4. transição do status ------------------------------------------- #
    repositorio.atualizar_status_solicitacao(
        cliente.cpf, solicitacao.data_hora_solicitacao, decisao.status
    )

    # --- 5. efeito da aprovação ------------------------------------------- #
    if decisao.aprovado:
        cliente = repositorio.atualizar_limite(cliente.cpf, valor)
        sessao.ultimo_pedido_rejeitado = False
        logger.info(
            "Aumento aprovado para o cpf %s: novo limite %.2f.",
            mascarar_cpf(cliente.cpf),
            valor,
        )
        return ok(
            "Solicitação aprovada.",
            **decisao.as_dict(),
            limite_vigente=cliente.limite_atual,
        )

    sessao.ultimo_pedido_rejeitado = True
    logger.info(
        "Aumento rejeitado para o cpf %s: pediu %.2f, teto %.2f.",
        mascarar_cpf(cliente.cpf),
        valor,
        decisao.limite_permitido,
    )
    return ok(
        "Solicitação rejeitada.",
        **decisao.as_dict(),
        limite_vigente=cliente.limite_atual,
        # Sinaliza ao agente que cabe oferecer a entrevista (item 3 do
        # enunciado). Quem decide oferecer é o prompt; o dado vem do código.
        pode_oferecer_entrevista=True,
    )


@tratar_falhas
@exige_sessao_ativa
def consultar_historico_solicitacoes(contexto: ContextoAtendimento) -> Resposta:
    """Pedidos anteriores do cliente, do mais recente para o mais antigo."""
    solicitacoes = contexto.repositorio.listar_solicitacoes_do_cliente(
        contexto.sessao.cpf
    )
    registros = [
        {
            "data_hora": s.data_hora_solicitacao,
            "limite_atual": s.limite_atual,
            "novo_limite_solicitado": s.novo_limite_solicitado,
            "status": s.status_pedido,
        }
        for s in reversed(solicitacoes)
    ]
    return ok(
        "Histórico consultado.",
        total=len(registros),
        solicitacoes=registros,
    )
