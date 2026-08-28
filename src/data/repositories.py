"""
Única porta de entrada para os CSVs (regra inviolável nº 3 do CLAUDE.md).

Nenhum `pd.read_csv` deve existir fora deste módulo.

Duas garantias, ambas exigidas pelo ADR-006:

1. **Escrita atômica** — grava num arquivo temporário no MESMO diretório e
   troca com `os.replace`, que é atômico no mesmo sistema de arquivos. Um
   crash no meio da escrita deixa o arquivo antigo intacto, nunca truncado.
2. **Lock de processo** — o Streamlit re-executa o script a cada interação, em
   threads do mesmo processo. Sem lock, duas escritas concorrentes podem
   duplicar ou perder uma linha.

E uma regra transversal: todo CSV é lido com `dtype=str`. Deixar o pandas
inferir tipos apagaria os zeros à esquerda do CPF e quebraria a autenticação
silenciosamente para parte da base (regra inviolável nº 4).
"""

from __future__ import annotations

import logging
import os
import shutil
import tempfile
import threading
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import pandas as pd

from src.core.limites import FaixaScore, validar_tabela
from src.core.validadores import mascarar_cpf, normalizar_cpf
from src.data.models import Cliente, DadosInvalidosError, Solicitacao

logger = logging.getLogger(__name__)

# Um único lock para todos os arquivos. São três CSVs pequenos e as operações
# duram microssegundos; um lock por arquivo só adicionaria risco de deadlock.
_LOCK = threading.RLock()

ARQUIVO_CLIENTES = "clientes.csv"
ARQUIVO_SCORE_LIMITE = "score_limite.csv"
ARQUIVO_SOLICITACOES = "solicitacoes_aumento_limite.csv"

COLUNAS_CLIENTES = ["cpf", "nome", "data_nascimento", "limite_atual", "score"]
COLUNAS_SOLICITACOES = [
    "cpf_cliente",
    "data_hora_solicitacao",
    "limite_atual",
    "novo_limite_solicitado",
    "status_pedido",
]


class RepositorioError(Exception):
    """Falha ao ler ou gravar um arquivo de dados."""


class ArquivoDeDadosError(RepositorioError):
    """Arquivo ausente, ilegível ou com esquema incompatível."""


class ClienteNaoEncontradoError(RepositorioError):
    """Nenhum cliente com o CPF informado."""


class SolicitacaoNaoEncontradaError(RepositorioError):
    """Nenhuma solicitação com a chave informada."""


# --------------------------------------------------------------------------- #
# Leitura e escrita de baixo nível
# --------------------------------------------------------------------------- #


def _ler_csv(caminho: Path, colunas_esperadas: Iterable[str]) -> pd.DataFrame:
    """Lê um CSV inteiro como texto e confere o cabeçalho."""
    if not caminho.exists():
        raise ArquivoDeDadosError(
            f"Arquivo de dados não encontrado: {caminho}. "
            f"Restaure a partir de data/seed/."
        )
    try:
        # dtype=str + keep_default_na=False: sem inferência de tipo e sem NaN.
        df = pd.read_csv(
            caminho, dtype=str, keep_default_na=False, encoding="utf-8"
        )
    except pd.errors.EmptyDataError as exc:
        raise ArquivoDeDadosError(
            f"Arquivo de dados vazio (sem cabeçalho): {caminho}."
        ) from exc
    except (OSError, UnicodeDecodeError, pd.errors.ParserError) as exc:
        raise ArquivoDeDadosError(f"Falha ao ler {caminho}: {exc}") from exc

    faltando = set(colunas_esperadas) - set(df.columns)
    if faltando:
        raise ArquivoDeDadosError(
            f"Colunas ausentes em {caminho.name}: {sorted(faltando)}."
        )
    return df


def _escrever_csv_atomico(caminho: Path, linhas: list[dict[str, str]],
                          colunas: list[str]) -> None:
    """Grava via arquivo temporário no mesmo diretório + `os.replace`.

    O temporário precisa estar no MESMO diretório: `os.replace` só é atômico
    dentro do mesmo sistema de arquivos.
    """
    caminho.parent.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame(linhas, columns=colunas, dtype=str)

    descritor, temporario = tempfile.mkstemp(
        dir=str(caminho.parent), prefix=f".{caminho.name}.", suffix=".tmp"
    )
    try:
        with os.fdopen(descritor, "w", encoding="utf-8", newline="") as arquivo:
            df.to_csv(arquivo, index=False)
            arquivo.flush()
            # Sem fsync, o os.replace pode ser ordenado antes dos dados chegarem
            # ao disco e um crash deixaria um arquivo válido porém vazio.
            os.fsync(arquivo.fileno())
        os.replace(temporario, caminho)
    except OSError as exc:
        raise ArquivoDeDadosError(f"Falha ao gravar {caminho}: {exc}") from exc
    finally:
        # Só sobra se o replace não aconteceu; ignorar aqui é seguro porque o
        # arquivo original permanece intacto de qualquer forma.
        if os.path.exists(temporario):
            try:
                os.unlink(temporario)
            except OSError:
                logger.warning(
                    "Não foi possível remover o temporário %s", temporario
                )


# --------------------------------------------------------------------------- #
# Repositório
# --------------------------------------------------------------------------- #


class RepositorioBancoAgil:
    """Acesso aos três CSVs do sistema.

    Instanciado com o diretório de dados, para que os testes usem `tmp_path` e
    nunca toquem em `data/`.
    """

    def __init__(self, diretorio: str | Path = "data") -> None:
        self.diretorio = Path(diretorio)

    # -- caminhos ---------------------------------------------------------- #

    @property
    def caminho_clientes(self) -> Path:
        return self.diretorio / ARQUIVO_CLIENTES

    @property
    def caminho_score_limite(self) -> Path:
        return self.diretorio / ARQUIVO_SCORE_LIMITE

    @property
    def caminho_solicitacoes(self) -> Path:
        return self.diretorio / ARQUIVO_SOLICITACOES

    # -- clientes ---------------------------------------------------------- #

    def listar_clientes(self) -> list[Cliente]:
        """Carrega a base inteira, validando cada linha."""
        with _LOCK:
            df = _ler_csv(self.caminho_clientes, COLUNAS_CLIENTES)
        clientes = [Cliente.from_row(linha) for linha in df.to_dict("records")]

        cpfs = [c.cpf for c in clientes]
        duplicados = {cpf for cpf in cpfs if cpfs.count(cpf) > 1}
        if duplicados:
            raise ArquivoDeDadosError(
                f"CPF duplicado em {ARQUIVO_CLIENTES}: "
                f"{sorted(mascarar_cpf(c) for c in duplicados)}."
            )
        return clientes

    def buscar_cliente(self, cpf: Any) -> Cliente | None:
        """Retorna o cliente ou `None`. Um CPF malformado também vira `None`:
        na autenticação isso é falha de credencial, não erro de sistema."""
        try:
            procurado = normalizar_cpf(cpf)
        except ValueError:
            logger.info(
                "Busca com CPF malformado (%s) tratada como não encontrado.",
                mascarar_cpf(cpf),
            )
            return None
        for cliente in self.listar_clientes():
            if cliente.cpf == procurado:
                return cliente
        return None

    def obter_cliente(self, cpf: Any) -> Cliente:
        cliente = self.buscar_cliente(cpf)
        if cliente is None:
            raise ClienteNaoEncontradoError(
                f"Cliente não encontrado para o cpf {mascarar_cpf(cpf)}."
            )
        return cliente

    def _salvar_clientes(self, clientes: list[Cliente]) -> None:
        _escrever_csv_atomico(
            self.caminho_clientes,
            [c.to_row() for c in clientes],
            COLUNAS_CLIENTES,
        )

    def atualizar_score(self, cpf: Any, novo_score: int) -> Cliente:
        """Persiste o novo score calculado pela entrevista."""
        with _LOCK:
            clientes = self.listar_clientes()
            procurado = normalizar_cpf(cpf)
            for indice, cliente in enumerate(clientes):
                if cliente.cpf == procurado:
                    atualizado = cliente.com_score(novo_score)
                    clientes[indice] = atualizado
                    self._salvar_clientes(clientes)
                    logger.info(
                        "Score atualizado para o cpf %s: %s -> %s",
                        mascarar_cpf(procurado),
                        cliente.score,
                        novo_score,
                    )
                    return atualizado
        raise ClienteNaoEncontradoError(
            f"Cliente não encontrado para o cpf {mascarar_cpf(cpf)}."
        )

    def atualizar_limite(self, cpf: Any, novo_limite: float) -> Cliente:
        """Aplica o novo limite depois de uma solicitação aprovada."""
        with _LOCK:
            clientes = self.listar_clientes()
            procurado = normalizar_cpf(cpf)
            for indice, cliente in enumerate(clientes):
                if cliente.cpf == procurado:
                    atualizado = cliente.com_limite(novo_limite)
                    clientes[indice] = atualizado
                    self._salvar_clientes(clientes)
                    logger.info(
                        "Limite atualizado para o cpf %s: %.2f -> %.2f",
                        mascarar_cpf(procurado),
                        cliente.limite_atual,
                        atualizado.limite_atual,
                    )
                    return atualizado
        raise ClienteNaoEncontradoError(
            f"Cliente não encontrado para o cpf {mascarar_cpf(cpf)}."
        )

    # -- faixas de score --------------------------------------------------- #

    def carregar_faixas_score(self) -> tuple[FaixaScore, ...]:
        """Carrega e valida `score_limite.csv`.

        A validação roda aqui, no carregamento, para que uma tabela inconsistente
        falhe cedo — e não vire um teto de crédito errado lá na frente.
        """
        colunas = ["score_min", "score_max", "limite_maximo"]
        with _LOCK:
            df = _ler_csv(self.caminho_score_limite, colunas)

        faixas = []
        for linha in df.to_dict("records"):
            try:
                faixas.append(
                    FaixaScore(
                        score_min=int(linha["score_min"]),
                        score_max=int(linha["score_max"]),
                        limite_maximo=float(linha["limite_maximo"]),
                    )
                )
            except (ValueError, TypeError) as exc:
                raise ArquivoDeDadosError(
                    f"Linha inválida em {ARQUIVO_SCORE_LIMITE}: {linha} ({exc})"
                ) from exc

        return validar_tabela(faixas)

    # -- solicitações ------------------------------------------------------ #

    def listar_solicitacoes(self) -> list[Solicitacao]:
        with _LOCK:
            df = _ler_csv(self.caminho_solicitacoes, COLUNAS_SOLICITACOES)
        return [Solicitacao.from_row(linha) for linha in df.to_dict("records")]

    def listar_solicitacoes_do_cliente(self, cpf: Any) -> list[Solicitacao]:
        procurado = normalizar_cpf(cpf)
        return [s for s in self.listar_solicitacoes() if s.cpf_cliente == procurado]

    def registrar_solicitacao(self, solicitacao: Solicitacao) -> Solicitacao:
        """Faz append do pedido. Sempre gravado como `pendente` primeiro."""
        with _LOCK:
            existentes = self.listar_solicitacoes()
            existentes.append(solicitacao)
            _escrever_csv_atomico(
                self.caminho_solicitacoes,
                [s.to_row() for s in existentes],
                COLUNAS_SOLICITACOES,
            )
        logger.info(
            "Solicitação registrada para o cpf %s: %.2f -> %.2f (%s)",
            mascarar_cpf(solicitacao.cpf_cliente),
            solicitacao.limite_atual,
            solicitacao.novo_limite_solicitado,
            solicitacao.status_pedido,
        )
        return solicitacao

    def atualizar_status_solicitacao(
        self, cpf: Any, data_hora_solicitacao: str, novo_status: str
    ) -> Solicitacao:
        """Transiciona um pedido de `pendente` para a decisão final.

        A chave é (cpf, timestamp): o timestamp vem do próprio pedido recém
        gravado, então não há ambiguidade mesmo com vários pedidos do mesmo CPF.
        """
        with _LOCK:
            solicitacoes = self.listar_solicitacoes()
            procurado = normalizar_cpf(cpf)
            for indice, solicitacao in enumerate(solicitacoes):
                if (
                    solicitacao.cpf_cliente == procurado
                    and solicitacao.data_hora_solicitacao == data_hora_solicitacao
                ):
                    atualizada = solicitacao.com_status(novo_status)
                    solicitacoes[indice] = atualizada
                    _escrever_csv_atomico(
                        self.caminho_solicitacoes,
                        [s.to_row() for s in solicitacoes],
                        COLUNAS_SOLICITACOES,
                    )
                    logger.info(
                        "Solicitação do cpf %s em %s: %s -> %s",
                        mascarar_cpf(procurado),
                        data_hora_solicitacao,
                        solicitacao.status_pedido,
                        novo_status,
                    )
                    return atualizada
        raise SolicitacaoNaoEncontradaError(
            f"Solicitação não encontrada para o cpf {mascarar_cpf(cpf)} "
            f"em {data_hora_solicitacao}."
        )

    # -- manutenção -------------------------------------------------------- #

    def restaurar_seed(self, diretorio_seed: str | Path | None = None) -> None:
        """Devolve os CSVs ao estado original — usado pelo botão de reset da UI."""
        origem = Path(diretorio_seed) if diretorio_seed else self.diretorio / "seed"
        if not origem.is_dir():
            raise ArquivoDeDadosError(
                f"Diretório semente não encontrado: {origem}."
            )
        with _LOCK:
            self.diretorio.mkdir(parents=True, exist_ok=True)
            for nome in (
                ARQUIVO_CLIENTES,
                ARQUIVO_SCORE_LIMITE,
                ARQUIVO_SOLICITACOES,
            ):
                arquivo_origem = origem / nome
                if not arquivo_origem.exists():
                    raise ArquivoDeDadosError(
                        f"Arquivo semente ausente: {arquivo_origem}."
                    )
                shutil.copyfile(arquivo_origem, self.diretorio / nome)
        logger.info("Dados restaurados a partir de %s.", origem)


__all__ = [
    "ArquivoDeDadosError",
    "ClienteNaoEncontradoError",
    "DadosInvalidosError",
    "RepositorioBancoAgil",
    "RepositorioError",
    "SolicitacaoNaoEncontradaError",
]
