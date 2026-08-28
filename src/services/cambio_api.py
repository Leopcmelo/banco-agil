"""
Cliente HTTP de cotação de moedas (ADR-005).

Fonte primária: AwesomeAPI — gratuita, sem chave, JSON direto e latência baixa.
Fallback: open.er-api.com, também gratuita e sem chave.

Busca web genérica (Tavily/SerpAPI) foi descartada: é lenta e não determinística
para um número que precisa estar correto.

Timeout de 5s e 1 retry por fonte. Se as duas falharem, levanta
`CotacaoIndisponivelError` — o agente traduz isso numa mensagem amigável com
alternativa, nunca num stack trace na cara do cliente.
"""

from __future__ import annotations

import logging
import os
import unicodedata
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

import requests

logger = logging.getLogger(__name__)

URL_PRIMARIA = os.getenv(
    "BANCO_AGIL_CAMBIO_URL_PRIMARIA", "https://economia.awesomeapi.com.br/json/last"
)
URL_FALLBACK = os.getenv(
    "BANCO_AGIL_CAMBIO_URL_FALLBACK", "https://open.er-api.com/v6/latest"
)
TIMEOUT_SEGUNDOS = float(os.getenv("BANCO_AGIL_CAMBIO_TIMEOUT", "5"))
TENTATIVAS_POR_FONTE = 2  # a inicial + 1 retry

MOEDA_PADRAO = "USD"
MOEDA_DESTINO_PADRAO = "BRL"


class CambioError(Exception):
    """Falha no serviço de câmbio."""


class MoedaNaoSuportadaError(CambioError):
    """O código de moeda informado não é reconhecido."""


class CotacaoIndisponivelError(CambioError):
    """Nenhuma das fontes respondeu com uma cotação utilizável."""


# Como o cliente fala, mapeado para o código ISO. A normalização é do código,
# nunca do prompt.
_APELIDOS: dict[str, str] = {
    "dolar": "USD",
    "dolar americano": "USD",
    "dolares": "USD",
    "usd": "USD",
    "us$": "USD",
    "euro": "EUR",
    "euros": "EUR",
    "eur": "EUR",
    "libra": "GBP",
    "libra esterlina": "GBP",
    "gbp": "GBP",
    "iene": "JPY",
    "iene japones": "JPY",
    "jpy": "JPY",
    "peso argentino": "ARS",
    "ars": "ARS",
    "franco suico": "CHF",
    "chf": "CHF",
    "dolar canadense": "CAD",
    "cad": "CAD",
    "dolar australiano": "AUD",
    "aud": "AUD",
    "yuan": "CNY",
    "cny": "CNY",
    "bitcoin": "BTC",
    "btc": "BTC",
    "real": "BRL",
    "reais": "BRL",
    "brl": "BRL",
}

NOMES_AMIGAVEIS: dict[str, str] = {
    "USD": "dólar americano",
    "EUR": "euro",
    "GBP": "libra esterlina",
    "JPY": "iene japonês",
    "ARS": "peso argentino",
    "CHF": "franco suíço",
    "CAD": "dólar canadense",
    "AUD": "dólar australiano",
    "CNY": "yuan chinês",
    "BTC": "bitcoin",
    "BRL": "real",
}


@dataclass(frozen=True)
class Cotacao:
    """Uma cotação já normalizada, pronta para o agente verbalizar."""

    moeda_origem: str
    moeda_destino: str
    valor: float
    nome: str
    fonte: str
    atualizado_em: str
    variacao_pct: float | None = None
    valor_venda: float | None = None

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @property
    def descricao(self) -> str:
        """Texto pronto, para o LLM não ter que formatar número."""
        base = (
            f"1 {NOMES_AMIGAVEIS.get(self.moeda_origem, self.moeda_origem)} "
            f"= {self.valor:,.4f} {self.moeda_destino}".replace(",", "@")
            .replace(".", ",")
            .replace("@", ".")
        )
        if self.variacao_pct is not None:
            sinal = "+" if self.variacao_pct >= 0 else ""
            base += f" ({sinal}{self.variacao_pct:.2f}% no dia)".replace(".", ",")
        return base


def _remover_acentos(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def normalizar_moeda(valor: Any, *, padrao: str | None = None) -> str:
    """`'Dólar'`, `'dolares'`, `'usd'` -> `'USD'`.

    Se `valor` for vazio e houver `padrao`, devolve o padrão — o cliente que
    pergunta "qual a cotação?" está falando do dólar.
    """
    if valor is None or not str(valor).strip():
        if padrao:
            return padrao
        raise MoedaNaoSuportadaError("Nenhuma moeda informada.")

    texto = " ".join(_remover_acentos(str(valor)).lower().strip().split())
    if texto in _APELIDOS:
        return _APELIDOS[texto]

    # Um código ISO de 3 letras que não está no dicionário ainda pode ser
    # válido na API; deixamos passar em vez de recusar por desconhecimento.
    if len(texto) == 3 and texto.isalpha():
        return texto.upper()

    raise MoedaNaoSuportadaError(
        f"Moeda não reconhecida: {valor!r}. "
        f"Exemplos aceitos: dólar, euro, libra, iene."
    )


def _get_json(url: str, sessao: requests.Session | None) -> dict[str, Any]:
    """GET com timeout e 1 retry. Levanta CambioError em qualquer falha."""
    cliente = sessao or requests
    ultimo_erro: Exception | None = None

    for tentativa in range(1, TENTATIVAS_POR_FONTE + 1):
        try:
            resposta = cliente.get(url, timeout=TIMEOUT_SEGUNDOS)
            resposta.raise_for_status()
            return resposta.json()
        except (requests.RequestException, ValueError) as exc:
            ultimo_erro = exc
            logger.warning(
                "Cotação: tentativa %d/%d falhou em %s: %s",
                tentativa,
                TENTATIVAS_POR_FONTE,
                url,
                exc,
            )

    raise CambioError(f"Falha ao consultar {url}: {ultimo_erro}")


def _consultar_awesome(
    origem: str, destino: str, sessao: requests.Session | None
) -> Cotacao:
    """AwesomeAPI: /json/last/USD-BRL -> {"USDBRL": {...}}."""
    dados = _get_json(f"{URL_PRIMARIA}/{origem}-{destino}", sessao)

    chave = f"{origem}{destino}"
    if chave not in dados:
        raise CambioError(
            f"Resposta da AwesomeAPI sem a chave {chave}: {sorted(dados)[:5]}"
        )
    bloco = dados[chave]

    try:
        valor = float(bloco["bid"])
    except (KeyError, TypeError, ValueError) as exc:
        raise CambioError(
            f"Cotação ausente ou malformada na AwesomeAPI: {exc}"
        ) from exc

    def _opcional(campo: str) -> float | None:
        try:
            return float(bloco[campo])
        except (KeyError, TypeError, ValueError):
            return None

    return Cotacao(
        moeda_origem=origem,
        moeda_destino=destino,
        valor=valor,
        nome=str(bloco.get("name") or NOMES_AMIGAVEIS.get(origem, origem)),
        fonte="AwesomeAPI",
        atualizado_em=str(bloco.get("create_date") or datetime.now(UTC).isoformat()),
        variacao_pct=_opcional("pctChange"),
        valor_venda=_opcional("ask"),
    )


def _consultar_fallback(
    origem: str, destino: str, sessao: requests.Session | None
) -> Cotacao:
    """open.er-api.com: /v6/latest/USD -> {"rates": {"BRL": 5.42, ...}}."""
    dados = _get_json(f"{URL_FALLBACK}/{origem}", sessao)

    if dados.get("result") == "error":
        raise CambioError(
            f"Fonte de fallback recusou a moeda {origem}: "
            f"{dados.get('error-type', 'motivo desconhecido')}"
        )

    taxas = dados.get("rates")
    if not isinstance(taxas, dict) or destino not in taxas:
        raise CambioError(f"Resposta de fallback sem a taxa {origem}->{destino}.")
    try:
        valor = float(taxas[destino])
    except (TypeError, ValueError) as exc:
        raise CambioError(f"Taxa malformada na fonte de fallback: {exc}") from exc

    return Cotacao(
        moeda_origem=origem,
        moeda_destino=destino,
        valor=valor,
        nome=NOMES_AMIGAVEIS.get(origem, origem),
        fonte="open.er-api.com",
        atualizado_em=str(
            dados.get("time_last_update_utc") or datetime.now(UTC).isoformat()
        ),
    )


def obter_cotacao(
    moeda: Any = MOEDA_PADRAO,
    destino: Any = MOEDA_DESTINO_PADRAO,
    *,
    sessao: requests.Session | None = None,
) -> Cotacao:
    """Cotação de `moeda` em `destino`, tentando primária e depois fallback.

    Levanta `MoedaNaoSuportadaError` para entrada inválida e
    `CotacaoIndisponivelError` se nenhuma fonte responder.
    """
    origem = normalizar_moeda(moeda, padrao=MOEDA_PADRAO)
    alvo = normalizar_moeda(destino, padrao=MOEDA_DESTINO_PADRAO)

    if origem == alvo:
        raise MoedaNaoSuportadaError(
            f"Moeda de origem e destino são iguais ({origem})."
        )

    falhas: list[str] = []
    for nome_fonte, consulta in (
        ("primária", _consultar_awesome),
        ("fallback", _consultar_fallback),
    ):
        try:
            cotacao = consulta(origem, alvo, sessao)
            logger.info(
                "Cotação %s-%s obtida via %s: %s",
                origem,
                alvo,
                cotacao.fonte,
                cotacao.valor,
            )
            return cotacao
        except CambioError as exc:
            # Registrado com contexto e seguimos para a próxima fonte —
            # nada de `except: pass` (regra inviolável nº 8).
            logger.warning(
                "Fonte %s falhou para %s-%s: %s", nome_fonte, origem, alvo, exc
            )
            falhas.append(f"{nome_fonte}: {exc}")

    raise CotacaoIndisponivelError(
        f"Não foi possível obter a cotação {origem}-{alvo}. " + " | ".join(falhas)
    )
