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

from src.core.conversao import ConversaoError, converter, formatar_valor_br
from src.core.validadores import ValorMonetarioInvalidoError, normalizar_valor_monetario
from src.services.cambio_api import (
    CotacaoIndisponivelError,
    MoedaNaoSuportadaError,
    normalizar_moeda,
    obter_cotacao,
    simbolo_da_moeda,
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


@tratar_falhas
def converter_valor(
    contexto: ContextoAtendimento,
    valor: object,
    moeda_origem: str = "BRL",
    moeda_destino: str = "USD",
) -> Resposta:
    """Converte um montante de uma moeda para outra.

    Existe porque o agente não pode multiplicar (regra inviolável nº 1). Antes
    dela, o cliente que pedia "converta meu limite para dólar" recebia só a
    cotação unitária e um pedido de desculpas.

    A cotação é buscada no par NA DIREÇÃO pedida — reais para dólar consulta
    `BRL-USD`, e não o inverso de `USD-BRL`. Inverter introduziria erro e
    ignoraria o spread entre compra e venda.
    """
    sessao = contexto.sessao

    if sessao.bloqueado or sessao.encerrado:
        return bloqueado(
            "Este atendimento já foi encerrado.",
            motivo="atendimento_encerrado",
        )

    try:
        montante = normalizar_valor_monetario(valor, nome="Valor a converter")
    except ValorMonetarioInvalidoError as exc:
        return erro(
            "Não consegui entender o valor a converter. "
            "Pode informar apenas o número, por exemplo 8000?",
            motivo="valor_invalido",
            detalhe=str(exc),
        )

    try:
        origem = normalizar_moeda(moeda_origem, padrao="BRL")
        destino = normalizar_moeda(moeda_destino, padrao="USD")
    except MoedaNaoSuportadaError as exc:
        return erro(
            "Não reconheci uma das moedas. Consigo converter entre real, "
            "dólar, euro, libra e outras principais — quais você quer usar?",
            motivo="moeda_nao_suportada",
            detalhe=str(exc),
        )

    if origem == destino:
        return erro(
            "As duas moedas são a mesma, então não há o que converter.",
            motivo="moedas_iguais",
        )

    try:
        cotacao = obter_cotacao(origem, destino)
    except CotacaoIndisponivelError as exc:
        logger.error("Conversão sem cotação disponível: %s", exc)
        return erro(
            "Não consegui consultar a cotação agora — o serviço de câmbio está "
            "temporariamente indisponível. Pode tentar de novo em alguns "
            "minutos, e enquanto isso posso ajudar com outro assunto.",
            motivo="cotacao_indisponivel",
            detalhe=str(exc),
        )

    try:
        resultado = converter(montante, cotacao.valor, origem, destino)
    except ConversaoError as exc:
        # Cotação absurda vinda da fonte. Falhar é melhor do que devolver um
        # montante errado sobre o dinheiro do cliente.
        logger.error("Conversão recusada com cotação %s: %s", cotacao.valor, exc)
        return erro(
            "A cotação recebida não permite calcular esse valor com segurança. "
            "Prefiro não arriscar um número errado — pode tentar em instantes?",
            motivo="cotacao_invalida",
            detalhe=str(exc),
        )

    descricao = (
        f"{simbolo_da_moeda(origem)} "
        f"{formatar_valor_br(resultado.valor_origem)} = "
        f"{simbolo_da_moeda(destino)} "
        f"{formatar_valor_br(resultado.valor_convertido)}"
    )

    return ok(
        "Conversão realizada.",
        **resultado.as_dict(),
        # Texto pronto: o modelo não formata número (mesma regra da cotação).
        descricao=descricao,
        cotacao_descricao=cotacao.descricao,
        atualizado_em=cotacao.atualizado_em,
        fonte=cotacao.fonte,
    )
