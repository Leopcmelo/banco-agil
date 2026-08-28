"""
Estado da sessão de atendimento.

O estado de autenticação vive aqui e é verificado em CÓDIGO, nunca em prompt
(regra inviolável nº 6). O LLM não tem como marcar alguém como autenticado:
`autenticar()` é a única transição possível e só o repositório decide.

O contador de tentativas segue o ADR-003: no máximo 3 tentativas no total.
Quem incrementa é a tool de autenticação; o agente apenas verbaliza o desfecho.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

MAX_TENTATIVAS_AUTH = 3


@dataclass
class DadosEntrevista:
    """Respostas coletadas na entrevista financeira.

    Guardadas cruas, exatamente como o cliente respondeu. Quem normaliza e
    calcula é `src/core/score.py` — aqui é só o acumulador da conversa.
    """

    renda_mensal: Any = None
    tipo_emprego: Any = None
    despesas_fixas: Any = None
    num_dependentes: Any = None
    tem_dividas: Any = None

    CAMPOS = (
        "renda_mensal",
        "tipo_emprego",
        "despesas_fixas",
        "num_dependentes",
        "tem_dividas",
    )

    @property
    def completa(self) -> bool:
        return all(getattr(self, campo) is not None for campo in self.CAMPOS)

    @property
    def faltando(self) -> list[str]:
        return [c for c in self.CAMPOS if getattr(self, c) is None]

    def as_dict(self) -> dict[str, Any]:
        return {campo: getattr(self, campo) for campo in self.CAMPOS}

    def limpar(self) -> None:
        for campo in self.CAMPOS:
            setattr(self, campo, None)


@dataclass
class SessionState:
    """Tudo o que o atendimento precisa lembrar entre uma mensagem e outra."""

    # --- autenticação ---
    # Campo privado com propriedade somente-leitura: `autenticado` é a
    # invariante mais importante do sistema, e antes desta mudança ela era
    # apenas convenção. Qualquer `sessao.autenticado = True` escrito por
    # engano agora falha na hora, em vez de abrir acesso silenciosamente.
    # A única transição continua sendo `autenticar()`.
    _autenticado: bool = False
    cpf: str | None = None
    nome_cliente: str | None = None
    tentativas_auth: int = 0
    bloqueado: bool = False

    # --- crédito ---
    # Chave da última solicitação gravada, para transicionar o status depois.
    ultima_solicitacao_timestamp: str | None = None
    ultimo_pedido_rejeitado: bool = False

    # --- entrevista ---
    entrevista: DadosEntrevista = field(default_factory=DadosEntrevista)

    # --- encerramento ---
    encerrado: bool = False
    motivo_encerramento: str | None = None

    # ---------------------------------------------------------------- #
    # Autenticação
    # ---------------------------------------------------------------- #

    @property
    def autenticado(self) -> bool:
        """Somente leitura. Para autenticar, use `autenticar()`."""
        return self._autenticado

    @property
    def tentativas_restantes(self) -> int:
        return max(0, MAX_TENTATIVAS_AUTH - self.tentativas_auth)

    def registrar_tentativa(self) -> int:
        """Incrementa o contador e bloqueia ao atingir o máximo.

        Retorna quantas tentativas ainda restam.
        """
        self.tentativas_auth += 1
        if self.tentativas_auth >= MAX_TENTATIVAS_AUTH and not self.autenticado:
            self.bloqueado = True
        return self.tentativas_restantes

    def autenticar(self, cpf: str, nome_cliente: str) -> None:
        """Única forma de marcar a sessão como autenticada."""
        if self.bloqueado:
            raise PermissionError(
                "Sessão bloqueada por excesso de tentativas de autenticação."
            )
        self._autenticado = True
        self.cpf = cpf
        self.nome_cliente = nome_cliente

    @property
    def pode_operar(self) -> bool:
        """Porta única usada por toda tool que exponha dado do cliente."""
        return self.autenticado and not self.bloqueado and not self.encerrado

    # ---------------------------------------------------------------- #
    # Encerramento
    # ---------------------------------------------------------------- #

    def encerrar(self, motivo: str) -> None:
        self.encerrado = True
        self.motivo_encerramento = motivo

    # ---------------------------------------------------------------- #
    # Diagnóstico
    # ---------------------------------------------------------------- #

    def resumo_seguro(self) -> dict[str, Any]:
        """Visão do estado sem dado sensível — para log e para a UI."""
        from src.core.validadores import mascarar_cpf

        return {
            "autenticado": self.autenticado,
            "cpf": mascarar_cpf(self.cpf) if self.cpf else None,
            "nome_cliente": self.nome_cliente,
            "tentativas_auth": self.tentativas_auth,
            "tentativas_restantes": self.tentativas_restantes,
            "bloqueado": self.bloqueado,
            "encerrado": self.encerrado,
            "entrevista_completa": self.entrevista.completa,
            "entrevista_faltando": self.entrevista.faltando,
        }
