"""
Contrato comum das tools.

Toda tool devolve `dict` serializável, nunca objeto de domínio (seção 5 do
CLAUDE.md):

    {"status": "ok" | "erro" | "bloqueado", "dados": {...}, "mensagem": "..."}

E toda tool que exponha dado de cliente passa por `exige_sessao_ativa`, que
verifica `session.autenticado` em CÓDIGO. O LLM não tem como contornar isso:
mesmo que o prompt seja manipulado a afirmar que o cliente está autenticado, a
tool consulta o objeto de sessão e recusa.
"""

from __future__ import annotations

import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from src.data.repositories import RepositorioBancoAgil
from src.session import SessionState

logger = logging.getLogger(__name__)

STATUS_OK = "ok"
STATUS_ERRO = "erro"
STATUS_BLOQUEADO = "bloqueado"

Resposta = dict[str, Any]


@dataclass
class ContextoAtendimento:
    """O que as tools precisam para trabalhar.

    Injetado explicitamente em vez de virar estado global: é o que permite que
    cada teste — e cada sessão do Streamlit — tenha o seu próprio.
    """

    sessao: SessionState
    repositorio: RepositorioBancoAgil


def ok(mensagem: str, **dados: Any) -> Resposta:
    return {"status": STATUS_OK, "dados": dados, "mensagem": mensagem}


def erro(mensagem: str, **dados: Any) -> Resposta:
    return {"status": STATUS_ERRO, "dados": dados, "mensagem": mensagem}


def bloqueado(mensagem: str, **dados: Any) -> Resposta:
    return {"status": STATUS_BLOQUEADO, "dados": dados, "mensagem": mensagem}


def exige_sessao_ativa(funcao: Callable[..., Resposta]) -> Callable[..., Resposta]:
    """Recusa a execução se a sessão não estiver autenticada e ativa.

    Ordem das checagens importa: bloqueio e encerramento vêm antes da
    autenticação, porque uma sessão bloqueada não deve receber convite para
    tentar de novo.
    """

    @functools.wraps(funcao)
    def wrapper(contexto: ContextoAtendimento, *args: Any, **kwargs: Any) -> Resposta:
        sessao = contexto.sessao

        if sessao.bloqueado:
            logger.warning(
                "Tool %s recusada: sessão bloqueada por excesso de tentativas.",
                funcao.__name__,
            )
            return bloqueado(
                "O atendimento foi encerrado porque não foi possível "
                "confirmar a identidade.",
                motivo="autenticacao_bloqueada",
            )

        if sessao.encerrado:
            logger.info("Tool %s recusada: atendimento já encerrado.", funcao.__name__)
            return bloqueado(
                "Este atendimento já foi encerrado.",
                motivo="atendimento_encerrado",
            )

        if not sessao.autenticado:
            logger.warning("Tool %s recusada: sessão não autenticada.", funcao.__name__)
            return erro(
                "É necessário confirmar CPF e data de nascimento antes de "
                "acessar qualquer informação da conta.",
                motivo="nao_autenticado",
            )

        return funcao(contexto, *args, **kwargs)

    return wrapper


def tratar_falhas(funcao: Callable[..., Resposta]) -> Callable[..., Resposta]:
    """Rede de segurança: qualquer exceção inesperada vira resposta `erro`.

    Nada de `except: pass` (regra inviolável nº 8) — a exceção é logada com
    stack trace completo e o cliente recebe uma mensagem clara.
    """

    @functools.wraps(funcao)
    def wrapper(contexto: ContextoAtendimento, *args: Any, **kwargs: Any) -> Resposta:
        try:
            return funcao(contexto, *args, **kwargs)
        except Exception:
            logger.exception("Falha inesperada em %s.", funcao.__name__)
            return erro(
                "Tive um problema técnico ao processar essa informação. "
                "Podemos tentar de novo em instantes, ou seguir com outro "
                "assunto se preferir.",
                motivo="falha_interna",
            )

    return wrapper
