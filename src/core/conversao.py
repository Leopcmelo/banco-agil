"""
Conversão de valores entre moedas.

Módulo puro: recebe o valor e a cotação já obtidos, devolve o montante. Não
faz rede, não conhece o LLM.

Existe porque o agente não pode multiplicar (regra inviolável nº 1). Sem uma
função como esta, o cliente que pedia "converta meu limite para dólar" ouvia
que a conversão não era possível — o agente estava certo em recusar, mas a
recusa expunha uma lacuna do sistema, não um limite desejável.

Duas decisões de precisão:

- **`Decimal` com meio-para-cima**, não `float` com `round()`. Dinheiro
  arredondado com o *banker's rounding* nativo do Python faria `round(0.125, 2)`
  virar `0,12`, e a diferença aparece somada em cima de valores grandes.
- **Nunca invertemos a cotação.** Para converter reais em dólares pedimos a
  cotação do par `BRL-USD` à fonte, em vez de dividir por `USD-BRL`. Inverter
  introduz erro e ignora o spread entre compra e venda.
"""

from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from decimal import Decimal, InvalidOperation
from typing import Any

CASAS_DECIMAIS = 2

# Acima disso o valor é quase certamente erro de digitação. Espelha o teto de
# `validadores.normalizar_valor_monetario` para que os dois concordem.
VALOR_MAXIMO = 1_000_000_000.0


class ConversaoError(ValueError):
    """Entrada inválida para a conversão de moeda."""


@dataclass(frozen=True)
class Conversao:
    """Resultado da conversão, com as parcelas que a compõem.

    O detalhamento existe para o agente conseguir explicar o número sem
    recalcular nada: ele lê `valor_convertido` e, se quiser, mostra a cotação
    que foi usada.
    """

    valor_origem: float
    moeda_origem: str
    valor_convertido: float
    moeda_destino: str
    cotacao: float

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _validar_numero(valor: Any, nome: str, *, positivo: bool = False) -> Decimal:
    if isinstance(valor, bool):
        raise ConversaoError(f"{nome} não pode ser booleano.")
    try:
        numero = Decimal(str(valor))
    except (InvalidOperation, TypeError, ValueError) as exc:
        raise ConversaoError(f"{nome} inválido: {valor!r}.") from exc

    if not numero.is_finite():
        raise ConversaoError(f"{nome} deve ser um número finito.")
    if numero < 0:
        raise ConversaoError(f"{nome} não pode ser negativo.")
    if positivo and numero == 0:
        raise ConversaoError(f"{nome} deve ser maior que zero.")
    if numero > Decimal(str(VALOR_MAXIMO)):
        raise ConversaoError(f"{nome} excede o máximo aceito de {VALOR_MAXIMO:,.0f}.")
    return numero


def _arredondar(valor: Decimal, casas: int = CASAS_DECIMAIS) -> float:
    """Meio-para-cima explícito, como no motor de score.

    `Decimal.quantize(ROUND_HALF_UP)` seria o caminho natural, mas repetimos a
    mesma mecânica de `score.py` de propósito: os dois arredondamentos do
    sistema precisam concordar, e um deles não pode mudar sem o outro.
    """
    fator = Decimal(10) ** casas
    escalado = valor * fator
    return float(Decimal(math.floor(escalado + Decimal("0.5"))) / fator)


def converter(
    valor: Any,
    cotacao: Any,
    moeda_origem: str = "",
    moeda_destino: str = "",
) -> Conversao:
    """Converte `valor` usando a `cotacao` do par origem→destino.

    A cotação precisa ser a do par NA DIREÇÃO pedida — quem a busca é
    `services/cambio_api.py`. Aqui é só a multiplicação, validada.
    """
    valor_decimal = _validar_numero(valor, "Valor")
    cotacao_decimal = _validar_numero(cotacao, "Cotação", positivo=True)

    convertido = _arredondar(valor_decimal * cotacao_decimal)

    return Conversao(
        valor_origem=_arredondar(valor_decimal),
        moeda_origem=str(moeda_origem).upper(),
        valor_convertido=convertido,
        moeda_destino=str(moeda_destino).upper(),
        cotacao=float(cotacao_decimal),
    )


def formatar_valor_br(valor: Any, casas: int = CASAS_DECIMAIS) -> str:
    """`1234.5` -> `'1.234,50'`.

    Formatar aqui, e não no prompt, é o mesmo princípio das demais regras: o
    modelo recebe o texto pronto e não tem por que reformatar um número.
    """
    numero = _validar_numero(valor, "Valor")
    americano = f"{float(numero):,.{casas}f}"
    # Troca via marcador para não embaralhar os separadores no meio do caminho.
    return americano.replace(",", "\x00").replace(".", ",").replace("\x00", ".")
