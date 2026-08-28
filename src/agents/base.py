"""
Infraestrutura comum aos quatro agentes.

Duas responsabilidades:

1. **Carregar prompts de arquivo.** As instruções vivem em `prompts/*.md`, não
   hardcoded (seção 5 do CLAUDE.md). Cada agente recebe `comum.md` seguido do
   seu próprio arquivo.
2. **Envelopar as tools puras como tools do LangChain.** As funções de
   `src/tools/` recebem `ContextoAtendimento` como primeiro argumento; aqui
   elas viram closures que o LLM enxerga sem esse parâmetro.

Toda fronteira com o LLM é de texto: os parâmetros das tools são `str`, e a
conversão para número, data ou booleano acontece dentro do código. É o mesmo
princípio de sempre — o modelo coleta, o código interpreta.
"""

from __future__ import annotations

import functools
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from langchain_core.tools import BaseTool, tool

from src.tools import encerrar_atendimento as _encerrar_atendimento
from src.tools.base import ContextoAtendimento, ok

DIRETORIO_PROMPTS = Path(__file__).parent / "prompts"

ASSUNTOS_VALIDOS = ("triagem", "credito", "entrevista", "cambio")
Assunto = Literal["triagem", "credito", "entrevista", "cambio"]

# Nome da tool de transferência. O grafo procura por ele para saber que o
# agente ativo mudou; por isso está numa constante e não repetido em strings.
TOOL_DIRECIONAR = "direcionar_atendimento"


@functools.cache
def carregar_prompt(nome: str) -> str:
    """Concatena `comum.md` com o prompt do agente.

    Em cache: o Streamlit re-executa o script a cada interação e reler quatro
    arquivos por mensagem seria desperdício puro.
    """
    comum = (DIRETORIO_PROMPTS / "comum.md").read_text(encoding="utf-8")
    especifico = (DIRETORIO_PROMPTS / f"{nome}.md").read_text(encoding="utf-8")
    return f"{comum.strip()}\n\n---\n\n{especifico.strip()}"


def texto_ou_none(valor: str | None) -> str | None:
    """Trata string vazia como ausência.

    Os parâmetros opcionais das tools são `str = ""` em vez de `str | None`
    porque o schema de função do Gemini lida muito melhor com um tipo simples
    do que com uma união anulável.
    """
    if valor is None:
        return None
    texto = str(valor).strip()
    return texto or None


def construir_tool_direcionar(contexto: ContextoAtendimento) -> BaseTool:
    """Tool de transferência implícita entre agentes (ADR-004).

    Não executa regra de negócio: só sinaliza a mudança de assunto. Quem troca
    o agente ativo é o grafo, ao ver esta tool na lista de chamadas.
    """

    @tool(TOOL_DIRECIONAR)
    def direcionar_atendimento(assunto: Assunto) -> dict[str, Any]:
        """Registra o assunto da conversa para seguir com o tratamento certo.

        Ferramenta interna: o cliente NUNCA pode saber que ela existe. Não
        anuncie a mudança, não diga que vai transferir e não se despeça.

        Args:
            assunto: 'credito' para limite de crédito e pedido de aumento,
                'entrevista' para a entrevista financeira que recalcula o
                score, 'cambio' para cotação de moedas, 'triagem' para voltar
                ao início do atendimento.
        """
        return ok("Assunto registrado.", assunto=assunto)

    return direcionar_atendimento


def construir_tool_encerrar(contexto: ContextoAtendimento) -> BaseTool:
    """Tool de encerramento, disponível para os quatro agentes."""

    @tool("encerrar_atendimento")
    def encerrar(motivo: str = "pedido_do_cliente") -> dict[str, Any]:
        """Encerra o atendimento e finaliza o loop de execução.

        Chame quando o cliente pedir para encerrar, se despedir, ou quando a
        autenticação for definitivamente bloqueada.

        Args:
            motivo: 'pedido_do_cliente' ou 'autenticacao_bloqueada'.
        """
        return _encerrar_atendimento(contexto, motivo=motivo)

    return encerrar


def tools_comuns(contexto: ContextoAtendimento) -> list[BaseTool]:
    return [
        construir_tool_direcionar(contexto),
        construir_tool_encerrar(contexto),
    ]


ConstrutorDeTools = Callable[[ContextoAtendimento], list[BaseTool]]
