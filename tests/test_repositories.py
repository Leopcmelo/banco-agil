"""
Testes da camada de dados.

Todos usam `tmp_path`: nenhum teste toca em `data/` do repositório.
"""

from __future__ import annotations

import csv
import threading
from datetime import UTC, datetime
from pathlib import Path

import pytest

from src.core.limites import STATUS_APROVADO, STATUS_PENDENTE
from src.data.models import Cliente, DadosInvalidosError, Solicitacao
from src.data.repositories import (
    ArquivoDeDadosError,
    ClienteNaoEncontradoError,
    RepositorioBancoAgil,
    SolicitacaoNaoEncontradaError,
)

RAIZ = Path(__file__).resolve().parents[1]
SEED = RAIZ / "data" / "seed"

CPF_COM_ZEROS = "00553479326"
CPF_CARLA = "39819391903"


@pytest.fixture()
def repo(tmp_path: Path) -> RepositorioBancoAgil:
    """Uma cópia limpa dos dados semente, isolada por teste."""
    destino = tmp_path / "data"
    destino.mkdir()
    for nome in (
        "clientes.csv",
        "score_limite.csv",
        "solicitacoes_aumento_limite.csv",
    ):
        (destino / nome).write_bytes((SEED / nome).read_bytes())
    return RepositorioBancoAgil(destino)


# --------------------------------------------------------------------------- #
# 1. Leitura de clientes — a regra do dtype=str
# --------------------------------------------------------------------------- #


def test_listar_clientes_carrega_a_base_semente(repo):
    clientes = repo.listar_clientes()
    assert len(clientes) == 8
    assert all(isinstance(c, Cliente) for c in clientes)


def test_cpf_com_zeros_a_esquerda_sobrevive_a_leitura(repo):
    """Regra inviolável nº 4: sem dtype=str este teste falha."""
    cliente = repo.buscar_cliente(CPF_COM_ZEROS)
    assert cliente is not None
    assert cliente.cpf == CPF_COM_ZEROS
    assert isinstance(cliente.cpf, str)
    assert cliente.cpf.startswith("00")


def test_todos_os_cpfs_da_base_tem_11_digitos(repo):
    for cliente in repo.listar_clientes():
        assert len(cliente.cpf) == 11, f"{cliente.nome} perdeu dígitos do CPF"


def test_buscar_cliente_aceita_cpf_pontuado(repo):
    assert repo.buscar_cliente("005.534.793-26").cpf == CPF_COM_ZEROS


def test_buscar_cliente_inexistente_retorna_none(repo):
    assert repo.buscar_cliente("52998224725") is None


def test_buscar_cliente_com_cpf_malformado_retorna_none(repo):
    """Na autenticação isso é credencial errada, não erro de sistema."""
    assert repo.buscar_cliente("123") is None
    assert repo.buscar_cliente("abc") is None
    assert repo.buscar_cliente(None) is None


def test_obter_cliente_inexistente_levanta_erro(repo):
    with pytest.raises(ClienteNaoEncontradoError):
        repo.obter_cliente("52998224725")


def test_mensagem_de_erro_nao_vaza_o_cpf_completo(repo):
    """Seção 6 do CLAUDE.md: nunca logar nem exibir o CPF inteiro."""
    with pytest.raises(ClienteNaoEncontradoError) as exc:
        repo.obter_cliente("52998224725")
    assert "52998224725" not in str(exc.value)
    assert "***" in str(exc.value)


# --------------------------------------------------------------------------- #
# 2. Arquivos ausentes ou corrompidos
# --------------------------------------------------------------------------- #


def test_arquivo_ausente_falha_com_mensagem_clara(tmp_path):
    repo = RepositorioBancoAgil(tmp_path / "vazio")
    with pytest.raises(ArquivoDeDadosError, match="não encontrado"):
        repo.listar_clientes()


def test_coluna_faltando_falha_cedo(repo):
    repo.caminho_clientes.write_text("cpf,nome\n00553479326,Ana\n", encoding="utf-8")
    with pytest.raises(ArquivoDeDadosError, match="Colunas ausentes"):
        repo.listar_clientes()


def test_arquivo_totalmente_vazio_falha(repo):
    repo.caminho_clientes.write_text("", encoding="utf-8")
    with pytest.raises(ArquivoDeDadosError, match="vazio"):
        repo.listar_clientes()


def test_score_invalido_na_base_falha(repo):
    repo.caminho_clientes.write_text(
        "cpf,nome,data_nascimento,limite_atual,score\n"
        "00553479326,Ana,1988-03-14,8000.00,1500\n",
        encoding="utf-8",
    )
    with pytest.raises(DadosInvalidosError, match="fora de"):
        repo.listar_clientes()


def test_cpf_duplicado_na_base_falha(repo):
    linhas = repo.caminho_clientes.read_text(encoding="utf-8").splitlines()
    repo.caminho_clientes.write_text(
        "\n".join([*linhas, linhas[1]]) + "\n", encoding="utf-8"
    )
    with pytest.raises(ArquivoDeDadosError, match="duplicado"):
        repo.listar_clientes()


# --------------------------------------------------------------------------- #
# 3. Faixas de score
# --------------------------------------------------------------------------- #


def test_carregar_faixas_semente(repo):
    faixas = repo.carregar_faixas_score()
    assert len(faixas) == 5
    assert faixas[0].score_min == 0
    assert faixas[-1].score_max == 1000


def test_tabela_com_buraco_falha_no_carregamento(repo):
    repo.caminho_score_limite.write_text(
        "score_min,score_max,limite_maximo\n0,299,500.00\n400,1000,2000.00\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception, match="Buraco"):
        repo.carregar_faixas_score()


def test_tabela_com_valor_nao_numerico_falha(repo):
    repo.caminho_score_limite.write_text(
        "score_min,score_max,limite_maximo\n0,mil,500.00\n",
        encoding="utf-8",
    )
    with pytest.raises(ArquivoDeDadosError, match="Linha inválida"):
        repo.carregar_faixas_score()


# --------------------------------------------------------------------------- #
# 4. Escrita: score, limite e atomicidade
# --------------------------------------------------------------------------- #


def test_atualizar_score_persiste(repo):
    repo.atualizar_score(CPF_COM_ZEROS, 640)
    assert repo.obter_cliente(CPF_COM_ZEROS).score == 640


def test_atualizar_score_nao_altera_os_outros_clientes(repo):
    antes = {c.cpf: (c.nome, c.limite_atual, c.score) for c in repo.listar_clientes()}
    repo.atualizar_score(CPF_COM_ZEROS, 640)
    depois = {c.cpf: (c.nome, c.limite_atual, c.score) for c in repo.listar_clientes()}
    assert set(antes) == set(depois)
    for cpf in antes:
        if cpf != CPF_COM_ZEROS:
            assert antes[cpf] == depois[cpf]


def test_escrita_preserva_zeros_a_esquerda_no_arquivo(repo):
    """O round-trip completo: se a escrita converter para número, quebra."""
    repo.atualizar_score(CPF_CARLA, 700)
    bruto = repo.caminho_clientes.read_text(encoding="utf-8")
    assert CPF_COM_ZEROS in bruto, "o CPF com zeros à esquerda foi corrompido"

    with open(repo.caminho_clientes, encoding="utf-8", newline="") as f:
        cpfs = [linha["cpf"] for linha in csv.DictReader(f)]
    assert all(len(c) == 11 for c in cpfs)


def test_atualizar_score_de_cliente_inexistente_levanta_erro(repo):
    with pytest.raises(ClienteNaoEncontradoError):
        repo.atualizar_score("52998224725", 500)


def test_score_fora_da_faixa_nao_e_persistido(repo):
    with pytest.raises(DadosInvalidosError):
        repo.atualizar_score(CPF_COM_ZEROS, 1200)
    assert repo.obter_cliente(CPF_COM_ZEROS).score == 720  # inalterado


def test_atualizar_limite_persiste(repo):
    repo.atualizar_limite(CPF_COM_ZEROS, 12000)
    assert repo.obter_cliente(CPF_COM_ZEROS).limite_atual == 12000.00


def test_escrita_nao_deixa_arquivo_temporario(repo):
    repo.atualizar_score(CPF_COM_ZEROS, 640)
    residuos = list(repo.diretorio.glob("*.tmp")) + list(repo.diretorio.glob(".*tmp"))
    assert residuos == []


def test_escritas_concorrentes_nao_perdem_linhas(repo):
    """O lock do ADR-006: 20 threads gravando ao mesmo tempo."""
    def registrar(indice: int) -> None:
        repo.registrar_solicitacao(
            Solicitacao.nova(
                CPF_COM_ZEROS,
                limite_atual=8000,
                novo_limite_solicitado=9000 + indice,
                agora=datetime(2026, 8, 28, 10, indice % 60, indice % 60,
                               tzinfo=UTC),
            )
        )

    threads = [threading.Thread(target=registrar, args=(i,)) for i in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(repo.listar_solicitacoes()) == 20


# --------------------------------------------------------------------------- #
# 5. Solicitações — a trilha de auditoria
# --------------------------------------------------------------------------- #


def test_solicitacao_nasce_pendente(repo):
    s = Solicitacao.nova(CPF_COM_ZEROS, 8000, 12000)
    assert s.status_pedido == STATUS_PENDENTE


def test_registrar_e_depois_decidir(repo):
    """Grava pendente primeiro e só então transiciona (seção 4 do CLAUDE.md)."""
    solicitacao = repo.registrar_solicitacao(
        Solicitacao.nova(CPF_COM_ZEROS, 8000, 12000)
    )
    assert repo.listar_solicitacoes()[0].status_pedido == STATUS_PENDENTE

    atualizada = repo.atualizar_status_solicitacao(
        CPF_COM_ZEROS, solicitacao.data_hora_solicitacao, STATUS_APROVADO
    )
    assert atualizada.status_pedido == STATUS_APROVADO

    gravadas = repo.listar_solicitacoes()
    assert len(gravadas) == 1, "a transição criou uma linha nova em vez de atualizar"
    assert gravadas[0].status_pedido == STATUS_APROVADO


def test_registrar_faz_append_sem_apagar_o_historico(repo):
    for valor in (9000, 10000, 11000):
        repo.registrar_solicitacao(Solicitacao.nova(CPF_COM_ZEROS, 8000, valor))
    assert [s.novo_limite_solicitado for s in repo.listar_solicitacoes()] == [
        9000.0,
        10000.0,
        11000.0,
    ]


def test_atualizar_status_de_solicitacao_inexistente_levanta_erro(repo):
    with pytest.raises(SolicitacaoNaoEncontradaError):
        repo.atualizar_status_solicitacao(
            CPF_COM_ZEROS, "2020-01-01T00:00:00+00:00", STATUS_APROVADO
        )


def test_status_invalido_e_rejeitado(repo):
    """ADR-002: 'reprovado' não existe no domínio."""
    with pytest.raises(DadosInvalidosError):
        Solicitacao.nova(CPF_COM_ZEROS, 8000, 12000).com_status("reprovado")


def test_timestamp_da_solicitacao_tem_timezone(repo):
    s = Solicitacao.nova(CPF_COM_ZEROS, 8000, 12000)
    assert datetime.fromisoformat(s.data_hora_solicitacao).tzinfo is not None


def test_listar_solicitacoes_do_cliente_filtra_por_cpf(repo):
    repo.registrar_solicitacao(Solicitacao.nova(CPF_COM_ZEROS, 8000, 12000))
    repo.registrar_solicitacao(Solicitacao.nova(CPF_CARLA, 20000, 30000))
    assert len(repo.listar_solicitacoes_do_cliente(CPF_COM_ZEROS)) == 1


def test_cpf_da_solicitacao_preserva_zeros_no_arquivo(repo):
    repo.registrar_solicitacao(Solicitacao.nova(CPF_COM_ZEROS, 8000, 12000))
    bruto = repo.caminho_solicitacoes.read_text(encoding="utf-8")
    assert CPF_COM_ZEROS in bruto


# --------------------------------------------------------------------------- #
# 6. Reset a partir do seed
# --------------------------------------------------------------------------- #


def test_restaurar_seed_desfaz_as_alteracoes(tmp_path):
    destino = tmp_path / "data"
    destino.mkdir()
    (destino / "seed").mkdir()
    for nome in (
        "clientes.csv",
        "score_limite.csv",
        "solicitacoes_aumento_limite.csv",
    ):
        conteudo = (SEED / nome).read_bytes()
        (destino / nome).write_bytes(conteudo)
        (destino / "seed" / nome).write_bytes(conteudo)

    repo = RepositorioBancoAgil(destino)
    repo.atualizar_score(CPF_COM_ZEROS, 100)
    repo.registrar_solicitacao(Solicitacao.nova(CPF_COM_ZEROS, 8000, 12000))

    repo.restaurar_seed()

    assert repo.obter_cliente(CPF_COM_ZEROS).score == 720
    assert repo.listar_solicitacoes() == []


def test_restaurar_seed_sem_diretorio_falha(repo):
    with pytest.raises(ArquivoDeDadosError, match="semente"):
        repo.restaurar_seed(repo.diretorio / "inexistente")
