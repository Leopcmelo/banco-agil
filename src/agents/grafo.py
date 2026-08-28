"""
Grafo de atendimento — costura os quatro agentes num único atendimento.

Topologia:

    START -> [roteador de entrada] -> triagem | credito | entrevista | cambio
                                          |
                                          v
                                    ferramentas -> (de volta ao agente ativo)
                                          |
                                          v
                                         END

Duas decisões de projeto importantes:

**O roteamento é código, não conversa.** O roteador de entrada é uma função
Python que lê `SessionState`: sem autenticação, só a triagem roda; com sessão
bloqueada ou encerrada, nada roda. O LLM não tem como se declarar autenticado
para pular etapa (regra inviolável nº 6).

**A troca de agente é implícita (ADR-004).** O agente ativo chama
`direcionar_atendimento`, o nó de ferramentas percebe a chamada e troca
`estado["agente"]`. O cliente não vê nada disso: os prompts proíbem anunciar
transferência, e há teste de conversa que falha se alguma resposta contiver
marcas do tipo "vou te transferir".

Não existe um quinto agente nem um orquestrador: o grafo é a costura, e cada
nó é um dos quatro agentes do enunciado.
"""

from __future__ import annotations

import logging
import os
from typing import Annotated, Any, TypedDict

from langchain_core.language_models import BaseChatModel
from langchain_core.messages import AIMessage, AnyMessage, SystemMessage, ToolMessage
from langchain_core.tools import BaseTool
from langgraph.graph import END, START, StateGraph
from langgraph.graph.message import add_messages

from src.agents import cambio, credito, entrevista, triagem
from src.agents.base import ASSUNTOS_VALIDOS, TOOL_DIRECIONAR
from src.tools.base import ContextoAtendimento

logger = logging.getLogger(__name__)

NO_FERRAMENTAS = "ferramentas"

# Cada agente do enunciado é um nó. A ordem aqui é a ordem do fluxo natural.
AGENTES = {
    triagem.NOME: triagem,
    credito.NOME: credito,
    entrevista.NOME: entrevista,
    cambio.NOME: cambio,
}

# Teto de idas e voltas entre agente e ferramentas dentro de um mesmo turno.
# Existe para que um modelo em laço não rode indefinidamente.
LIMITE_RECURSAO = 25


class EstadoAtendimento(TypedDict):
    """Estado que atravessa o grafo.

    `ContextoAtendimento` deliberadamente NÃO está aqui: sessão e repositório
    são capturados por closure na construção do grafo. Colocá-los no estado
    exigiria serializá-los a cada passo, sem ganho nenhum.
    """

    messages: Annotated[list[AnyMessage], add_messages]
    agente: str


# --------------------------------------------------------------------------- #
# Modelo
# --------------------------------------------------------------------------- #


def criar_llm(**kwargs: Any) -> BaseChatModel:
    """Instancia o Gemini a partir do `.env`.

    Importado aqui dentro para que o pacote de agentes possa ser carregado —
    e testado — sem nenhuma chave configurada.
    """
    from langchain_google_genai import ChatGoogleGenerativeAI

    chave = os.getenv("GOOGLE_API_KEY")
    if not chave:
        raise RuntimeError(
            "GOOGLE_API_KEY não configurada. Copie .env.example para .env e "
            "preencha a chave obtida em https://aistudio.google.com/apikey."
        )

    parametros: dict[str, Any] = {
        "model": os.getenv("BANCO_AGIL_MODELO", "gemini-2.5-flash"),
        # Temperatura baixa de propósito: o agente conversa, quem decide é o
        # código. Criatividade aqui só produz número inventado.
        "temperature": float(os.getenv("BANCO_AGIL_TEMPERATURA", "0.2")),
        "google_api_key": chave,
    }
    parametros.update(kwargs)
    return ChatGoogleGenerativeAI(**parametros)


# --------------------------------------------------------------------------- #
# Nós
# --------------------------------------------------------------------------- #


def _criar_no_agente(
    nome: str, llm: BaseChatModel, tools: list[BaseTool], instrucoes: str
):
    """Nó de um agente: prompt do sistema + histórico -> resposta do modelo."""
    modelo = llm.bind_tools(tools)

    def no_agente(estado: EstadoAtendimento) -> dict[str, Any]:
        mensagens = [SystemMessage(content=instrucoes), *estado["messages"]]
        resposta = modelo.invoke(mensagens)
        logger.debug(
            "Agente %s respondeu (%d tool calls).",
            nome,
            len(getattr(resposta, "tool_calls", []) or []),
        )
        return {"messages": [resposta]}

    return no_agente


def _criar_no_ferramentas(tools_por_agente: dict[str, list[BaseTool]]):
    """Executa as tools pedidas e trata a transferência implícita.

    Escrito à mão em vez de usar `ToolNode` porque precisa de duas coisas que
    o nó pronto não dá: escolher o conjunto de tools do agente ativo e trocar
    o agente quando `direcionar_atendimento` for chamada.
    """

    def no_ferramentas(estado: EstadoAtendimento) -> dict[str, Any]:
        ultima = estado["messages"][-1]
        chamadas = getattr(ultima, "tool_calls", None) or []
        agente_atual = estado["agente"]
        por_nome = {t.name: t for t in tools_por_agente[agente_atual]}

        respostas: list[ToolMessage] = []
        proximo_agente = agente_atual

        for chamada in chamadas:
            nome = chamada["name"]
            ferramenta = por_nome.get(nome)

            if ferramenta is None:
                # O modelo alucinou uma tool que este agente não tem. Devolver
                # o erro como ToolMessage deixa ele se corrigir no próximo passo.
                logger.warning(
                    "Agente %s chamou tool inexistente: %s", agente_atual, nome
                )
                respostas.append(
                    ToolMessage(
                        content=str(
                            {
                                "status": "erro",
                                "dados": {"motivo": "tool_inexistente"},
                                "mensagem": (
                                    f"A ferramenta {nome} não está disponível "
                                    f"neste momento."
                                ),
                            }
                        ),
                        tool_call_id=chamada["id"],
                        name=nome,
                    )
                )
                continue

            try:
                resultado = ferramenta.invoke(chamada["args"])
            except Exception:
                # As tools já tratam as próprias falhas; isto cobre erro de
                # schema. Nada de `except: pass` — logamos com stack trace.
                logger.exception("Falha ao executar a tool %s.", nome)
                resultado = {
                    "status": "erro",
                    "dados": {"motivo": "falha_interna"},
                    "mensagem": (
                        "Tive um problema técnico ao processar essa "
                        "informação. Podemos tentar de novo?"
                    ),
                }

            respostas.append(
                ToolMessage(
                    content=str(resultado), tool_call_id=chamada["id"], name=nome
                )
            )

            if nome == TOOL_DIRECIONAR:
                destino = str(chamada["args"].get("assunto", "")).strip().lower()
                if destino in ASSUNTOS_VALIDOS:
                    proximo_agente = destino
                    logger.info(
                        "Transferência implícita: %s -> %s", agente_atual, destino
                    )
                else:
                    logger.warning("Assunto de transferência inválido: %r", destino)

        return {"messages": respostas, "agente": proximo_agente}

    return no_ferramentas


# --------------------------------------------------------------------------- #
# Roteadores — todos em código puro
# --------------------------------------------------------------------------- #


def _criar_roteador_entrada(contexto: ContextoAtendimento):
    """Escolhe quem atende este turno, olhando o estado da sessão."""

    def roteador_entrada(estado: EstadoAtendimento) -> str:
        sessao = contexto.sessao

        # Sessão encerrada ou bloqueada não fala mais. A despedida já foi dita
        # no turno em que o encerramento aconteceu.
        if sessao.encerrado or sessao.bloqueado:
            return END

        # Sem autenticação, só a triagem roda — independentemente do que o
        # histórico da conversa sugira.
        if not sessao.autenticado:
            return triagem.NOME

        agente = estado.get("agente") or triagem.NOME
        return agente if agente in AGENTES else triagem.NOME

    return roteador_entrada


def _rotear_depois_do_agente(estado: EstadoAtendimento) -> str:
    """Se o agente pediu ferramentas, executa; senão o turno acabou."""
    ultima = estado["messages"][-1]
    if isinstance(ultima, AIMessage) and getattr(ultima, "tool_calls", None):
        return NO_FERRAMENTAS
    return END


def _criar_roteador_depois_das_ferramentas(contexto: ContextoAtendimento):
    """Devolve o controle ao agente ativo, que agora pode falar."""

    def roteador(estado: EstadoAtendimento) -> str:
        # Encerramento pedido pelo cliente: para o loop imediatamente, sem dar
        # ao modelo mais uma chance de falar (o enunciado pede exatamente isso).
        if contexto.sessao.encerrado:
            return END
        agente = estado.get("agente") or triagem.NOME
        return agente if agente in AGENTES else triagem.NOME

    return roteador


# --------------------------------------------------------------------------- #
# Construção
# --------------------------------------------------------------------------- #


def construir_grafo(contexto: ContextoAtendimento, llm: BaseChatModel):
    """Monta e compila o grafo de atendimento.

    `llm` é injetado em vez de criado aqui dentro: é o que permite rodar os
    testes de conversa com um modelo roteirizado, sem chave e sem rede.
    """
    tools_por_agente = {
        nome: modulo.construir_tools(contexto) for nome, modulo in AGENTES.items()
    }

    grafo = StateGraph(EstadoAtendimento)

    for nome, modulo in AGENTES.items():
        grafo.add_node(
            nome,
            _criar_no_agente(nome, llm, tools_por_agente[nome], modulo.prompt()),
        )

    grafo.add_node(NO_FERRAMENTAS, _criar_no_ferramentas(tools_por_agente))

    grafo.add_conditional_edges(
        START,
        _criar_roteador_entrada(contexto),
        {**{n: n for n in AGENTES}, END: END},
    )

    for nome in AGENTES:
        grafo.add_conditional_edges(
            nome,
            _rotear_depois_do_agente,
            {NO_FERRAMENTAS: NO_FERRAMENTAS, END: END},
        )

    grafo.add_conditional_edges(
        NO_FERRAMENTAS,
        _criar_roteador_depois_das_ferramentas(contexto),
        {**{n: n for n in AGENTES}, END: END},
    )

    return grafo.compile()


class Atendimento:
    """Fachada de uso: uma conversa, com o histórico preservado entre turnos.

    É o que a UI do Streamlit consome. Mantém `messages` e `agente` entre as
    chamadas, para que o Streamlit não precise conhecer o formato do estado.
    """

    def __init__(self, contexto: ContextoAtendimento, llm: BaseChatModel) -> None:
        self.contexto = contexto
        self.grafo = construir_grafo(contexto, llm)
        self.estado: EstadoAtendimento = {"messages": [], "agente": triagem.NOME}

    def enviar(self, mensagem: str) -> str:
        """Processa uma mensagem do cliente e devolve a resposta em texto."""
        if self.contexto.sessao.encerrado:
            return ""

        self.estado["messages"] = [*self.estado["messages"], ("user", mensagem)]
        self.estado = self.grafo.invoke(
            self.estado, config={"recursion_limit": LIMITE_RECURSAO}
        )
        return self.ultima_resposta

    @property
    def ultima_resposta(self) -> str:
        """Texto da última fala do assistente, ignorando mensagens de tool."""
        for mensagem in reversed(self.estado["messages"]):
            if isinstance(mensagem, AIMessage) and str(mensagem.content).strip():
                return str(mensagem.content).strip()
        return ""

    @property
    def agente_ativo(self) -> str:
        return self.estado.get("agente", triagem.NOME)
