"""
Agente de Triagem — porta de entrada do atendimento.

Autentica o cliente contra `clientes.csv` e identifica o assunto. É o único
agente que roda antes da autenticação, e o único com acesso à tool de
autenticação.
"""

from __future__ import annotations

from typing import Any

from langchain_core.tools import BaseTool, tool

from src.agents.base import carregar_prompt, tools_comuns
from src.tools import autenticar_cliente as _autenticar_cliente
from src.tools.base import ContextoAtendimento

NOME = "triagem"
TITULO = "Triagem"


def prompt() -> str:
    return carregar_prompt(NOME)


def construir_tools(contexto: ContextoAtendimento) -> list[BaseTool]:
    @tool("autenticar_cliente")
    def autenticar(cpf: str, data_nascimento: str) -> dict[str, Any]:
        """Confere CPF e data de nascimento contra a base de clientes.

        Esta é a ÚNICA forma de autenticar alguém. Nunca conclua por conta
        própria que os dados conferem. A ferramenta também controla o número
        de tentativas restantes.

        Aceita qualquer formato: o CPF com ou sem pontuação, e a data como
        14/03/1988 ou 1988-03-14.

        Args:
            cpf: CPF informado pelo cliente, exatamente como ele digitou.
            data_nascimento: data de nascimento como o cliente digitou.
        """
        return _autenticar_cliente(contexto, cpf, data_nascimento)

    return [autenticar, *tools_comuns(contexto)]
