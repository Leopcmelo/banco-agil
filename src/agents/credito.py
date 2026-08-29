"""
Agente de Crédito — consulta de limite e solicitação de aumento.

Não conduz entrevista e não dá cotação: quando o assunto muda, transfere.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from src.agents.base import carregar_prompt, tools_comuns
from src.tools import (
    consultar_historico_solicitacoes as _consultar_historico,
)
from src.tools import consultar_limite as _consultar_limite
from src.tools import solicitar_aumento_limite as _solicitar_aumento
from src.tools.base import ContextoAtendimento

NOME = "credito"
TITULO = "Crédito"


def prompt() -> str:
    return carregar_prompt(NOME)


def construir_tools(contexto: ContextoAtendimento) -> list[BaseTool]:
    @tool("consultar_limite")
    def consultar_limite() -> dict[str, Any]:
        """Consulta o limite de crédito atual do cliente autenticado.

        Devolve também o score e o teto permitido para esse score. Informe ao
        cliente apenas o limite atual, a menos que ele pergunte pelo score.
        """
        return _consultar_limite(contexto)

    @tool("solicitar_aumento_limite")
    def solicitar_aumento_limite(novo_limite: str) -> dict[str, Any]:
        """Registra o pedido formal de aumento e devolve a decisão.

        A ferramenta grava a solicitação, avalia o score contra a tabela de
        limites e devolve 'aprovado' ou 'rejeitado'. Você NÃO avalia nada:
        apenas comunique o resultado que vier.

        Args:
            novo_limite: valor desejado, como o cliente informou. Pode vir
                como '12000', 'R$ 12.000,00' ou '12 mil'.
        """
        return _solicitar_aumento(contexto, novo_limite)

    @tool("consultar_historico_solicitacoes")
    def consultar_historico_solicitacoes() -> dict[str, Any]:
        """Lista os pedidos de aumento anteriores do cliente, do mais recente
        para o mais antigo. Use apenas se o cliente perguntar pelo histórico.
        """
        return _consultar_historico(contexto)

    return [
        consultar_limite,
        solicitar_aumento_limite,
        consultar_historico_solicitacoes,
        *tools_comuns(contexto),
    ]
