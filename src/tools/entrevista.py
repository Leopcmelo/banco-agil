"""
Tools da entrevista de crédito.

A entrevista é conversacional, mas a aritmética não: o agente coleta as cinco
respostas, o código valida cada uma e `src/core/score.py` calcula. O LLM nunca
soma, divide nem compara (regra inviolável nº 1).

Duas tools de propósito:
- `registrar_resposta_entrevista` acumula respostas parciais, uma a uma, na
  ordem em que o cliente quiser responder;
- `finalizar_entrevista` só roda com as cinco respostas presentes, calcula o
  score e persiste.
"""

from __future__ import annotations

import logging

from src.core.limites import limite_permitido
from src.core.score import (
    ScoreInputError,
    calcular_score,
    normalizar_dependentes,
    normalizar_dividas,
    normalizar_emprego,
)
from src.core.validadores import (
    ValorMonetarioInvalidoError,
    mascarar_cpf,
    normalizar_valor_monetario,
)
from src.tools.base import (
    ContextoAtendimento,
    Resposta,
    erro,
    exige_sessao_ativa,
    ok,
    tratar_falhas,
)

logger = logging.getLogger(__name__)

# Rótulos usados nas mensagens de volta ao agente.
PERGUNTAS = {
    "renda_mensal": "renda mensal",
    "tipo_emprego": "tipo de emprego (formal, autônomo ou desempregado)",
    "despesas_fixas": "despesas fixas mensais",
    "num_dependentes": "número de dependentes",
    "tem_dividas": "existência de dívidas ativas",
}


def _validar_campo(campo: str, valor: object) -> object:
    """Valida uma resposta isolada usando o mesmo normalizador do cálculo.

    Validar na hora da resposta — e não só no fim — permite que o agente
    reformule a pergunta imediatamente, em vez de descobrir o problema depois
    de ter coletado tudo.
    """
    if campo in ("renda_mensal", "despesas_fixas"):
        return normalizar_valor_monetario(valor, nome=PERGUNTAS[campo].capitalize())
    if campo == "tipo_emprego":
        normalizar_emprego(valor)
        return valor
    if campo == "num_dependentes":
        normalizar_dependentes(valor)
        return valor
    if campo == "tem_dividas":
        normalizar_dividas(valor)
        return valor
    raise ScoreInputError(f"Campo desconhecido na entrevista: {campo!r}.")


@tratar_falhas
@exige_sessao_ativa
def registrar_resposta_entrevista(
    contexto: ContextoAtendimento,
    renda_mensal: object = None,
    tipo_emprego: object = None,
    despesas_fixas: object = None,
    num_dependentes: object = None,
    tem_dividas: object = None,
) -> Resposta:
    """Guarda uma ou mais respostas da entrevista, validando cada uma.

    Aceita respostas parciais: o cliente pode responder tudo de uma vez ou aos
    poucos. Um campo inválido não descarta os demais.
    """
    entrevista = contexto.sessao.entrevista
    recebidos = {
        "renda_mensal": renda_mensal,
        "tipo_emprego": tipo_emprego,
        "despesas_fixas": despesas_fixas,
        "num_dependentes": num_dependentes,
        "tem_dividas": tem_dividas,
    }

    informados = {k: v for k, v in recebidos.items() if v is not None}
    if not informados:
        return erro(
            "Nenhuma resposta foi informada.",
            motivo="nada_informado",
            faltando=entrevista.faltando,
        )

    aceitos: list[str] = []
    for campo, valor in informados.items():
        try:
            setattr(entrevista, campo, _validar_campo(campo, valor))
            aceitos.append(campo)
        except (ScoreInputError, ValorMonetarioInvalidoError) as exc:
            # Falha só neste campo: o que já foi aceito permanece guardado.
            return erro(
                f"Não consegui interpretar a resposta sobre " f"{PERGUNTAS[campo]}.",
                motivo="resposta_invalida",
                campo=campo,
                detalhe=str(exc),
                aceitos=aceitos,
                faltando=entrevista.faltando,
            )

    faltando = entrevista.faltando
    return ok(
        "Respostas registradas.",
        aceitos=aceitos,
        faltando=faltando,
        faltando_descricao=[PERGUNTAS[c] for c in faltando],
        entrevista_completa=entrevista.completa,
    )


@tratar_falhas
@exige_sessao_ativa
def finalizar_entrevista(contexto: ContextoAtendimento) -> Resposta:
    """Calcula o novo score, persiste em `clientes.csv` e devolve o resultado.

    Também informa o novo teto de crédito, para que o agente de crédito possa
    retomar a análise sem precisar de outra consulta.
    """
    sessao = contexto.sessao
    entrevista = sessao.entrevista

    if not entrevista.completa:
        return erro(
            "Ainda faltam respostas para calcular o score.",
            motivo="entrevista_incompleta",
            faltando=entrevista.faltando,
            faltando_descricao=[PERGUNTAS[c] for c in entrevista.faltando],
        )

    cliente = contexto.repositorio.obter_cliente(sessao.cpf)
    score_anterior = cliente.score

    try:
        resultado = calcular_score(**entrevista.as_dict())
    except ScoreInputError as exc:
        logger.warning("Score não pôde ser calculado: %s", exc)
        return erro(
            "Alguma das respostas ficou inconsistente. "
            "Podemos revisar os dados da entrevista?",
            motivo="dados_inconsistentes",
            detalhe=str(exc),
        )

    atualizado = contexto.repositorio.atualizar_score(cliente.cpf, resultado.score)
    faixas = contexto.repositorio.carregar_faixas_score()
    novo_teto = limite_permitido(atualizado.score, faixas)

    logger.info(
        "Entrevista concluída para o cpf %s: score %s -> %s.",
        mascarar_cpf(cliente.cpf),
        score_anterior,
        resultado.score,
    )

    return ok(
        "Score recalculado.",
        score_anterior=score_anterior,
        score_novo=resultado.score,
        variacao=resultado.score - score_anterior,
        melhorou=resultado.score > score_anterior,
        limite_maximo_para_o_novo_score=novo_teto,
        limite_atual=atualizado.limite_atual,
        componentes=resultado.as_dict(),
    )


@tratar_falhas
@exige_sessao_ativa
def consultar_progresso_entrevista(contexto: ContextoAtendimento) -> Resposta:
    """O que já foi respondido e o que ainda falta perguntar."""
    entrevista = contexto.sessao.entrevista
    return ok(
        "Progresso da entrevista.",
        respondido={
            campo: valor
            for campo, valor in entrevista.as_dict().items()
            if valor is not None
        },
        faltando=entrevista.faltando,
        faltando_descricao=[PERGUNTAS[c] for c in entrevista.faltando],
        entrevista_completa=entrevista.completa,
    )
