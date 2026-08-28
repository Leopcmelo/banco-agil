"""
Testes da própria estrutura do projeto.

Existem por causa de uma falha real: `src/core/conversao.py` foi criado sem
entrar na lista de arquivos do passo de cobertura da CI, e o gate de 100%
quebrou só depois do push. Um módulo do núcleo sem teste dedicado passa a
falhar aqui, no suite local, antes de chegar na CI.
"""

from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]
DIRETORIO_CORE = RAIZ / "src" / "core"
DIRETORIO_TESTES = RAIZ / "tests"


def modulos_do_nucleo() -> list[str]:
    return sorted(
        caminho.stem
        for caminho in DIRETORIO_CORE.glob("*.py")
        if not caminho.stem.startswith("__")
    )


def test_o_nucleo_tem_modulos():
    """Guarda contra um glob que silenciosamente não encontra nada."""
    assert modulos_do_nucleo()


def test_todo_modulo_do_nucleo_tem_teste_dedicado():
    """Regra da seção 7 do CLAUDE.md, verificada em vez de combinada.

    O núcleo exige teste unitário sem exceção. Cobertura incidental vinda dos
    testes de tools não conta: a regra pede caminho feliz, borda e entrada
    inválida, e isso só existe num arquivo próprio.
    """
    faltando = [
        nome
        for nome in modulos_do_nucleo()
        if not (DIRETORIO_TESTES / f"test_{nome}.py").exists()
    ]
    assert not faltando, (
        f"Módulos de src/core sem tests/test_<nome>.py: {faltando}. "
        f"O passo de cobertura da CI deriva a lista desses arquivos, então "
        f"sem eles o gate de 100% falha no push."
    )


def test_nenhum_pd_read_csv_fora_do_repositorio():
    """Regra inviolável nº 3: todo acesso a CSV passa pelo repositório."""
    permitido = {RAIZ / "src" / "data" / "repositories.py"}
    infratores = [
        caminho.relative_to(RAIZ)
        for caminho in (RAIZ / "src").rglob("*.py")
        if caminho not in permitido
        and "read_csv" in caminho.read_text(encoding="utf-8")
    ]
    assert not infratores, f"pd.read_csv fora do repositório: {infratores}"


def test_o_nucleo_nao_importa_llm_nem_framework_de_agente():
    """Regra inviolável nº 2: `src/core/` é puro.

    Se algum dia alguém importar LangChain aqui, o núcleo deixa de poder ser
    testado sem chave e sem os SDKs — e o teste quebra antes disso acontecer.
    """
    proibidos = ("langchain", "langgraph", "anthropic", "google.genai", "streamlit")
    infratores = []
    for caminho in DIRETORIO_CORE.glob("*.py"):
        texto = caminho.read_text(encoding="utf-8")
        for termo in proibidos:
            if f"import {termo}" in texto or f"from {termo}" in texto:
                infratores.append(f"{caminho.name}: {termo}")
    assert not infratores, f"src/core não pode importar: {infratores}"
