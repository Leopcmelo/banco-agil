"""
Testes da camada de apresentação.

Só o que é testável sem subir o Streamlit: o escape de Markdown, que existe
por causa de dois defeitos observados no navegador.
"""

from __future__ import annotations

import pytest

from app import texto_seguro


@pytest.mark.parametrize(
    "entrada,nao_pode_conter",
    [
        # O par de $ vira delimitador de LaTeX no Streamlit, e todo valor em
        # reais tem R$ — duas ocorrências bastam para mutilar a frase inteira.
        ("Aprovado R$ 3.000,00, antes era R$ 500,00", "$ 3.000,00, antes era R$"),
        # *** é marcador de ênfase: a máscara sumia e virava "..877-39".
        ("CPF ***.***.877-39", "***"),
    ],
)
def test_sintaxe_de_markdown_e_neutralizada(entrada, nao_pode_conter):
    assert nao_pode_conter not in texto_seguro(entrada)


@pytest.mark.parametrize("caractere", ["*", "_", "`", "$", "~", "\\"])
def test_todo_caractere_perigoso_e_escapado(caractere):
    assert texto_seguro(f"a{caractere}b") == f"a\\{caractere}b"


def test_o_texto_visivel_e_preservado():
    """Escapar não pode apagar conteúdo — só neutralizar a formatação."""
    original = "Seu limite é de R$ 8.000,00 e o CPF é ***.***.793-26."
    escapado = texto_seguro(original)
    for trecho in (
        "Seu limite é de R",
        "8.000,00",
        "877-39".replace("877-39", "793-26"),
    ):
        assert trecho in escapado


def test_texto_sem_marcacao_passa_praticamente_intacto():
    simples = "Tudo certo, Giovana. Como posso ajudar voce hoje?"
    assert texto_seguro(simples) == simples
