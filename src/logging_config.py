"""
Logging estruturado em `logs/app.log`.

Duas exigências da seção 6 do CLAUDE.md:

- CPF sempre mascarado; data de nascimento nunca registrada.
- Nada de `except: pass` — toda exceção é logada com contexto.

O mascaramento é feito por um filtro no próprio logger, e não só nas chamadas.
Confiar em cada `logger.info` lembrar de mascarar é frágil; um filtro é uma
rede de segurança que pega o que passou despercebido.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import re
from pathlib import Path

FORMATO = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"

# 11 dígitos seguidos, ou pontuados como CPF.
_PADRAO_CPF = re.compile(r"\b(\d{3})\.?(\d{3})\.?(\d{3})-?(\d{2})\b")
# YYYY-MM-DD e DD/MM/YYYY.
_PADRAO_DATA = re.compile(r"\b(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})\b")

_configurado = False


class FiltroDadosSensiveis(logging.Filter):
    """Mascara CPF e remove data de nascimento de qualquer registro.

    Atua na mensagem já formatada, então pega inclusive o CPF que vier dentro
    do texto de uma exceção de terceiros.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        try:
            mensagem = record.getMessage()
        except Exception:  # noqa: BLE001 - formatação inválida não pode derrubar o log
            return True

        limpa = _PADRAO_CPF.sub(r"***.***.\3-\4", mensagem)
        limpa = _PADRAO_DATA.sub("<data-oculta>", limpa)

        if limpa != mensagem:
            # Substitui a mensagem e zera os args, que já foram interpolados.
            record.msg = limpa
            record.args = ()
        return True


def configurar_logging(
    *,
    diretorio: str | Path = "logs",
    nivel: str | None = None,
    para_console: bool = False,
) -> logging.Logger:
    """Configura o logger raiz da aplicação. Idempotente.

    O Streamlit re-executa o script a cada interação; sem a guarda de
    idempotência os handlers se acumulariam e cada linha sairia duplicada.
    """
    global _configurado

    logger_app = logging.getLogger("src")
    if _configurado:
        return logger_app

    nivel_efetivo = (nivel or os.getenv("BANCO_AGIL_LOG_LEVEL", "INFO")).upper()
    logger_app.setLevel(nivel_efetivo)
    logger_app.propagate = False

    filtro = FiltroDadosSensiveis()
    formatador = logging.Formatter(FORMATO)

    caminho = Path(diretorio)
    try:
        caminho.mkdir(parents=True, exist_ok=True)
        handler_arquivo = logging.handlers.RotatingFileHandler(
            caminho / "app.log",
            maxBytes=1_000_000,
            backupCount=3,
            encoding="utf-8",
        )
        handler_arquivo.setFormatter(formatador)
        handler_arquivo.addFilter(filtro)
        logger_app.addHandler(handler_arquivo)
    except OSError as exc:
        # Sem permissão de escrita a aplicação continua: perder o log é ruim,
        # derrubar o atendimento é pior.
        logging.getLogger(__name__).warning(
            "Não foi possível abrir o arquivo de log em %s: %s", caminho, exc
        )
        para_console = True

    if para_console:
        handler_console = logging.StreamHandler()
        handler_console.setFormatter(formatador)
        handler_console.addFilter(filtro)
        logger_app.addHandler(handler_console)

    _configurado = True
    return logger_app


def resetar_logging() -> None:
    """Desfaz a configuração — usado entre testes."""
    global _configurado
    logger_app = logging.getLogger("src")
    for handler in list(logger_app.handlers):
        handler.close()
        logger_app.removeHandler(handler)
    _configurado = False
