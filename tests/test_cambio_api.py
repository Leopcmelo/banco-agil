"""
Testes do serviço de cotação (ADR-005).

Nenhum teste faz rede: a `requests.Session` é substituída por um dublê que
devolve respostas roteirizadas. Assim o suite roda em CI, offline e rápido.
"""

from __future__ import annotations

import pytest
import requests

from src.services import cambio_api
from src.services.cambio_api import (
    CambioError,
    Cotacao,
    CotacaoIndisponivelError,
    MoedaNaoSuportadaError,
    normalizar_moeda,
    obter_cotacao,
)

RESPOSTA_AWESOME = {
    "USDBRL": {
        "code": "USD",
        "codein": "BRL",
        "name": "Dólar Americano/Real Brasileiro",
        "bid": "5.4210",
        "ask": "5.4230",
        "pctChange": "-0.25",
        "create_date": "2026-08-28 10:30:00",
    }
}

RESPOSTA_FALLBACK = {
    "result": "success",
    "base_code": "USD",
    "rates": {"BRL": 5.4188, "EUR": 0.92},
    "time_last_update_utc": "Fri, 28 Aug 2026 10:30:00 +0000",
}


class RespostaFalsa:
    def __init__(self, payload, status=200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class SessaoFalsa:
    """Devolve uma resposta roteirizada por trecho de URL.

    Um valor `Exception` na rota faz o GET levantar — é assim que simulamos
    timeout e indisponibilidade.
    """

    def __init__(self, rotas: dict[str, object]):
        self.rotas = rotas
        self.chamadas: list[str] = []

    def get(self, url, timeout=None):
        self.chamadas.append(url)
        for trecho, resultado in self.rotas.items():
            if trecho in url:
                if isinstance(resultado, Exception):
                    raise resultado
                return RespostaFalsa(resultado)
        raise requests.ConnectionError(f"rota não roteirizada: {url}")


@pytest.fixture()
def sessao_ok() -> SessaoFalsa:
    return SessaoFalsa({"awesomeapi": RESPOSTA_AWESOME, "er-api": RESPOSTA_FALLBACK})


# --------------------------------------------------------------------------- #
# 1. Normalização de moeda
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "entrada,esperado",
    [
        ("dólar", "USD"),
        ("dolar", "USD"),
        ("DÓLAR", "USD"),
        ("  dólares  ", "USD"),
        ("usd", "USD"),
        ("euro", "EUR"),
        ("libra esterlina", "GBP"),
        ("iene", "JPY"),
        ("bitcoin", "BTC"),
        ("chf", "CHF"),
        ("sek", "SEK"),  # ISO desconhecido do dicionário, mas plausível
    ],
)
def test_normalizar_moeda(entrada, esperado):
    assert normalizar_moeda(entrada) == esperado


def test_moeda_vazia_usa_o_padrao():
    """ "Qual a cotação?" sem dizer qual moeda significa dólar."""
    assert normalizar_moeda("", padrao="USD") == "USD"
    assert normalizar_moeda(None, padrao="USD") == "USD"


@pytest.mark.parametrize("entrada", ["batata", "moeda de ouro", "12345"])
def test_moeda_desconhecida_e_rejeitada(entrada):
    with pytest.raises(MoedaNaoSuportadaError):
        normalizar_moeda(entrada)


def test_moeda_vazia_sem_padrao_e_erro():
    with pytest.raises(MoedaNaoSuportadaError):
        normalizar_moeda("")


def test_origem_igual_ao_destino_e_erro():
    with pytest.raises(MoedaNaoSuportadaError, match="iguais"):
        obter_cotacao("BRL", "BRL")


# --------------------------------------------------------------------------- #
# 2. Fonte primária
# --------------------------------------------------------------------------- #


def test_cotacao_pela_fonte_primaria(sessao_ok):
    c = obter_cotacao("dólar", sessao=sessao_ok)
    assert isinstance(c, Cotacao)
    assert c.moeda_origem == "USD"
    assert c.moeda_destino == "BRL"
    assert c.valor == 5.4210
    assert c.valor_venda == 5.4230
    assert c.variacao_pct == -0.25
    assert c.fonte == "AwesomeAPI"


def test_fallback_nao_e_chamado_quando_a_primaria_responde(sessao_ok):
    obter_cotacao("dólar", sessao=sessao_ok)
    assert len(sessao_ok.chamadas) == 1
    assert "awesomeapi" in sessao_ok.chamadas[0]


def test_descricao_formata_numero_em_padrao_brasileiro(sessao_ok):
    """O LLM não formata número — recebe o texto pronto."""
    descricao = obter_cotacao("dólar", sessao=sessao_ok).descricao
    assert "5,4210" in descricao
    assert "-0,25%" in descricao


def test_descricao_usa_separador_de_milhar_brasileiro():
    c = Cotacao("BTC", "BRL", 350000.5, "Bitcoin", "teste", "2026-08-28")
    assert "350.000,5000" in c.descricao


# --------------------------------------------------------------------------- #
# 3. Retry e fallback
# --------------------------------------------------------------------------- #


def test_retry_na_mesma_fonte_antes_de_desistir():
    """Uma falha transitória não deve derrubar a consulta."""

    class SessaoInstavel:
        def __init__(self):
            self.chamadas = 0

        def get(self, url, timeout=None):
            self.chamadas += 1
            if self.chamadas == 1:
                raise requests.Timeout("timeout simulado")
            return RespostaFalsa(RESPOSTA_AWESOME)

    sessao = SessaoInstavel()
    assert obter_cotacao("dólar", sessao=sessao).fonte == "AwesomeAPI"
    assert sessao.chamadas == 2, "deveria ter feito exatamente 1 retry"


def test_cai_para_o_fallback_quando_a_primaria_morre():
    sessao = SessaoFalsa(
        {
            "awesomeapi": requests.ConnectionError("fonte fora do ar"),
            "er-api": RESPOSTA_FALLBACK,
        }
    )
    c = obter_cotacao("dólar", sessao=sessao)
    assert c.fonte == "open.er-api.com"
    assert c.valor == 5.4188


def test_fallback_tambem_e_usado_em_erro_http_500():
    sessao = SessaoFalsa(
        {"awesomeapi": requests.HTTPError("500"), "er-api": RESPOSTA_FALLBACK}
    )
    assert obter_cotacao("dólar", sessao=sessao).fonte == "open.er-api.com"


def test_json_malformado_na_primaria_cai_para_o_fallback():
    sessao = SessaoFalsa({"awesomeapi": {"inesperado": 1}, "er-api": RESPOSTA_FALLBACK})
    assert obter_cotacao("dólar", sessao=sessao).fonte == "open.er-api.com"


def test_bid_ausente_na_primaria_cai_para_o_fallback():
    sessao = SessaoFalsa(
        {
            "awesomeapi": {"USDBRL": {"name": "sem cotação"}},
            "er-api": RESPOSTA_FALLBACK,
        }
    )
    assert obter_cotacao("dólar", sessao=sessao).fonte == "open.er-api.com"


def test_as_duas_fontes_fora_levanta_erro_de_dominio():
    sessao = SessaoFalsa(
        {
            "awesomeapi": requests.ConnectionError("fora"),
            "er-api": requests.Timeout("fora"),
        }
    )
    with pytest.raises(CotacaoIndisponivelError) as exc:
        obter_cotacao("dólar", sessao=sessao)
    # A mensagem precisa dizer o que falhou, para o log ter contexto.
    assert "primária" in str(exc.value)
    assert "fallback" in str(exc.value)


def test_erro_de_rede_cru_nunca_vaza_para_o_chamador():
    """O agente trata CambioError, não requests.ConnectionError."""
    sessao = SessaoFalsa(
        {
            "awesomeapi": requests.ConnectionError("fora"),
            "er-api": requests.ConnectionError("fora"),
        }
    )
    with pytest.raises(CambioError):
        obter_cotacao("dólar", sessao=sessao)


def test_fallback_que_recusa_a_moeda_gera_indisponibilidade():
    sessao = SessaoFalsa(
        {
            "awesomeapi": requests.ConnectionError("fora"),
            "er-api": {"result": "error", "error-type": "unsupported-code"},
        }
    )
    with pytest.raises(CotacaoIndisponivelError):
        obter_cotacao("xyz", sessao=sessao)


def test_timeout_configurado_e_repassado_ao_requests(monkeypatch):
    registrados: list[float] = []

    class SessaoQueRegistraTimeout:
        def get(self, url, timeout=None):
            registrados.append(timeout)
            return RespostaFalsa(RESPOSTA_AWESOME)

    monkeypatch.setattr(cambio_api, "TIMEOUT_SEGUNDOS", 5.0)
    obter_cotacao("dólar", sessao=SessaoQueRegistraTimeout())
    assert registrados == [5.0]


# --------------------------------------------------------------------------- #
# 4. Outras moedas
# --------------------------------------------------------------------------- #


def test_euro_monta_a_url_correta(sessao_ok):
    obter_cotacao("euro", sessao=sessao_ok)
    assert sessao_ok.chamadas[0].endswith("/EUR-BRL")


def test_par_arbitrario(sessao_ok):
    sessao = SessaoFalsa(
        {"awesomeapi": {"EURUSD": {"bid": "1.0850", "name": "Euro/Dólar"}}}
    )
    c = obter_cotacao("euro", "dólar", sessao=sessao)
    assert c.moeda_origem == "EUR"
    assert c.moeda_destino == "USD"
    assert c.valor == 1.0850


# --------------------------------------------------------------------------- #
# 5. Símbolos de moeda
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "codigo,esperado",
    [("BRL", "R$"), ("USD", "US$"), ("EUR", "€"), ("brl", "R$"), ("SEK", "SEK")],
)
def test_simbolo_da_moeda(codigo, esperado):
    """Sem símbolo conhecido, o próprio código ISO serve."""
    from src.services.cambio_api import simbolo_da_moeda

    assert simbolo_da_moeda(codigo) == esperado


def test_simbolo_evita_o_singular_errado():
    """ "8.000,00 real" está errado em português; o símbolo não tem plural."""
    from src.services.cambio_api import simbolo_da_moeda

    assert simbolo_da_moeda("BRL") == "R$"
