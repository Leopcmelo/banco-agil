"""
Testes da camada de apresentação.

Só o que é testável sem subir o Streamlit: o preparo de texto para exibição,
que existe por causa de defeitos observados no navegador.

A distinção entre os dois casos é o ponto: uma FALA pode ter formatação
intencional do modelo, um DADO nunca quer nenhuma.
"""

from __future__ import annotations

import pytest

from app import texto_de_conversa, texto_de_dado

# --------------------------------------------------------------------------- #
# 1. Falas — escapar só o que quebra
# --------------------------------------------------------------------------- #


def test_valores_em_reais_nao_viram_formula():
    """Um par de $ era lido como LaTeX, e todo valor em reais tem R$."""
    saida = texto_de_conversa("Aprovado R$ 3.000,00, antes era R$ 500,00")
    assert saida.count("\\$") == 2
    assert "3.000,00" in saida and "500,00" in saida


def test_negrito_do_modelo_continua_funcionando():
    """Escapar tudo mostrava `**R$ 8.000,00**` com os asteriscos na tela."""
    saida = texto_de_conversa("Seu limite é **R$ 8.000,00**")
    assert "**" in saida
    assert "\\*" not in saida


def test_fala_sem_cifrao_passa_intacta():
    fala = "Tudo certo, Giovana. Como posso ajudar voce hoje?"
    assert texto_de_conversa(fala) == fala


# --------------------------------------------------------------------------- #
# 2. Dados — neutralizar tudo
# --------------------------------------------------------------------------- #


def test_mascara_de_cpf_sobrevive():
    """`***.***.877-39` aparecia como `..877-39`: *** é marcador de ênfase."""
    saida = texto_de_dado("***.***.877-39")
    assert "\\*\\*\\*" in saida
    assert "877-39" in saida


@pytest.mark.parametrize("caractere", ["*", "_", "`", "$", "~", "\\", "[", "]"])
def test_todo_caractere_de_markdown_e_escapado_em_dado(caractere):
    assert texto_de_dado(f"a{caractere}b") == f"a\\{caractere}b"


def test_dado_sem_marcacao_passa_intacto():
    assert texto_de_dado("Ana Beatriz Cardoso") == "Ana Beatriz Cardoso"


# --------------------------------------------------------------------------- #
# 3. Nenhum dos dois pode apagar conteúdo
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("preparo", [texto_de_conversa, texto_de_dado])
def test_o_conteudo_visivel_e_preservado(preparo):
    original = "Limite de R$ 8.000,00 para o CPF 793-26."
    saida = preparo(original)
    for trecho in ("8.000,00", "793-26", "Limite de R"):
        assert trecho in saida
