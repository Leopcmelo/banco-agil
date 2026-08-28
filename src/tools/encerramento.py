"""
Tool de encerramento.

O enunciado é explícito: "a qualquer momento, se o usuário solicitar o fim da
conversa, o agente deve chamar a ferramenta de encerramento para finalizar o
loop de execução". Quem encerra o loop é o código; o agente só se despede.

Não exige autenticação — o cliente pode desistir antes de se identificar.
"""

from __future__ import annotations

import logging

from src.tools.base import ContextoAtendimento, Resposta, ok, tratar_falhas

logger = logging.getLogger(__name__)

MOTIVO_PEDIDO_DO_CLIENTE = "pedido_do_cliente"
MOTIVO_AUTENTICACAO_BLOQUEADA = "autenticacao_bloqueada"


@tratar_falhas
def encerrar_atendimento(
    contexto: ContextoAtendimento,
    motivo: str = MOTIVO_PEDIDO_DO_CLIENTE,
) -> Resposta:
    """Marca a sessão como encerrada e interrompe o loop de execução."""
    sessao = contexto.sessao

    if sessao.encerrado:
        return ok(
            "Atendimento já estava encerrado.",
            motivo=sessao.motivo_encerramento,
            ja_encerrado=True,
        )

    sessao.encerrar(motivo)
    logger.info("Atendimento encerrado (%s).", motivo)
    return ok(
        "Atendimento encerrado.",
        motivo=motivo,
        ja_encerrado=False,
    )
