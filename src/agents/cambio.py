"""
Agente de Câmbio — cotação de moedas via API externa (ADR-005).

Único agente que opera sem autenticação: cotação é informação pública.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from src.agents.base import carregar_prompt, tools_comuns
from src.tools import consultar_cotacao as _consultar_cotacao
from src.tools import converter_valor as _converter_valor
from src.tools.base import ContextoAtendimento

NOME = "cambio"
TITULO = "Câmbio"


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

    @tool("converter_valor")
    def converter_valor(
        valor: str, moeda_origem: str = "BRL", moeda_destino: str = "USD"
    ) -> dict[str, Any]:
        """Converte um MONTANTE de uma moeda para outra e devolve o total.

        Use sempre que o cliente quiser saber quanto um valor específico dá em
        outra moeda — "quanto é 100 dólares em reais?", "converta meu limite
        para dólar". Você NUNCA multiplica por conta própria: chame esta
        ferramenta e repita o texto de 'descricao'.

        Para converter o limite de crédito, use o valor que já apareceu na
        conversa — não peça o número de novo ao cliente.

        Args:
            valor: montante a converter, ex. '8000' ou 'R$ 8.000,00'.
            moeda_origem: moeda em que o valor está hoje. Para o limite de
                crédito e qualquer valor em reais, use 'BRL'.
            moeda_destino: moeda para a qual converter, ex. 'USD', 'EUR'.
        """
        return _converter_valor(contexto, valor, moeda_origem, moeda_destino)

    return [consultar_cotacao, converter_valor, *tools_comuns(contexto)]
