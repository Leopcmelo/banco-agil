"""
Interface de testes do Banco Ágil (Streamlit).

Uma UI de chat simples para simular um atendimento completo, como pede o
enunciado. A barra lateral existe para inspeção durante os testes: mostra o
estado da sessão, os CSVs e um botão que restaura os dados semente.

Nota sobre estado: `st.session_state` é usado apenas como cache de UI — a
verdade continua nos CSVs, acessados via repositório (seção 8 do CLAUDE.md).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

import pandas as pd
import streamlit as st
from dotenv import load_dotenv

from src.agents.grafo import Atendimento, criar_llm
from src.core.validadores import formatar_cpf
from src.data.repositories import RepositorioBancoAgil, RepositorioError
from src.logging_config import configurar_logging
from src.session import SessionState
from src.tools.base import ContextoAtendimento

load_dotenv()
configurar_logging()
logger = logging.getLogger(__name__)

RAIZ = Path(__file__).parent
DIRETORIO_DADOS = Path(os.getenv("BANCO_AGIL_DATA_DIR", RAIZ / "data"))

st.set_page_config(page_title="Banco Ágil", page_icon="🏦", layout="centered")


# --------------------------------------------------------------------------- #
# Sessão
# --------------------------------------------------------------------------- #


def iniciar_atendimento() -> None:
    """Cria uma sessão nova, descartando a conversa anterior."""
    contexto = ContextoAtendimento(
        sessao=SessionState(), repositorio=RepositorioBancoAgil(DIRETORIO_DADOS)
    )
    st.session_state.contexto = contexto
    st.session_state.historico = []
    st.session_state.erro_inicializacao = None

    try:
        st.session_state.atendimento = Atendimento(contexto, criar_llm())
    except Exception as exc:
        # Sem chave ou sem rede, a UI ainda sobe e explica o que falta em vez
        # de mostrar um stack trace.
        logger.exception("Não foi possível inicializar o modelo.")
        st.session_state.atendimento = None
        st.session_state.erro_inicializacao = str(exc)


if "atendimento" not in st.session_state:
    iniciar_atendimento()


contexto: ContextoAtendimento = st.session_state.contexto
atendimento: Atendimento | None = st.session_state.atendimento


# --------------------------------------------------------------------------- #
# Barra lateral — inspeção
# --------------------------------------------------------------------------- #

with st.sidebar:
    st.subheader("Sessão")

    resumo = contexto.sessao.resumo_seguro()
    if resumo["autenticado"]:
        st.success(f"Autenticado: {resumo['nome_cliente']}")
        st.caption(f"CPF {resumo['cpf']}")
    elif resumo["bloqueado"]:
        st.error("Bloqueado por excesso de tentativas")
    else:
        st.info("Não autenticado")
        st.caption(f"Tentativas restantes: {resumo['tentativas_restantes']}")

    if resumo["encerrado"]:
        st.warning("Atendimento encerrado")

    st.caption(f"Assunto atual: `{atendimento.agente_ativo if atendimento else '—'}`")

    entrevista_iniciada = any(
        v is not None for v in contexto.sessao.entrevista.as_dict().values()
    )
    if entrevista_iniciada and not resumo["entrevista_completa"]:
        st.caption(f"Entrevista — falta: {', '.join(resumo['entrevista_faltando'])}")

    st.divider()

    col_a, col_b = st.columns(2)
    if col_a.button("Nova conversa", width="stretch"):
        iniciar_atendimento()
        st.rerun()

    if col_b.button("Resetar dados", width="stretch"):
        try:
            contexto.repositorio.restaurar_seed()
            iniciar_atendimento()
            st.toast("Dados restaurados a partir de data/seed/.")
            st.rerun()
        except RepositorioError as exc:
            st.error(f"Falha ao restaurar: {exc}")

    st.divider()
    st.subheader("Dados")

    try:
        clientes = contexto.repositorio.listar_clientes()
        with st.expander(f"Clientes ({len(clientes)})"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "CPF": formatar_cpf(c.cpf),
                            "Nome": c.nome,
                            "Nascimento": c.data_nascimento,
                            "Limite": f"R$ {c.limite_atual:,.2f}",
                            "Score": c.score,
                        }
                        for c in clientes
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
            st.caption(
                "Base de testes: use qualquer CPF e data desta tabela para "
                "autenticar."
            )

        solicitacoes = contexto.repositorio.listar_solicitacoes()
        with st.expander(f"Solicitações ({len(solicitacoes)})"):
            if solicitacoes:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                "CPF": formatar_cpf(s.cpf_cliente),
                                "Quando": s.data_hora_solicitacao[:19],
                                "De": f"R$ {s.limite_atual:,.2f}",
                                "Para": f"R$ {s.novo_limite_solicitado:,.2f}",
                                "Status": s.status_pedido,
                            }
                            for s in reversed(solicitacoes)
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
            else:
                st.caption("Nenhuma solicitação registrada ainda.")

        faixas = contexto.repositorio.carregar_faixas_score()
        with st.expander("Faixas de limite"):
            st.dataframe(
                pd.DataFrame(
                    [
                        {
                            "Score": f"{f.score_min} – {f.score_max}",
                            "Limite máximo": f"R$ {f.limite_maximo:,.2f}",
                        }
                        for f in faixas
                    ]
                ),
                hide_index=True,
                width="stretch",
            )
    except RepositorioError as exc:
        st.error(f"Erro ao ler os dados: {exc}")


# --------------------------------------------------------------------------- #
# Chat
# --------------------------------------------------------------------------- #

st.title("🏦 Banco Ágil")
st.caption("Atendimento virtual")

if st.session_state.erro_inicializacao:
    st.error(
        "Não foi possível iniciar o atendimento.\n\n"
        f"`{st.session_state.erro_inicializacao}`\n\n"
        "Copie `.env.example` para `.env` e preencha a `GOOGLE_API_KEY`."
    )
    st.stop()

for autor, texto in st.session_state.historico:
    with st.chat_message(autor):
        st.markdown(texto)

# A primeira fala é do atendente, como pede o fluxo do enunciado.
if not st.session_state.historico:
    with st.chat_message("assistant"):
        marcador = st.empty()
        marcador.markdown("_...")
        try:
            saudacao = atendimento.enviar("Olá")
        except Exception as exc:
            logger.exception("Falha ao iniciar a conversa.")
            saudacao = (
                "Tive um problema técnico para iniciar o atendimento. "
                "Tente recarregar a página."
            )
            st.error(str(exc))
        marcador.markdown(saudacao)
    st.session_state.historico.append(("assistant", saudacao))

encerrado = contexto.sessao.encerrado or contexto.sessao.bloqueado

if encerrado:
    st.info("Atendimento encerrado. Use **Nova conversa** para começar de novo.")
else:
    pergunta = st.chat_input("Digite sua mensagem...")
    if pergunta:
        st.session_state.historico.append(("user", pergunta))
        with st.chat_message("user"):
            st.markdown(pergunta)

        with st.chat_message("assistant"):
            marcador = st.empty()
            marcador.markdown("_...")
            try:
                resposta = atendimento.enviar(pergunta)
            except Exception as exc:
                # O enunciado pede tratamento controlado: mensagem clara para
                # o cliente e registro técnico para análise posterior.
                logger.exception("Falha ao processar a mensagem do cliente.")
                resposta = (
                    "Tive um problema técnico ao processar sua mensagem. "
                    "Pode tentar de novo?"
                )
                st.caption(f"Detalhe técnico: {exc}")
            marcador.markdown(resposta or "_(atendimento encerrado)_")

        st.session_state.historico.append(("assistant", resposta))
        st.rerun()
