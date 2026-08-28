"""
Objetos de domínio persistidos em CSV.

São dataclasses puras: sabem se converter de e para linha de CSV, e nada mais.
Não leem arquivo, não conhecem pandas e não chegam à camada de tools — as tools
devolvem sempre `dict` (seção 5 do CLAUDE.md).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from src.core.limites import STATUS_PENDENTE, STATUS_VALIDOS
from src.core.score import SCORE_MAX, SCORE_MIN
from src.core.validadores import (
    mascarar_cpf,
    normalizar_cpf,
    normalizar_data_nascimento,
)


class DadosInvalidosError(ValueError):
    """Uma linha do CSV não respeita o esquema documentado."""


@dataclass(frozen=True)
class Cliente:
    """Uma linha de `clientes.csv`.

    `cpf` é sempre `str` com 11 dígitos (regra inviolável nº 4).
    """

    cpf: str
    nome: str
    data_nascimento: str
    limite_atual: float
    score: int

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Cliente:
        faltando = {"cpf", "nome", "data_nascimento", "limite_atual", "score"} - set(
            row
        )
        if faltando:
            raise DadosInvalidosError(
                f"Colunas ausentes em clientes.csv: {sorted(faltando)}."
            )
        try:
            cpf = normalizar_cpf(row["cpf"])
            nascimento = normalizar_data_nascimento(row["data_nascimento"])
            limite = float(row["limite_atual"])
            score = int(row["score"])
        except (ValueError, TypeError) as exc:
            raise DadosInvalidosError(
                f"Linha inválida em clientes.csv "
                f"(cpf {mascarar_cpf(row.get('cpf'))}): {exc}"
            ) from exc

        if limite < 0:
            raise DadosInvalidosError(
                f"Limite atual negativo para o cpf {mascarar_cpf(cpf)}."
            )
        if not SCORE_MIN <= score <= SCORE_MAX:
            raise DadosInvalidosError(
                f"Score {score} fora de [{SCORE_MIN}, {SCORE_MAX}] "
                f"para o cpf {mascarar_cpf(cpf)}."
            )

        nome = str(row["nome"]).strip()
        if not nome:
            raise DadosInvalidosError(f"Nome vazio para o cpf {mascarar_cpf(cpf)}.")

        return cls(
            cpf=cpf,
            nome=nome,
            data_nascimento=nascimento,
            limite_atual=round(limite, 2),
            score=score,
        )

    def to_row(self) -> dict[str, str]:
        """Serializa para CSV. Tudo vira str para o CPF não perder os zeros."""
        return {
            "cpf": self.cpf,
            "nome": self.nome,
            "data_nascimento": self.data_nascimento,
            "limite_atual": f"{self.limite_atual:.2f}",
            "score": str(self.score),
        }

    @property
    def primeiro_nome(self) -> str:
        return self.nome.split()[0]

    def com_score(self, novo_score: int) -> Cliente:
        """Cópia com o score atualizado — o dataclass é imutável de propósito."""
        if not SCORE_MIN <= novo_score <= SCORE_MAX:
            raise DadosInvalidosError(
                f"Score {novo_score} fora de [{SCORE_MIN}, {SCORE_MAX}]."
            )
        return Cliente(
            cpf=self.cpf,
            nome=self.nome,
            data_nascimento=self.data_nascimento,
            limite_atual=self.limite_atual,
            score=novo_score,
        )

    def com_limite(self, novo_limite: float) -> Cliente:
        if novo_limite < 0:
            raise DadosInvalidosError("Limite não pode ser negativo.")
        return Cliente(
            cpf=self.cpf,
            nome=self.nome,
            data_nascimento=self.data_nascimento,
            limite_atual=round(float(novo_limite), 2),
            score=self.score,
        )


@dataclass(frozen=True)
class Solicitacao:
    """Uma linha de `solicitacoes_aumento_limite.csv`.

    Nasce sempre como `pendente` e só depois transiciona para `aprovado` ou
    `rejeitado` — a trilha de auditoria pedida no enunciado (seção 4 do
    CLAUDE.md).
    """

    cpf_cliente: str
    data_hora_solicitacao: str
    limite_atual: float
    novo_limite_solicitado: float
    status_pedido: str = STATUS_PENDENTE

    def __post_init__(self) -> None:
        if self.status_pedido not in STATUS_VALIDOS:
            raise DadosInvalidosError(
                f"status_pedido inválido: {self.status_pedido!r}. "
                f"Esperado um de {sorted(STATUS_VALIDOS)}."
            )

    @classmethod
    def nova(
        cls,
        cpf_cliente: str,
        limite_atual: float,
        novo_limite_solicitado: float,
        *,
        agora: datetime | None = None,
    ) -> Solicitacao:
        """Cria um pedido `pendente` com timestamp ISO 8601 e timezone.

        `agora` é injetável para que os testes não dependam do relógio.
        """
        momento = agora or datetime.now(UTC).astimezone()
        if momento.tzinfo is None:
            raise DadosInvalidosError(
                "O timestamp da solicitação precisa ter timezone."
            )
        return cls(
            cpf_cliente=normalizar_cpf(cpf_cliente),
            data_hora_solicitacao=momento.isoformat(),
            limite_atual=round(float(limite_atual), 2),
            novo_limite_solicitado=round(float(novo_limite_solicitado), 2),
            status_pedido=STATUS_PENDENTE,
        )

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Solicitacao:
        faltando = {
            "cpf_cliente",
            "data_hora_solicitacao",
            "limite_atual",
            "novo_limite_solicitado",
            "status_pedido",
        } - set(row)
        if faltando:
            raise DadosInvalidosError(
                f"Colunas ausentes em solicitacoes_aumento_limite.csv: "
                f"{sorted(faltando)}."
            )
        try:
            return cls(
                cpf_cliente=normalizar_cpf(row["cpf_cliente"]),
                data_hora_solicitacao=str(row["data_hora_solicitacao"]),
                limite_atual=round(float(row["limite_atual"]), 2),
                novo_limite_solicitado=round(float(row["novo_limite_solicitado"]), 2),
                status_pedido=str(row["status_pedido"]).strip().lower(),
            )
        except (ValueError, TypeError) as exc:
            raise DadosInvalidosError(
                f"Linha inválida em solicitacoes_aumento_limite.csv: {exc}"
            ) from exc

    def to_row(self) -> dict[str, str]:
        return {
            "cpf_cliente": self.cpf_cliente,
            "data_hora_solicitacao": self.data_hora_solicitacao,
            "limite_atual": f"{self.limite_atual:.2f}",
            "novo_limite_solicitado": f"{self.novo_limite_solicitado:.2f}",
            "status_pedido": self.status_pedido,
        }

    def com_status(self, novo_status: str) -> Solicitacao:
        """Transição de `pendente` para a decisão final."""
        return Solicitacao(
            cpf_cliente=self.cpf_cliente,
            data_hora_solicitacao=self.data_hora_solicitacao,
            limite_atual=self.limite_atual,
            novo_limite_solicitado=self.novo_limite_solicitado,
            status_pedido=novo_status,
        )
