"""
Tool de câmbio.

Diferente das demais, NÃO exige autenticação: cotação é informação pública e
não expõe nenhum dado da conta. Exigir login para dizer o preço do dólar seria
atrito sem ganho de segurança.

O bloqueio por excesso de tentativas, porém, continua valendo — uma sessão
bloqueada está encerrada para todos os efeitos.
"""

from __future__ import annotations

import logging

from src.services.cambio_api import (
    CotacaoIndisponivelError,
    MoedaNaoSuportadaError,
    obter_cotacao,
)
from src.tools.base import (
    ContextoAtendimento,
    Resposta,
    bloqueado,
    erro,
    ok,
    tratar_falhas,
)

logger = logging.getLogger(__name__)


@tratar_falhas
def consultar_cotacao(
    contexto: ContextoAtendimento,
    moeda: str = "USD",
    moeda_destino: str = "BRL",
) -> Resposta:
    """Cotação atual de uma moeda. Sem moeda informada, assume dólar."""
    sessao = contexto.sessao

    if sessao.bloqueado or sessao.encerrado:
        return bloqueado(
            "Este atendimento já foi encerrado.",
            motivo="atendimento_encerrado",
        )

    try:
        cotacao = obter_cotacao(moeda, moeda_destino)
    except MoedaNaoSuportadaError as exc:
        return erro(
            "Não reconheci essa moeda. Consigo consultar dólar, euro, libra, "
            "iene e outras moedas principais — qual delas você prefere?",
            motivo="moeda_nao_suportada",
            detalhe=str(exc),
        )
    except CotacaoIndisponivelError as exc:
        # As duas fontes falharam. O cliente recebe alternativa, não stack trace.
        logger.error("Cotação indisponível: %s", exc)
        return erro(
            "Não consegui consultar a cotação agora — o serviço de câmbio está "
            "temporariamente indisponível. Pode tentar de novo em alguns "
            "minutos, e enquanto isso posso ajudar com outro assunto.",
            motivo="cotacao_indisponivel",
            detalhe=str(exc),
        )

    return ok(
        "Cotação obtida.",
        moeda_origem=cotacao.moeda_origem,
        moeda_destino=cotacao.moeda_destino,
        valor=cotacao.valor,
        # Texto já formatado em padrão brasileiro: o LLM não formata número.
        descricao=cotacao.descricao,
        variacao_pct=cotacao.variacao_pct,
        atualizado_em=cotacao.atualizado_em,
        fonte=cotacao.fonte,
    )
