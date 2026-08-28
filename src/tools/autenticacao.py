"""
Tool de autenticação — a única que pode marcar a sessão como autenticada.

ADR-003: 3 tentativas no total. O contador é incrementado AQUI, nunca pelo LLM.
Ao esgotar, a sessão é bloqueada e todas as demais tools passam a recusar.
"""

from __future__ import annotations

import logging

from src.core.validadores import (
    CPFInvalidoError,
    DataInvalidaError,
    datas_conferem,
    mascarar_cpf,
    normalizar_cpf,
    normalizar_data_nascimento,
)
from src.tools.base import (
    ContextoAtendimento,
    Resposta,
    bloqueado,
    erro,
    ok,
    tratar_falhas,
)

logger = logging.getLogger(__name__)


@tratar_falhas
def autenticar_cliente(
    contexto: ContextoAtendimento, cpf: str, data_nascimento: str
) -> Resposta:
    """Confere CPF e data de nascimento contra `clientes.csv`.

    Uma tentativa é consumida a cada chamada com credenciais que não conferem —
    inclusive quando o CPF é malformado, porque do ponto de vista do
    atendimento é uma tentativa falha como qualquer outra.
    """
    sessao = contexto.sessao

    if sessao.bloqueado:
        return bloqueado(
            "Não foi possível confirmar a identidade após três tentativas.",
            motivo="autenticacao_bloqueada",
            tentativas_restantes=0,
        )

    if sessao.autenticado:
        # Idempotente: reautenticar não consome tentativa nem troca de cliente.
        return ok(
            "Cliente já autenticado nesta sessão.",
            nome_cliente=sessao.nome_cliente,
            ja_autenticado=True,
        )

    # --- validação de formato ------------------------------------------- #
    try:
        cpf_normalizado = normalizar_cpf(cpf)
    except CPFInvalidoError as exc:
        logger.info("CPF malformado na autenticação (%s): %s", mascarar_cpf(cpf), exc)
        return _falha(contexto, "cpf_invalido", str(exc))

    try:
        nascimento_informado = normalizar_data_nascimento(data_nascimento)
    except DataInvalidaError as exc:
        logger.info("Data malformada na autenticação: %s", exc)
        return _falha(contexto, "data_invalida", str(exc))

    # --- conferência contra a base --------------------------------------- #
    cliente = contexto.repositorio.buscar_cliente(cpf_normalizado)
    if cliente is None:
        logger.info(
            "Autenticação falhou: cpf %s não encontrado.",
            mascarar_cpf(cpf_normalizado),
        )
        return _falha(contexto, "credenciais_incorretas", "Dados não conferem.")

    if not datas_conferem(nascimento_informado, cliente.data_nascimento):
        logger.info(
            "Autenticação falhou: data não confere para o cpf %s.",
            mascarar_cpf(cpf_normalizado),
        )
        return _falha(contexto, "credenciais_incorretas", "Dados não conferem.")

    # --- sucesso ---------------------------------------------------------- #
    sessao.autenticar(cliente.cpf, cliente.nome)
    logger.info("Cliente %s autenticado com sucesso.", mascarar_cpf(cliente.cpf))
    return ok(
        "Autenticação confirmada.",
        nome_cliente=cliente.nome,
        primeiro_nome=cliente.primeiro_nome,
        ja_autenticado=False,
    )


def _falha(contexto: ContextoAtendimento, motivo: str, detalhe: str) -> Resposta:
    """Consome uma tentativa e devolve `erro` ou `bloqueado`.

    A mensagem nunca diz QUAL dado está errado: informar que "o CPF existe mas
    a data não confere" entregaria de graça a validade de um CPF.
    """
    restantes = contexto.sessao.registrar_tentativa()

    if contexto.sessao.bloqueado:
        logger.warning("Sessão bloqueada após %d tentativas de autenticação.",
                       contexto.sessao.tentativas_auth)
        return bloqueado(
            "Não foi possível confirmar a identidade após três tentativas.",
            motivo="autenticacao_bloqueada",
            detalhe=detalhe,
            tentativas_restantes=0,
        )

    return erro(
        "Os dados informados não conferem com o nosso cadastro.",
        motivo=motivo,
        detalhe=detalhe,
        tentativas_restantes=restantes,
    )
