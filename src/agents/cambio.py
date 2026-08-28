"""
Agente de Câmbio — cotação de moedas via API externa (ADR-005).

Único agente que opera sem autenticação: cotação é informação pública.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from src.agents.base import carregar_prompt, tools_comuns
from src.tools import consultar_cotacao as _consultar_cotacao
from src.tools.base import ContextoAtendimento

NOME = "cambio"


def prompt() -> str:
    return carregar_prompt(NOME)


def construir_tools(contexto: ContextoAtendimento) -> list[BaseTool]:
    @tool("consultar_cotacao")
    def consultar_cotacao(
        moeda: str = "USD", moeda_destino: str = "BRL"
    ) -> dict[str, Any]:
        """Consulta a cotação atual de uma moeda em tempo real.

        Devolve o campo 'descricao' com o valor já formatado em padrão
        brasileiro — use esse texto como veio, sem reformatar nem arredondar.

        Args:
            moeda: moeda a consultar, ex. 'dólar', 'euro', 'USD', 'EUR'.
                Se o cliente não especificar, use 'USD'.
            moeda_destino: moeda de referência, normalmente 'BRL'.
        """
        return _consultar_cotacao(contexto, moeda, moeda_destino)

    return [consultar_cotacao, *tools_comuns(contexto)]
