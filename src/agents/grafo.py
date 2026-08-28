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


PROVEDOR_PADRAO = "anthropic"
MODELO_PADRAO_ANTHROPIC = "claude-opus-5"
MODELO_PADRAO_GOOGLE = "gemini-3.6-flash"

# Teto de tokens por resposta. Precisa ser folgado nos modelos com raciocínio
# ligado por padrão (Claude Opus 5), onde `max_tokens` limita o raciocínio E o
# texto juntos — um teto apertado trunca a resposta no meio.
MAX_TOKENS_PADRAO = 8192


def _temperatura_configurada() -> float | None:
    """Temperatura só quando explicitamente pedida no `.env`.

    Os dois provedores tratam amostragem de forma diferente e ambos reagem mal
    ao valor default: a família gemini-3.x ignora `temperature` e emite aviso a
    cada chamada; o Claude Opus 5 recusa a requisição com 400. Mandar só quando
    o operador pediu resolve os dois casos.
    """
    valor = os.getenv("BANCO_AGIL_TEMPERATURA", "").strip()
    return float(valor) if valor else None


def _criar_llm_anthropic(**kwargs: Any) -> BaseChatModel:
    from langchain_anthropic import ChatAnthropic

    chave = os.getenv("ANTHROPIC_API_KEY")
    if not chave:
        raise RuntimeError(
            "ANTHROPIC_API_KEY não configurada. Copie .env.example para .env e "
            "preencha a chave obtida em "
            "https://console.anthropic.com/settings/keys."
        )

    parametros: dict[str, Any] = {
        "model": os.getenv("BANCO_AGIL_MODELO_ANTHROPIC", MODELO_PADRAO_ANTHROPIC),
        "api_key": chave,
        "max_tokens": int(os.getenv("BANCO_AGIL_MAX_TOKENS", MAX_TOKENS_PADRAO)),
    }

    # Chaves vinculadas a identidade exigem o workspace em toda requisição —
    # sem o header, a API recusa com 400 antes mesmo de olhar o corpo. Chaves
    # comuns de workspace não precisam, então o campo é opcional.
    workspace = os.getenv("ANTHROPIC_WORKSPACE_ID", "").strip()
    if workspace:
        parametros["default_headers"] = {"anthropic-workspace-id": workspace}

    temperatura = _temperatura_configurada()
    if temperatura is not None:
        parametros["temperature"] = temperatura

    parametros.update(kwargs)
    return ChatAnthropic(**parametros)


def _criar_llm_google(**kwargs: Any) -> BaseChatModel:
    from langchain_google_genai import ChatGoogleGenerativeAI

    chave = os.getenv("GOOGLE_API_KEY")
    if not chave:
        raise RuntimeError(
            "GOOGLE_API_KEY não configurada. Copie .env.example para .env e "
            "preencha a chave obtida em https://aistudio.google.com/apikey."
        )

    parametros: dict[str, Any] = {
        "model": os.getenv("BANCO_AGIL_MODELO", MODELO_PADRAO_GOOGLE),
        "google_api_key": chave,
    }

    temperatura = _temperatura_configurada()
    if temperatura is not None:
        parametros["temperature"] = temperatura

    parametros.update(kwargs)
    return ChatGoogleGenerativeAI(**parametros)


PROVEDORES = {
    "anthropic": _criar_llm_anthropic,
    "google": _criar_llm_google,
}


def criar_llm(**kwargs: Any) -> BaseChatModel:
    """Instancia o modelo do provedor configurado em `BANCO_AGIL_PROVEDOR`.

    Dois provedores porque a escolha é de operação, não de arquitetura: o resto
    do sistema fala com `BaseChatModel` e não sabe qual está atrás. Os imports
    ficam dentro de cada função para que o pacote de agentes possa ser carregado
    — e testado — sem nenhuma chave e sem os dois SDKs instalados.
    """
    nome = os.getenv("BANCO_AGIL_PROVEDOR", PROVEDOR_PADRAO).strip().lower()
    construtor = PROVEDORES.get(nome)
    if construtor is None:
        raise RuntimeError(
            f"Provedor de LLM desconhecido: {nome!r}. "
            f"Use um de: {', '.join(sorted(PROVEDORES))}."
        )
    return construtor(**kwargs)


# --------------------------------------------------------------------------- #
# Extração de texto
# --------------------------------------------------------------------------- #


def texto_da_mensagem(mensagem: AnyMessage) -> str:
    """Extrai o texto legível de uma mensagem do modelo.

    `content` nem sempre é `str`. Os modelos Gemini 3.x devolvem uma lista de
    blocos — `[{"type": "text", "text": "...", "extras": {"signature": "..."}}]`
    — e um `str()` ingênuo aqui despejaria o repr do dicionário inteiro, com a
    assinatura criptográfica junto, na cara do cliente.
    """
    conteudo = mensagem.content

    if isinstance(conteudo, str):
        return conteudo.strip()

    if isinstance(conteudo, list):
        partes: list[str] = []
        for bloco in conteudo:
            if isinstance(bloco, str):
                partes.append(bloco)
            # Só blocos de texto interessam: `thinking`, `tool_use` e afins são
            # internos e não devem aparecer na conversa.
            elif (
                isinstance(bloco, dict)
                and bloco.get("type") in (None, "text")
                and "text" in bloco
            ):
                partes.append(str(bloco["text"]))
        return "\n".join(p for p in partes if p.strip()).strip()

    return str(conteudo).strip() if conteudo else ""


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


def _criar_roteador_depois_do_agente(contexto: ContextoAtendimento):
    """Se o agente pediu ferramentas, executa; senão o turno acabou."""

    def roteador(estado: EstadoAtendimento) -> str:
        # Com a sessão encerrada o turno termina aqui, mesmo que o modelo tenha
        # pedido mais ferramentas. É o que fecha o ciclo do encerramento em
        # exatamente um passo: ferramentas -> agente (despedida) -> fim.
        if contexto.sessao.encerrado:
            return END

        ultima = estado["messages"][-1]
        if isinstance(ultima, AIMessage) and getattr(ultima, "tool_calls", None):
            return NO_FERRAMENTAS
        return END

    return roteador


def _criar_roteador_depois_das_ferramentas(contexto: ContextoAtendimento):
    """Devolve o controle ao agente ativo, que agora pode falar."""

    def roteador(estado: EstadoAtendimento) -> str:
        # Mesmo com a sessão encerrada, o agente volta a falar UMA vez: é o
        # turno da despedida, que o enunciado pede explicitamente. Quem corta
        # o loop em seguida é o roteador pós-agente, acima — sem ele, o modelo
        # calava e a UI acabava repetindo a última fala anterior.
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
            _criar_roteador_depois_do_agente(contexto),
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
        # Onde começa o turno atual. Sem isso, um turno em que o agente só
        # chama ferramentas devolveria a fala do turno ANTERIOR — foi o que
        # fez a despedida sair como uma repetição da cotação.
        self._inicio_do_turno = 0

    def enviar(self, mensagem: str) -> str:
        """Processa uma mensagem do cliente e devolve a resposta em texto."""
        if self.contexto.sessao.encerrado:
            return ""

        self.estado["messages"] = [*self.estado["messages"], ("user", mensagem)]
        self._inicio_do_turno = len(self.estado["messages"])
        self.estado = self.grafo.invoke(
            self.estado, config={"recursion_limit": LIMITE_RECURSAO}
        )
        return self.ultima_resposta

    @property
    def ultima_resposta(self) -> str:
        """Texto da última fala do assistente NESTE turno.

        Limitado ao turno de propósito: devolver fala antiga é pior do que
        devolver vazio — o cliente veria uma resposta que não tem relação com
        o que acabou de perguntar.
        """
        for mensagem in reversed(self.estado["messages"][self._inicio_do_turno :]):
            if isinstance(mensagem, AIMessage):
                texto = texto_da_mensagem(mensagem)
                if texto:
                    return texto
        return ""

    @property
    def agente_ativo(self) -> str:
        return self.estado.get("agente", triagem.NOME)
