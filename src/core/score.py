"""
Motor de score de crédito — Banco Ágil.

Módulo puro e determinístico: sem I/O, sem LLM, sem dependências externas.
É a ÚNICA fonte de verdade para o cálculo de score no sistema.

Fórmula base (do enunciado):

    score = (renda / (despesas + 1)) * PESO_RENDA
          + PESO_EMPREGO[tipo_emprego]
          + PESO_DEPENDENTES[num_dependentes]
          + PESO_DIVIDAS[tem_dividas]

Ajustes aplicados sobre o enunciado (ver ADR-001 no CLAUDE.md):
  1. O componente de renda é saturado em TETO_COMPONENTE_RENDA (500).
  2. O total é limitado (clamp) ao intervalo [0, 1000].
  3. Entradas categóricas são normalizadas (caixa, acentos, sinônimos).
  4. Arredondamento half-up explícito, para não depender do
     "banker's rounding" do round() nativo do Python.
"""

from __future__ import annotations

import math
import unicodedata
from dataclasses import asdict, dataclass
from typing import Any

# --------------------------------------------------------------------------- #
# Constantes de calibração
# --------------------------------------------------------------------------- #

PESO_RENDA: float = 30.0

# Teto do componente de renda. Escolhido como 500 porque a soma máxima dos
# componentes fixos também é 500 (300 formal + 100 sem dependentes + 100 sem
# dívidas). Assim o score máximo teórico é exatamente 1000, sem precisar de um
# clamp superior artificial.
TETO_COMPONENTE_RENDA: float = 500.0

SCORE_MIN: int = 0
SCORE_MAX: int = 1000

PESO_EMPREGO: dict[str, float] = {
    "formal": 300.0,
    "autonomo": 200.0,
    "desempregado": 0.0,
}

PESO_DEPENDENTES: dict[Any, float] = {
    0: 100.0,
    1: 80.0,
    2: 60.0,
    "3+": 30.0,
}

PESO_DIVIDAS: dict[bool, float] = {
    True: -100.0,
    False: 100.0,
}

# Sinônimos aceitos vindos da conversa (o LLM extrai, mas quem valida é o código)
_SINONIMOS_EMPREGO: dict[str, str] = {
    "formal": "formal",
    "clt": "formal",
    "carteira assinada": "formal",
    "empregado": "formal",
    "servidor publico": "formal",
    "autonomo": "autonomo",
    "freelancer": "autonomo",
    "freela": "autonomo",
    "pj": "autonomo",
    "mei": "autonomo",
    "informal": "autonomo",
    "empresario": "autonomo",
    "desempregado": "desempregado",
    "sem emprego": "desempregado",
    "desocupado": "desempregado",
}

_VERDADEIRO: frozenset[str] = frozenset({"sim", "s", "true", "1", "tenho", "possuo"})
_FALSO: frozenset[str] = frozenset({"nao", "n", "false", "0", "nenhuma", "nenhum"})


class ScoreInputError(ValueError):
    """Entrada inválida para o cálculo de score."""


@dataclass(frozen=True)
class ResultadoScore:
    """Resultado do cálculo, com o detalhamento de cada componente.

    O detalhamento existe para que o agente consiga explicar o resultado ao
    cliente sem recalcular nada por conta própria.
    """

    score: int
    componente_renda: float
    componente_emprego: float
    componente_dependentes: float
    componente_dividas: float
    total_bruto: float
    teto_renda_atingido: bool
    clamp_aplicado: bool

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


# --------------------------------------------------------------------------- #
# Normalização de entradas
# --------------------------------------------------------------------------- #


def _normalizar_texto(valor: Any) -> str:
    """Minúsculas, sem acentos, sem espaços nas bordas, espaços colapsados."""
    if valor is None:
        raise ScoreInputError("Valor de texto ausente.")
    texto = unicodedata.normalize("NFKD", str(valor))
    texto = "".join(c for c in texto if not unicodedata.combining(c))
    return " ".join(texto.strip().lower().split())


def normalizar_emprego(valor: Any) -> str:
    """Aceita 'Autônomo', 'AUTONOMO', 'CLT', 'freelancer'... -> chave canônica."""
    texto = _normalizar_texto(valor)
    if texto not in _SINONIMOS_EMPREGO:
        raise ScoreInputError(
            f"Tipo de emprego não reconhecido: {valor!r}. "
            f"Esperado um de: formal, autônomo, desempregado."
        )
    return _SINONIMOS_EMPREGO[texto]


def normalizar_dependentes(valor: Any) -> Any:
    """Qualquer inteiro >= 3 (ou a string '3+') colapsa para a chave '3+'."""
    if isinstance(valor, bool):
        raise ScoreInputError("Número de dependentes não pode ser booleano.")
    if isinstance(valor, str):
        texto = _normalizar_texto(valor)
        if texto in {"3+", "3 ou mais", "mais de 3"}:
            return "3+"
        if not texto.isdigit():
            raise ScoreInputError(f"Número de dependentes inválido: {valor!r}.")
        numero = int(texto)
    else:
        try:
            numero = int(valor)
        except (TypeError, ValueError) as exc:
            raise ScoreInputError(
                f"Número de dependentes inválido: {valor!r}."
            ) from exc
        if numero != valor:
            raise ScoreInputError(
                f"Número de dependentes deve ser inteiro: {valor!r}."
            )
    if numero < 0:
        raise ScoreInputError("Número de dependentes não pode ser negativo.")
    return numero if numero < 3 else "3+"


def normalizar_dividas(valor: Any) -> bool:
    """Aceita bool, 'sim'/'não'/'nao'/'S'/'N'/'true'/'false'/1/0."""
    if isinstance(valor, bool):
        return valor
    if isinstance(valor, int | float) and valor in (0, 1):
        return bool(valor)
    texto = _normalizar_texto(valor)
    if texto in _VERDADEIRO:
        return True
    if texto in _FALSO:
        return False
    raise ScoreInputError(
        f"Resposta sobre dívidas não reconhecida: {valor!r}. Esperado sim ou não."
    )


def _validar_monetario(valor: Any, nome: str) -> float:
    if isinstance(valor, bool):
        raise ScoreInputError(f"{nome} não pode ser booleano.")
    try:
        numero = float(valor)
    except (TypeError, ValueError) as exc:
        raise ScoreInputError(f"{nome} inválido: {valor!r}.") from exc
    if math.isnan(numero) or math.isinf(numero):
        raise ScoreInputError(f"{nome} deve ser um número finito.")
    if numero < 0:
        raise ScoreInputError(f"{nome} não pode ser negativo.")
    return numero


def _arredondar_half_up(valor: float) -> int:
    """Arredondamento half-up determinístico (0.5 sempre sobe)."""
    return math.floor(valor + 0.5)


# --------------------------------------------------------------------------- #
# Cálculo
# --------------------------------------------------------------------------- #


def calcular_score(
    renda_mensal: Any,
    despesas_fixas: Any,
    tipo_emprego: Any,
    num_dependentes: Any,
    tem_dividas: Any,
) -> ResultadoScore:
    """Calcula o score de crédito (0 a 1000) a partir dos dados da entrevista.

    Levanta ScoreInputError para qualquer entrada inválida. O chamador deve
    tratar essa exceção e pedir a informação novamente ao cliente.
    """
    renda = _validar_monetario(renda_mensal, "Renda mensal")
    despesas = _validar_monetario(despesas_fixas, "Despesas fixas")

    emprego = normalizar_emprego(tipo_emprego)
    dependentes = normalizar_dependentes(num_dependentes)
    dividas = normalizar_dividas(tem_dividas)

    # Multiplicação antes da divisão: matematicamente equivalente ao enunciado,
    # porém numericamente mais estável na fronteira do teto.
    renda_bruta = (renda * PESO_RENDA) / (despesas + 1.0)
    teto_atingido = renda_bruta > TETO_COMPONENTE_RENDA
    componente_renda = min(renda_bruta, TETO_COMPONENTE_RENDA)

    componente_emprego = PESO_EMPREGO[emprego]
    componente_dependentes = PESO_DEPENDENTES[dependentes]
    componente_dividas = PESO_DIVIDAS[dividas]

    total_bruto = (
        componente_renda
        + componente_emprego
        + componente_dependentes
        + componente_dividas
    )

    total_limitado = min(max(total_bruto, float(SCORE_MIN)), float(SCORE_MAX))
    clamp_aplicado = total_limitado != total_bruto

    return ResultadoScore(
        score=_arredondar_half_up(total_limitado),
        componente_renda=componente_renda,
        componente_emprego=componente_emprego,
        componente_dependentes=componente_dependentes,
        componente_dividas=componente_dividas,
        total_bruto=total_bruto,
        teto_renda_atingido=teto_atingido,
        clamp_aplicado=clamp_aplicado,
    )
