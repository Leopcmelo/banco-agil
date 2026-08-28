"""
Lista os arquivos de teste do núcleo, um por linha.

Usado pelo passo de cobertura da CI. A lista é derivada de `src/core/` em vez
de escrita à mão porque a versão manual já quebrou uma vez: um módulo novo foi
criado sem entrar nela, e o gate de 100% falhou só depois do push.

`tests/test_estrutura.py` garante o outro lado — que todo módulo do núcleo
tenha o seu arquivo de teste.
"""

from __future__ import annotations

from pathlib import Path

RAIZ = Path(__file__).resolve().parents[1]


def main() -> None:
    modulos = sorted(
        caminho.stem
        for caminho in (RAIZ / "src" / "core").glob("*.py")
        if not caminho.stem.startswith("__")
    )
    for nome in modulos:
        print(f"tests/test_{nome}.py")


if __name__ == "__main__":
    main()
