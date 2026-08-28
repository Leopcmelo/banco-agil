"""
Dublês usados pelos testes de conversa.

O objetivo é que o suite rode em CI, offline e sem `GOOGLE_API_KEY`: o modelo
é substituído por um roteiro de respostas fixas. Assim os testes verificam o
que é determinístico — roteamento, autorização e fiação das tools — em vez de
verificar o humor do LLM.
"""

from __future__ import annotations

import re
from typing import Any

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import Field


def fala(texto: str) -> AIMessage:
    """Uma resposta de texto puro, sem chamada de ferramenta."""
    return AIMessage(content=texto)


def chama(nome: str, **args: Any) -> AIMessage:
    """Uma resposta que chama uma ferramenta."""
    return AIMessage(
        content="",
        tool_calls=[
            {"name": nome, "args": args, "id": f"call_{nome}", "type": "tool_call"}
        ],
    )


class LLMRoteirizado(BaseChatModel):
    """Modelo falso que consome um roteiro de respostas em ordem.

    `bind_tools` devolve o próprio objeto, de modo que os quatro nós de agente
    compartilham o mesmo roteiro — é isso que permite escrever uma conversa
    inteira, atravessando agentes, como uma lista única.
    """

    roteiro: list[AIMessage] = Field(default_factory=list)
    # Diagnóstico: o que cada nó viu quando foi chamado.
    prompts_vistos: list[str] = Field(default_factory=list)
    tools_vistas: list[list[str]] = Field(default_factory=list)
    turnos: int = 0

    @property
    def _llm_type(self) -> str:
        return "roteirizado"

    def bind_tools(self, tools: Any, **kwargs: Any) -> LLMRoteirizado:
        self.tools_vistas.append([getattr(t, "name", str(t)) for t in tools])
        return self

    def _generate(
        self,
        messages: list[BaseMessage],
        stop: list[str] | None = None,
        run_manager: Any = None,
        **kwargs: Any,
    ) -> ChatResult:
        if messages and messages[0].type == "system":
            self.prompts_vistos.append(str(messages[0].content))

        self.turnos += 1
        if not self.roteiro:
            raise AssertionError(
                f"O roteiro acabou, mas o grafo pediu mais uma resposta "
                f"(chamada nº {self.turnos}). Faltou um item no roteiro do teste."
            )
        return ChatResult(generations=[ChatGeneration(message=self.roteiro.pop(0))])


# --------------------------------------------------------------------------- #
# Detector de quebra do ADR-004
# --------------------------------------------------------------------------- #

# Marcas que denunciam a transição de agente ao cliente. Se qualquer uma
# aparecer numa fala do assistente, o requisito de transferência implícita foi
# violado. A função é pública de propósito: serve tanto para os testes com
# roteiro quanto para auditar uma conversa real gravada com o Gemini.
MARCAS_DE_TRANSFERENCIA = [
    r"\btransferir\b",
    r"\btransferindo\b",
    r"\btransfer[êe]ncia\b",
    r"\bencaminhar\b",
    r"\bencaminhando\b",
    r"\bredirecionar\b",
    r"\bredirecionando\b",
    r"\bdirecionar\b",
    r"\bvou te passar\b",
    r"\bpassar (?:voc[êe]|o senhor|a senhora) para\b",
    r"\bsetor\b",
    r"\bdepartamento\b",
    r"\b[áa]rea respons[áa]vel\b",
    r"\bequipe respons[áa]vel\b",
    r"\bespecialista\b",
    r"\boutro atendente\b",
    r"\bnosso agente\b",
    r"\bagente de (?:cr[ée]dito|c[âa]mbio|triagem)\b",
    r"\bassistente de (?:cr[ée]dito|c[âa]mbio)\b",
    r"\baguarde enquanto\b",
    r"\bum momento,? (?:vou|enquanto)\b",
]

_MARCAS = [re.compile(p, re.IGNORECASE) for p in MARCAS_DE_TRANSFERENCIA]


def marcas_de_transferencia(texto: str) -> list[str]:
    """Devolve as marcas proibidas encontradas no texto (vazio = tudo certo)."""
    return [m.pattern for m in _MARCAS if m.search(texto or "")]
