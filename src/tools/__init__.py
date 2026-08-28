"""Camada de tools: wrappers finos entre os agentes e as regras de negócio."""

from src.tools.autenticacao import autenticar_cliente
from src.tools.base import (
    STATUS_BLOQUEADO,
    STATUS_ERRO,
    STATUS_OK,
    ContextoAtendimento,
    Resposta,
)
from src.tools.cambio import consultar_cotacao
from src.tools.credito import (
    consultar_historico_solicitacoes,
    consultar_limite,
    solicitar_aumento_limite,
)
from src.tools.encerramento import encerrar_atendimento
from src.tools.entrevista import (
    consultar_progresso_entrevista,
    finalizar_entrevista,
    registrar_resposta_entrevista,
)

__all__ = [
    "STATUS_BLOQUEADO",
    "STATUS_ERRO",
    "STATUS_OK",
    "ContextoAtendimento",
    "Resposta",
    "autenticar_cliente",
    "consultar_cotacao",
    "consultar_historico_solicitacoes",
    "consultar_limite",
    "consultar_progresso_entrevista",
    "encerrar_atendimento",
    "finalizar_entrevista",
    "registrar_resposta_entrevista",
    "solicitar_aumento_limite",
]
