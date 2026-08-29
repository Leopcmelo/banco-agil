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

from src.agents.grafo import AGENTES, Atendimento, criar_llm
from src.core.conversao import formatar_valor_br
from src.core.validadores import formatar_cpf
from src.data.repositories import RepositorioBancoAgil, RepositorioError
from src.logging_config import configurar_logging
from src.session import SessionState
from src.tools.base import ContextoAtendimento
from src.tools.entrevista import PERGUNTAS

load_dotenv()
configurar_logging()
logger = logging.getLogger(__name__)

RAIZ = Path(__file__).parent
DIRETORIO_DADOS = Path(os.getenv("BANCO_AGIL_DATA_DIR", RAIZ / "data"))

st.set_page_config(page_title="Banco Ágil", page_icon="🏦", layout="centered")


def texto_de_conversa(valor: str) -> str:
    """Prepara uma fala do atendimento para exibição.

    Escapa APENAS `$`. O Streamlit lê um par de `$` como delimitador de LaTeX,
    e como todo valor em reais tem `R$`, qualquer frase com dois valores saía
    como fórmula — foi o que aconteceu com "R$ 3.000,00 ... R$ 500,00".

    O resto do Markdown fica valendo de propósito: o modelo usa negrito para
    destacar valores, e escapar tudo transformava `**R$ 8.000,00**` nos
    asteriscos literais na tela.
    """
    return valor.replace("$", "\\$")


def texto_de_dado(valor: str) -> str:
    """Prepara um DADO para exibição, sem interpretar nada.

    Diferente de uma fala, um dado nunca quer formatação: a máscara de CPF
    `***.***.877-39` aparecia como `..877-39` porque `***` é marcador de
    ênfase. Aqui todo caractere de Markdown é neutralizado.
    """
    for caractere in ("\\", "`", "*", "_", "$", "~", "[", "]"):
        valor = valor.replace(caractere, "\\" + caractere)
    return valor


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
        st.caption(f"CPF {texto_de_dado(resumo['cpf'])}")
    elif resumo["bloqueado"]:
        st.error("Bloqueado por excesso de tentativas")
    else:
        st.info("Não autenticado")
        st.caption(f"Tentativas restantes: {resumo['tentativas_restantes']}")

    if resumo["encerrado"]:
        st.warning("Atendimento encerrado")

    # O rótulo antigo dizia "Assunto atual", mas o valor sempre foi o nome do
    # agente. Nomear o que é: para o avaliador, ver o agente mudar de Triagem
    # para Crédito é a evidência de que a orquestração funciona — evidência
    # que o cliente, do outro lado, nunca vê (ADR-004).
    if atendimento:
        ativo = AGENTES[atendimento.agente_ativo].TITULO
        st.caption(f"Agente ativo: **{ativo}**")
        st.caption(
            "Painel de inspeção — o cliente não vê esta troca, "
            "para ele o atendimento é um só."
        )
    else:
        st.caption("Agente ativo: —")

    entrevista_iniciada = any(
        v is not None for v in contexto.sessao.entrevista.as_dict().values()
    )
    if entrevista_iniciada and not resumo["entrevista_completa"]:
        # Rótulos humanos, não os nomes de campo do Python: a sidebar mostrava
        # "despesas_fixas, num_dependentes, tem_dividas".
        faltando = ", ".join(
            PERGUNTAS[campo] for campo in resumo["entrevista_faltando"]
        )
        st.caption(f"Entrevista — falta: {texto_de_dado(faltando)}")

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
            # Linhas em vez de tabela: na largura da barra lateral, uma tabela
            # de cinco colunas cortava justamente a data de nascimento — que é
            # metade da credencial de teste. Cada cliente cabe em duas linhas.
            for c in clientes:
                nascimento = "/".join(reversed(c.data_nascimento.split("-")))
                st.markdown(
                    f"**{texto_de_dado(formatar_cpf(c.cpf))}** · {nascimento}  \n"
                    f"{texto_de_dado(c.primeiro_nome)} · "
                    f"score {c.score} · R$ {formatar_valor_br(c.limite_atual)}"
                )
            st.caption(
                "Base de testes: use o CPF e a data de qualquer cliente para "
                "autenticar."
            )

        solicitacoes = contexto.repositorio.listar_solicitacoes()
        with st.expander(f"Solicitações ({len(solicitacoes)})"):
            if solicitacoes:
                st.dataframe(
                    pd.DataFrame(
                        [
                            {
                                # Três colunas curtas: na largura da barra
                                # lateral, Status — o desfecho que a auditoria
                                # precisa ver — ficava fora da tela. O CPF saiu
                                # porque é redundante (o painel de sessão acima
                                # já diz quem está autenticado) e era o que
                                # empurrava o desfecho para fora.
                                # HH:MM basta no painel; a precisão completa fica no
                                # CSV. Os segundos empurravam Status para fora.
                                "Hora": s.data_hora_solicitacao[11:16],
                                "Pedido": formatar_valor_br(
                                    s.novo_limite_solicitado, casas=0
                                ),
                                # A base do julgamento fica ao lado do
                                # desfecho: é o que permite explicar dois
                                # pedidos iguais com resultados opostos.
                                "Score": s.score_na_decisao,
                                "Status": s.status_pedido,
                            }
                            for s in reversed(solicitacoes)
                        ]
                    ),
                    hide_index=True,
                    width="stretch",
                )
                st.caption(
                    "Pedidos de todos os clientes, do mais recente ao mais "
                    "antigo. Score é o que embasou cada decisão."
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
                            "Limite máximo": f"R$ {formatar_valor_br(f.limite_maximo)}",
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
        st.markdown(texto_de_conversa(texto))

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
        marcador.markdown(texto_de_conversa(saudacao))
    st.session_state.historico.append(("assistant", saudacao))

encerrado = contexto.sessao.encerrado or contexto.sessao.bloqueado

if encerrado:
    # Campo DESABILITADO, não removido. Removê-lo tirava a âncora do rodapé e a
    # página saltava para o topo — o cliente se despedia e via o começo da
    # conversa, sem a despedida nem este aviso.
    st.chat_input("Atendimento encerrado", disabled=True)
    st.info("Atendimento encerrado. Use **Nova conversa** para começar de novo.")
else:
    pergunta = st.chat_input("Digite sua mensagem...")
    if pergunta:
        st.session_state.historico.append(("user", pergunta))
        with st.chat_message("user"):
            st.markdown(texto_de_conversa(pergunta))

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
            marcador.markdown(
                texto_de_conversa(resposta) if resposta else "_(atendimento encerrado)_"
            )

        st.session_state.historico.append(("assistant", resposta))
        st.rerun()
