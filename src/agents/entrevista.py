"""
Agente de Entrevista de Crédito — coleta dados financeiros e recalcula o score.

Coleta as respostas em linguagem natural; a interpretação e o cálculo ficam
inteiramente no código (`src/core/score.py`).
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from src.agents.base import carregar_prompt, texto_ou_none, tools_comuns
from src.tools import (
    consultar_progresso_entrevista as _consultar_progresso,
)
from src.tools import finalizar_entrevista as _finalizar_entrevista
from src.tools import registrar_resposta_entrevista as _registrar_resposta
from src.tools.base import ContextoAtendimento

NOME = "entrevista"
TITULO = "Entrevista de Crédito"


def prompt() -> str:
    return carregar_prompt(NOME)


def construir_tools(contexto: ContextoAtendimento) -> list[BaseTool]:
    @tool("registrar_resposta_entrevista")
    def registrar_resposta_entrevista(
        renda_mensal: str = "",
        tipo_emprego: str = "",
        despesas_fixas: str = "",
        num_dependentes: str = "",
        tem_dividas: str = "",
    ) -> dict[str, Any]:
        """Registra uma ou mais respostas da entrevista financeira.

        Passe SOMENTE os campos que o cliente acabou de responder; deixe os
        demais vazios. A ferramenta interpreta linguagem natural ('uns 8 mil',
        'CLT', 'não tenho nenhuma') e devolve em 'faltando' o que ainda
        precisa ser perguntado.

        Args:
            renda_mensal: renda mensal informada, ex. '8000' ou 'uns 8 mil'.
            tipo_emprego: 'formal', 'autônomo' ou 'desempregado' (aceita
                sinônimos como CLT, MEI, freelancer).
            despesas_fixas: despesas fixas mensais, ex. '3000'.
            num_dependentes: número de dependentes, ex. '0', '2', '3'.
            tem_dividas: 'sim' ou 'não'.
        """
        return _registrar_resposta(
            contexto,
            renda_mensal=texto_ou_none(renda_mensal),
            tipo_emprego=texto_ou_none(tipo_emprego),
            despesas_fixas=texto_ou_none(despesas_fixas),
            num_dependentes=texto_ou_none(num_dependentes),
            tem_dividas=texto_ou_none(tem_dividas),
        )

    @tool("finalizar_entrevista")
    def finalizar_entrevista() -> dict[str, Any]:
        """Calcula o novo score e o salva na base de clientes.

        Só funciona com as cinco respostas registradas. Você NÃO calcula o
        score: apenas informe o número que vier em 'score_novo'.
        """
        return _finalizar_entrevista(contexto)

    @tool("consultar_progresso_entrevista")
    def consultar_progresso_entrevista() -> dict[str, Any]:
        """Mostra o que já foi respondido e o que ainda falta perguntar.

        Use se perder o fio da conversa, para não repetir uma pergunta.
        """
        return _consultar_progresso(contexto)

    return [
        registrar_resposta_entrevista,
        finalizar_entrevista,
        consultar_progresso_entrevista,
        *tools_comuns(contexto),
    ]
