"""
Testes do logging.

A garantia que importa: nem CPF completo nem data de nascimento podem chegar
ao arquivo de log (seção 6 do CLAUDE.md).
"""

from __future__ import annotations

import logging

import pytest

from src.logging_config import (
    FiltroDadosSensiveis,
    configurar_logging,
    resetar_logging,
)

CPF = "00553479326"


@pytest.fixture(autouse=True)
def _logging_limpo():
    resetar_logging()
    yield
    resetar_logging()


def _mascarar(mensagem: str, *args) -> str:
    """Passa uma mensagem pelo filtro e devolve o texto resultante."""
    registro = logging.LogRecord(
        name="src.teste",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=mensagem,
        args=args,
        exc_info=None,
    )
    FiltroDadosSensiveis().filter(registro)
    return registro.getMessage()


# --------------------------------------------------------------------------- #
# 1. Filtro de dados sensíveis
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "mensagem",
    [
        "cliente 00553479326 autenticado",
        "cliente 005.534.793-26 autenticado",
        "cliente 005534793-26 autenticado",
    ],
)
def test_cpf_e_mascarado_em_qualquer_formato(mensagem):
    saida = _mascarar(mensagem)
    assert "00553479326" not in saida
    assert "005.534.793" not in saida
    assert "***" in saida


def test_cpf_interpolado_por_args_tambem_e_mascarado():
    """O filtro age na mensagem já formatada, então pega o %s também."""
    saida = _mascarar("autenticando %s", CPF)
    assert CPF not in saida


def test_data_de_nascimento_e_removida():
    assert "1988-03-14" not in _mascarar("nascimento 1988-03-14")
    assert "14/03/1988" not in _mascarar("nascimento 14/03/1988")
    assert "<data-oculta>" in _mascarar("nascimento 1988-03-14")


def test_mensagem_sem_dado_sensivel_passa_intacta():
    assert _mascarar("score atualizado para 640") == "score atualizado para 640"


def test_valores_monetarios_nao_sao_confundidos_com_cpf():
    saida = _mascarar("limite 12000.00 -> 15000.00")
    assert "12000.00" in saida


def test_mensagem_com_formatacao_invalida_nao_derruba_o_log():
    """Um %s a mais não pode quebrar o atendimento."""
    registro = logging.LogRecord(
        name="src.teste",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg="faltou argumento %s %s",
        args=("um",),
        exc_info=None,
    )
    assert FiltroDadosSensiveis().filter(registro) is True


# --------------------------------------------------------------------------- #
# 2. Configuração do logger
# --------------------------------------------------------------------------- #


def test_configurar_cria_o_arquivo_de_log(tmp_path):
    configurar_logging(diretorio=tmp_path)
    logging.getLogger("src.teste").info("mensagem de teste")
    assert (tmp_path / "app.log").exists()
    assert "mensagem de teste" in (tmp_path / "app.log").read_text(encoding="utf-8")


def test_cpf_nao_chega_ao_arquivo(tmp_path):
    """O teste de ponta a ponta da regra: o disco não pode ter o CPF."""
    configurar_logging(diretorio=tmp_path)
    logging.getLogger("src.teste").info("cliente %s autenticado", CPF)
    conteudo = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert CPF not in conteudo
    assert "***.***.793-26" in conteudo


def test_configuracao_e_idempotente(tmp_path):
    """O Streamlit re-executa o script; sem isso cada linha sairia duplicada."""
    configurar_logging(diretorio=tmp_path)
    configurar_logging(diretorio=tmp_path)
    configurar_logging(diretorio=tmp_path)

    logging.getLogger("src.teste").info("uma vez só")
    conteudo = (tmp_path / "app.log").read_text(encoding="utf-8")
    assert conteudo.count("uma vez só") == 1


def test_diretorio_sem_permissao_nao_derruba_a_aplicacao(tmp_path, monkeypatch):
    """Perder o log é ruim; derrubar o atendimento é pior."""

    def mkdir_que_falha(*args, **kwargs):
        raise OSError("permissão negada")

    monkeypatch.setattr("pathlib.Path.mkdir", mkdir_que_falha)
    logger = configurar_logging(diretorio=tmp_path / "sem-permissao")
    assert logger is not None
    logger.info("ainda funciona")
