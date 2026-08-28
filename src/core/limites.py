"""
Faixas de score e decisão sobre solicitação de aumento de limite.

Módulo puro: recebe as faixas já carregadas, não lê CSV, não conhece o LLM.
Quem lê `score_limite.csv` é `src/data/repositories.py`.

Este é o módulo que decide `aprovado` vs `rejeitado` (regra inviolável nº 1 do
CLAUDE.md): o agente apenas comunica o resultado, nunca o calcula.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from src.core.score import SCORE_MAX, SCORE_MIN

# Domínio fechado de status_pedido (ADR-002). `reprovado` não existe no código.
STATUS_PENDENTE = "pendente"
STATUS_APROVADO = "aprovado"
STATUS_REJEITADO = "rejeitado"
STATUS_VALIDOS: frozenset[str] = frozenset(
    {STATUS_PENDENTE, STATUS_APROVADO, STATUS_REJEITADO}
)


class TabelaLimitesError(ValueError):
    """A tabela de faixas de score é inconsistente (erro de dados)."""


class ScoreForaDasFaixasError(LookupError):
    """O score não pertence a nenhuma faixa — erro de dados, não aprovação."""


@dataclass(frozen=True)
class FaixaScore:
    """Uma linha de `score_limite.csv`. Limites inclusivos nos dois lados."""

    score_min: int
    score_max: int
    limite_maximo: float

    def contem(self, score: int) -> bool:
        return self.score_min <= score <= self.score_max


@dataclass(frozen=True)
class DecisaoLimite:
    """Resultado da análise de um pedido de aumento.

    `status` já é o valor final gravado em `status_pedido`, para que nem o
    agente nem a camada de tools precisem traduzir nada.
    """

    status: str
    limite_permitido: float
    limite_atual: float
    novo_limite_solicitado: float
    score: int
    e_aumento: bool
    motivo: str

    @property
    def aprovado(self) -> bool:
        return self.status == STATUS_APROVADO

    def as_dict(self) -> dict[str, Any]:
        dados = asdict(self)
        dados["aprovado"] = self.aprovado
        return dados


def _validar_score(score: Any) -> int:
    if isinstance(score, bool):
        raise TabelaLimitesError("Score não pode ser booleano.")
    try:
        inteiro = int(score)
    except (TypeError, ValueError) as exc:
        raise TabelaLimitesError(f"Score inválido: {score!r}.") from exc
    # Um score fracionário (ex.: 549.7) é entrada malformada, não arredondável:
    # arredondar aqui esconderia um bug de quem chamou.
    if isinstance(score, float) and inteiro != score:
        raise TabelaLimitesError(f"Score deve ser inteiro: {score!r}.")
    if not SCORE_MIN <= inteiro <= SCORE_MAX:
        raise TabelaLimitesError(
            f"Score fora do intervalo [{SCORE_MIN}, {SCORE_MAX}]: {inteiro}."
        )
    return inteiro


def validar_tabela(faixas: Iterable[FaixaScore]) -> tuple[FaixaScore, ...]:
    """Ordena e valida a tabela; falha cedo se os dados forem inconsistentes.

    Exige: pelo menos uma faixa, `score_min <= score_max`, limite não negativo,
    cobertura total de [0, 1000], sem buracos e sem sobreposição.
    """
    ordenadas = tuple(sorted(faixas, key=lambda f: (f.score_min, f.score_max)))
    if not ordenadas:
        raise TabelaLimitesError("Tabela de faixas de score está vazia.")

    for faixa in ordenadas:
        if faixa.score_min > faixa.score_max:
            raise TabelaLimitesError(
                f"Faixa invertida: score_min={faixa.score_min} > "
                f"score_max={faixa.score_max}."
            )
        if faixa.limite_maximo < 0:
            raise TabelaLimitesError(
                f"Limite máximo negativo na faixa "
                f"[{faixa.score_min}, {faixa.score_max}]."
            )

    if ordenadas[0].score_min != SCORE_MIN:
        raise TabelaLimitesError(
            f"A primeira faixa deve começar em {SCORE_MIN}, "
            f"mas começa em {ordenadas[0].score_min}."
        )
    if ordenadas[-1].score_max != SCORE_MAX:
        raise TabelaLimitesError(
            f"A última faixa deve terminar em {SCORE_MAX}, "
            f"mas termina em {ordenadas[-1].score_max}."
        )

    for anterior, seguinte in zip(ordenadas, ordenadas[1:]):
        if seguinte.score_min <= anterior.score_max:
            raise TabelaLimitesError(
                f"Faixas se sobrepõem: [{anterior.score_min}, {anterior.score_max}] "
                f"e [{seguinte.score_min}, {seguinte.score_max}]."
            )
        if seguinte.score_min != anterior.score_max + 1:
            raise TabelaLimitesError(
                f"Buraco entre as faixas: nenhum limite definido para o score "
                f"{anterior.score_max + 1}."
            )

    return ordenadas


def faixa_do_score(score: Any, faixas: Iterable[FaixaScore]) -> FaixaScore:
    """Retorna a faixa que contém o score. Nunca devolve None silenciosamente."""
    valor = _validar_score(score)
    for faixa in validar_tabela(faixas):
        if faixa.contem(valor):
            return faixa
    # Inalcançável com uma tabela validada, mas explícito por segurança: um
    # score sem faixa é erro de dados, jamais uma aprovação por omissão.
    raise ScoreForaDasFaixasError(
        f"Score {valor} não pertence a nenhuma faixa da tabela."
    )


def limite_permitido(score: Any, faixas: Iterable[FaixaScore]) -> float:
    """Teto de crédito permitido para o score informado."""
    return faixa_do_score(score, faixas).limite_maximo


def avaliar_solicitacao(
    score: Any,
    limite_atual: float,
    novo_limite_solicitado: float,
    faixas: Iterable[FaixaScore],
) -> DecisaoLimite:
    """Decide se o novo limite pedido cabe no teto da faixa do cliente.

    A decisão é puramente comparativa: aprovado se, e somente se, o valor
    solicitado for menor ou igual ao teto da faixa. `e_aumento` é informativo —
    não altera a decisão, apenas permite ao agente notar que o cliente pediu um
    valor igual ou menor do que já possui.
    """
    valor_score = _validar_score(score)
    teto = limite_permitido(valor_score, faixas)

    atual = _validar_monetario(limite_atual, "Limite atual")
    solicitado = _validar_monetario(novo_limite_solicitado, "Novo limite solicitado")

    cabe = solicitado <= teto
    return DecisaoLimite(
        status=STATUS_APROVADO if cabe else STATUS_REJEITADO,
        limite_permitido=teto,
        limite_atual=atual,
        novo_limite_solicitado=solicitado,
        score=valor_score,
        e_aumento=solicitado > atual,
        motivo=(
            "valor_dentro_do_teto_da_faixa"
            if cabe
            else "valor_acima_do_teto_da_faixa"
        ),
    )


def _validar_monetario(valor: Any, nome: str) -> float:
    """Duplica a validação de `score.py` de propósito: os dois módulos são
    independentes e nenhum deve depender do detalhe interno do outro."""
    if isinstance(valor, bool):
        raise TabelaLimitesError(f"{nome} não pode ser booleano.")
    try:
        numero = float(valor)
    except (TypeError, ValueError) as exc:
        raise TabelaLimitesError(f"{nome} inválido: {valor!r}.") from exc
    if math.isnan(numero) or math.isinf(numero):
        raise TabelaLimitesError(f"{nome} deve ser um número finito.")
    if numero < 0:
        raise TabelaLimitesError(f"{nome} não pode ser negativo.")
    return numero
