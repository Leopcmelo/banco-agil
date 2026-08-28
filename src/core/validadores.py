"""
Validação e normalização de entradas vindas da conversa.

Módulo puro. O cliente digita em linguagem natural ("meu CPF é 123.456.789-01",
"nasci em 14/03/1988", "quero R$ 12 mil de limite"). Transformar isso em dado
canônico é responsabilidade do código, nunca do prompt.
"""

from __future__ import annotations

import math
import re
import unicodedata
from datetime import date, datetime
from typing import Any

# --------------------------------------------------------------------------- #
# Exceções de domínio
# --------------------------------------------------------------------------- #


class ValidacaoError(ValueError):
    """Raiz das falhas de validação de entrada do cliente."""


class CPFInvalidoError(ValidacaoError):
    """CPF malformado ou com dígitos verificadores incorretos."""


class DataInvalidaError(ValidacaoError):
    """Data de nascimento malformada ou implausível."""


class ValorMonetarioInvalidoError(ValidacaoError):
    """Valor monetário não reconhecível."""


# --------------------------------------------------------------------------- #
# CPF
# --------------------------------------------------------------------------- #

_SO_DIGITOS = re.compile(r"\D")

# Acima disso é quase certamente erro de digitação (ex.: ano com dois dígitos),
# não um cliente real. Não há piso de idade: a normalização também serve para
# comparar a data cadastrada, e recusá-la ali seria falha de sistema, não de
# autenticação.
IDADE_MAXIMA_ANOS = 120


def _digito_verificador(digitos: str) -> str:
    """Calcula um dígito verificador de CPF pelo módulo 11."""
    peso_inicial = len(digitos) + 1
    soma = sum(int(d) * (peso_inicial - i) for i, d in enumerate(digitos))
    resto = (soma * 10) % 11
    return "0" if resto == 10 else str(resto)


def normalizar_cpf(valor: Any) -> str:
    """Devolve o CPF com 11 dígitos, sem pontuação, validando o módulo 11.

    Sempre retorna `str` — nunca int. Zeros à esquerda são significativos
    (regra inviolável nº 4 do CLAUDE.md).
    """
    if valor is None:
        raise CPFInvalidoError("CPF não informado.")
    if isinstance(valor, bool) or isinstance(valor, float):
        raise CPFInvalidoError(f"CPF deve ser texto ou inteiro: {valor!r}.")

    digitos = _SO_DIGITOS.sub("", str(valor))

    if not digitos:
        raise CPFInvalidoError("CPF não contém nenhum dígito.")
    if len(digitos) != 11:
        raise CPFInvalidoError(
            f"CPF deve ter 11 dígitos, mas tem {len(digitos)}."
        )
    # 00000000000, 11111111111... passam no módulo 11 mas não são CPFs reais.
    if len(set(digitos)) == 1:
        raise CPFInvalidoError("CPF com todos os dígitos iguais é inválido.")

    base = digitos[:9]
    esperado = _digito_verificador(base)
    esperado += _digito_verificador(base + esperado)
    if digitos[9:] != esperado:
        raise CPFInvalidoError("Dígitos verificadores do CPF não conferem.")

    return digitos


def formatar_cpf(cpf: str) -> str:
    """`00553479326` -> `005.534.793-26`. Assume CPF já normalizado."""
    d = normalizar_cpf(cpf)
    return f"{d[:3]}.{d[3:6]}.{d[6:9]}-{d[9:]}"


def mascarar_cpf(valor: Any) -> str:
    """`00553479326` -> `***.***.793-26`, para log (seção 6 do CLAUDE.md).

    Nunca levanta exceção: mascarar é usado em caminho de log, inclusive no log
    de erro de um CPF inválido. Um CPF irreconhecível vira `***`.
    """
    digitos = _SO_DIGITOS.sub("", str(valor or ""))
    if len(digitos) != 11:
        return "***"
    return f"***.***.{digitos[6:9]}-{digitos[9:]}"


# --------------------------------------------------------------------------- #
# Data de nascimento
# --------------------------------------------------------------------------- #

_FORMATOS_DATA = (
    "%Y-%m-%d",  # ISO, o formato canônico do CSV
    "%d/%m/%Y",  # o que o brasileiro digita
    "%d-%m-%Y",
    "%d.%m.%Y",
)


def normalizar_data_nascimento(valor: Any, *, hoje: date | None = None) -> str:
    """Aceita ISO ou formato brasileiro e devolve sempre ISO `YYYY-MM-DD`.

    `hoje` é injetável para que os testes de idade sejam determinísticos.
    """
    if valor is None:
        raise DataInvalidaError("Data de nascimento não informada.")

    if isinstance(valor, datetime):
        nascimento = valor.date()
    elif isinstance(valor, date):
        nascimento = valor
    else:
        texto = " ".join(str(valor).strip().split())
        if not texto:
            raise DataInvalidaError("Data de nascimento não informada.")
        for formato in _FORMATOS_DATA:
            try:
                nascimento = datetime.strptime(texto, formato).date()
                break
            except ValueError:
                continue
        else:
            raise DataInvalidaError(
                f"Data de nascimento não reconhecida: {valor!r}. "
                f"Use DD/MM/AAAA."
            )

    referencia = hoje or date.today()
    if nascimento > referencia:
        raise DataInvalidaError("Data de nascimento não pode estar no futuro.")

    idade = calcular_idade(nascimento, hoje=referencia)
    if idade > IDADE_MAXIMA_ANOS:
        raise DataInvalidaError(
            f"Data de nascimento implausível: idade calculada de {idade} anos."
        )

    return nascimento.isoformat()


def calcular_idade(nascimento: date, *, hoje: date | None = None) -> int:
    """Idade em anos completos."""
    referencia = hoje or date.today()
    anos = referencia.year - nascimento.year
    # Ainda não fez aniversário este ano.
    if (referencia.month, referencia.day) < (nascimento.month, nascimento.day):
        anos -= 1
    return anos


def datas_conferem(informada: Any, cadastrada: Any) -> bool:
    """Compara duas datas já em qualquer formato aceito, sem levantar exceção.

    Usada na autenticação: uma data malformada é falha de autenticação, não um
    erro de sistema.
    """
    try:
        return normalizar_data_nascimento(informada) == normalizar_data_nascimento(
            cadastrada
        )
    except DataInvalidaError:
        return False


# --------------------------------------------------------------------------- #
# Valor monetário
# --------------------------------------------------------------------------- #

_MULTIPLICADORES: dict[str, int] = {
    "mil": 1_000,
    "k": 1_000,
    "milhao": 1_000_000,
    "milhoes": 1_000_000,
    "m": 1_000_000,
}

VALOR_MONETARIO_MAXIMO = 1_000_000_000.0


def _remover_acentos(texto: str) -> str:
    decomposto = unicodedata.normalize("NFKD", texto)
    return "".join(c for c in decomposto if not unicodedata.combining(c))


def _interpretar_separadores(numero: str) -> str:
    """Resolve a ambiguidade entre `1.234,56` (BR) e `1,234.56` (US).

    Regra: o separador decimal é o ÚLTIMO símbolo a aparecer, desde que sobrem
    no máximo dois dígitos depois dele. Caso contrário ambos são separadores de
    milhar e o número é inteiro.
    """
    tem_ponto = "." in numero
    tem_virgula = "," in numero

    if tem_ponto and tem_virgula:
        decimal = "," if numero.rfind(",") > numero.rfind(".") else "."
        milhar = "." if decimal == "," else ","
        return numero.replace(milhar, "").replace(decimal, ".")

    if tem_virgula:
        inteiro, _, fracao = numero.rpartition(",")
        # "1,5" é um e meio; "1,500" é mil e quinhentos.
        if len(fracao) <= 2 and inteiro:
            return f"{inteiro.replace(',', '')}.{fracao}"
        return numero.replace(",", "")

    if tem_ponto:
        inteiro, _, fracao = numero.rpartition(".")
        if len(fracao) <= 2 and inteiro:
            return f"{inteiro.replace('.', '')}.{fracao}"
        return numero.replace(".", "")

    return numero


def normalizar_valor_monetario(valor: Any, *, nome: str = "Valor") -> float:
    """Converte `'R$ 12.500,00'`, `'12 mil'`, `'5k'`, `7500` -> float.

    Rejeita negativos, zero, NaN/infinito e valores absurdamente altos.
    """
    if isinstance(valor, bool):
        raise ValorMonetarioInvalidoError(f"{nome} não pode ser booleano.")

    if isinstance(valor, (int, float)):
        numero = float(valor)
    else:
        if valor is None:
            raise ValorMonetarioInvalidoError(f"{nome} não informado.")
        texto = _remover_acentos(str(valor)).lower().strip()
        texto = texto.replace("r$", " ").replace("reais", " ")
        texto = " ".join(texto.split())
        if not texto:
            raise ValorMonetarioInvalidoError(f"{nome} não informado.")

        multiplicador = 1
        for sufixo, fator in _MULTIPLICADORES.items():
            # `\b` não funciona para "5k" colado, então casamos o sufixo no fim.
            padrao = rf"(?:\s|\d){re.escape(sufixo)}\s*$"
            if re.search(padrao, texto) or texto.endswith(f" {sufixo}"):
                multiplicador = fator
                texto = re.sub(rf"{re.escape(sufixo)}\s*$", "", texto).strip()
                break

        # O sinal precisa ser detectado ANTES de descartar a pontuação: sem
        # isso "-500" viraria 500 e um valor negativo passaria como positivo.
        if re.search(r"-\s*\d", texto):
            raise ValorMonetarioInvalidoError(f"{nome} não pode ser negativo.")

        corpo = re.sub(r"[^0-9.,]", "", texto)
        if not re.search(r"\d", corpo):
            raise ValorMonetarioInvalidoError(
                f"{nome} não reconhecido: {valor!r}."
            )
        try:
            numero = float(_interpretar_separadores(corpo)) * multiplicador
        except ValueError as exc:
            raise ValorMonetarioInvalidoError(
                f"{nome} não reconhecido: {valor!r}."
            ) from exc

    if math.isnan(numero) or math.isinf(numero):
        raise ValorMonetarioInvalidoError(f"{nome} deve ser um número finito.")
    if numero < 0:
        raise ValorMonetarioInvalidoError(f"{nome} não pode ser negativo.")
    if numero > VALOR_MONETARIO_MAXIMO:
        raise ValorMonetarioInvalidoError(
            f"{nome} excede o máximo aceito de {VALOR_MONETARIO_MAXIMO:,.0f}."
        )
    return round(numero, 2)
