# CLAUDE.md — Banco Ágil

Contexto permanente para o Claude Code neste repositório. Leia antes de
qualquer tarefa. Se uma instrução de tarefa conflitar com este arquivo, **pare
e pergunte** em vez de escolher sozinho.

---

## 1. O que é o projeto

Sistema de atendimento bancário multiagente para um banco digital fictício.
Quatro agentes especializados, apresentados ao cliente como **um único
atendente**:

| Agente | Escopo |
|---|---|
| Triagem | Autentica (CPF + data de nascimento) e roteia. Porta de entrada. |
| Crédito | Consulta limite, registra pedido de aumento, decide via score. |
| Entrevista de Crédito | Coleta dados financeiros e recalcula o score. |
| Câmbio | Consulta cotação de moeda via API externa. |

Entregável: repositório público + README + UI Streamlit funcionando.

---

## 2. Regras invioláveis

Estas regras não são negociáveis. Código que as viole deve ser rejeitado em
review, mesmo que funcione.

1. **O LLM nunca faz aritmética, comparação numérica ou decisão de negócio.**
   Score, aprovação/rejeição, contagem de tentativas e leitura de faixas são
   funções Python puras chamadas via tool. O agente apenas coleta entradas e
   comunica saídas.
2. **Toda regra de negócio vive em `src/core/`**, sem I/O, sem dependência de
   framework de agente, sem import de LLM. Cobertura de teste obrigatória.
3. **Todo acesso a CSV passa por `src/data/repositories.py`.** Nenhum
   `pd.read_csv` espalhado pelo código. Escrita é atômica (arquivo temporário
   no mesmo diretório + `os.replace`).
4. **CPF é sempre `str`.** Ler CSV com `dtype=str`. Nunca deixar o pandas
   inferir — zeros à esquerda somem e a autenticação quebra silenciosamente
   para parte da base. Normalizar removendo pontuação antes de comparar.
5. **Nenhum agente atua fora do escopo.** O agente de Câmbio não fala de
   limite; o de Crédito não conduz entrevista.
6. **Estado de autenticação é verificado em código, não em prompt.** Toda tool
   que exponha dado de cliente checa `session.autenticado` e falha se falso.
7. **Nenhum segredo no repositório.** Chaves só via `.env`, com `.env.example`
   versionado.
8. **Nada de `except: pass`.** Toda exceção é logada com contexto; o cliente
   recebe uma mensagem clara e uma alternativa.

---

## 3. Decisões arquiteturais (ADRs)

O enunciado tem ambiguidades. Estas são as resoluções adotadas. **Não as mude
sem atualizar esta seção e os testes correspondentes.**

### ADR-001 — Normalização da fórmula de score

O enunciado define score de 0 a 1000, mas a fórmula é ilimitada: com
`renda=15000` e `despesas=0` o resultado é 450.000; com renda zero, desempregado,
3+ dependentes e dívidas ativas, é −70.

Ajustes aplicados:

- **Teto do componente de renda em 500.** A soma máxima dos componentes fixos
  também é 500 (300 formal + 100 sem dependentes + 100 sem dívidas), então o
  máximo teórico passa a ser exatamente 1000 — sem teto artificial no total.
- **Clamp do total em [0, 1000]**, aplicado depois da soma.
- **Multiplicação antes da divisão**: `(renda * 30) / (despesas + 1)` em vez de
  `(renda / (despesas + 1)) * 30`. Matematicamente idêntico, numericamente
  estável na fronteira do teto.
- **Arredondamento half-up explícito** (`floor(x + 0.5)`), não o `round()`
  nativo — que usa banker's rounding e faria `round(0.5) == 0`.
- **Chaves categóricas normalizadas** antes do lookup: minúsculas, sem acentos,
  com sinônimos (`CLT`→formal, `MEI`→autônomo). O cliente digita em linguagem
  natural; a normalização é responsabilidade do código, não do prompt.
- **`peso_dependentes`**: qualquer inteiro ≥ 3 colapsa para a chave `"3+"`.

Limitação conhecida, a documentar no README: perfis realistas ficam na faixa
450–620, porque atingir o teto de renda exige razão renda/despesa ≈ 16,7. Isso
é consequência da fórmula do enunciado, não um bug. Foi mantida por fidelidade
ao escopo; uma curva log-saturada seria a evolução natural.

### ADR-002 — `rejeitado` vs `reprovado`

O enunciado usa `'rejeitado'` na definição da coluna e `'reprovado'` no texto.
**O valor canônico é `rejeitado`.** `reprovado` não aparece em lugar nenhum do
código.

Domínio fechado de `status_pedido`: `pendente`, `aprovado`, `rejeitado`.

### ADR-003 — Tentativas de autenticação

Máximo de **3 tentativas no total** (a inicial + 2 novas), conforme o texto.
O contador vive em `SessionState.tentativas_auth`, é incrementado pela tool de
autenticação e nunca pelo LLM. Ao atingir 3, a tool retorna
`{"status": "bloqueado"}` e todas as demais tools passam a recusar. O agente
apenas verbaliza o encerramento cordial.

### ADR-004 — Transferência implícita

O cliente não pode perceber troca de agente. Consequências práticas:

- Sub-agentes **não** se apresentam, não dão saudação e não se despedem.
- Dados já coletados **nunca** são pedidos de novo (CPF, nascimento, renda).
- Proibidas frases como "vou te transferir", "o setor de crédito", "como
  especialista em câmbio".
- Existe um teste de conversa que roda o fluxo completo e falha se qualquer
  resposta contiver essas marcas.

### ADR-005 — Fonte da cotação de câmbio

Primária: AwesomeAPI (`economia.awesomeapi.com.br`) — gratuita, sem chave,
resposta em JSON, latência baixa. Fallback: segunda fonte configurável.
Timeout de 5s, 1 retry, e mensagem amigável se ambas falharem. Busca web
genérica (Tavily/SerpAPI) foi descartada por ser lenta e não determinística
para um número que precisa estar correto.

### ADR-006 — Persistência

CSV, conforme o enunciado, com um lock de processo na camada de repositório.
O Streamlit re-executa o script a cada interação; sem lock e sem escrita
atômica há risco real de linha duplicada ou arquivo truncado.

---

## 4. Esquemas de dados

> ⚠️ Os esquemas de `clientes.csv` e `score_limite.csv` abaixo são a **proposta
> de trabalho**. Confirmar contra os arquivos reais assim que disponíveis e
> atualizar esta seção — ela é a fonte de verdade para o código.

**`data/clientes.csv`** (leitura e escrita — o score é atualizado)

| coluna | tipo | observação |
|---|---|---|
| `cpf` | str | somente dígitos, com zeros à esquerda |
| `nome` | str | |
| `data_nascimento` | str | ISO `YYYY-MM-DD` |
| `limite_atual` | float | |
| `score` | int | 0–1000, atualizado pela entrevista |

**`data/score_limite.csv`** (somente leitura)

| coluna | tipo | observação |
|---|---|---|
| `score_min` | int | inclusivo |
| `score_max` | int | inclusivo |
| `limite_maximo` | float | teto permitido para a faixa |

Faixas devem ser contíguas e sem sobreposição — validar no carregamento e
falhar cedo se não forem. Limites de faixa são **inclusivos nos dois lados**;
um score fora de todas as faixas é erro de dados, não aprovação silenciosa.

**`data/solicitacoes_aumento_limite.csv`** (append)

| coluna | tipo | observação |
|---|---|---|
| `cpf_cliente` | str | |
| `data_hora_solicitacao` | str | ISO 8601 com timezone |
| `limite_atual` | float | |
| `novo_limite_solicitado` | float | |
| `status_pedido` | str | `pendente` \| `aprovado` \| `rejeitado` |

O pedido é **sempre gravado como `pendente` primeiro**, e só depois atualizado
para `aprovado`/`rejeitado`. O enunciado pede o registro do pedido formal antes
da decisão; gravar já decidido perde a trilha de auditoria.

---

## 5. Estrutura do projeto

```
src/
  core/                    # puro, sem I/O, 100% testado
    score.py               # cálculo do score (ADR-001)
    limites.py             # faixa de score -> limite permitido
    validadores.py         # CPF, data, valores monetários
  data/
    repositories.py        # única porta de entrada para os CSVs
    models.py              # dataclasses: Cliente, Solicitacao
  agents/
    triagem.py
    credito.py
    entrevista.py
    cambio.py
    prompts/               # instruções em arquivos, não hardcoded
  tools/                   # wrappers finos: validam, chamam core/data, retornam dict
  services/
    cambio_api.py          # cliente HTTP com timeout, retry e fallback
  session.py               # SessionState
  logging_config.py
app.py                     # Streamlit
tests/
data/
  seed/                    # cópia imutável para o botão de reset
```

**Tools retornam sempre `dict` serializável, nunca objeto de domínio.** Formato:
`{"status": "ok"|"erro"|"bloqueado", "dados": {...}, "mensagem": "..."}`.

---

## 6. Convenções de código

- Python 3.11+, type hints em toda função pública, `from __future__ import annotations`.
- Docstrings e nomes de domínio em **português**; nomes técnicos em inglês onde
  for idiomático. Comentário explica *por quê*, não *o quê*.
- `ruff` + `black`, linha 88.
- Dependências fixadas com `==` em `requirements.txt`.
- Exceções de domínio próprias (`ScoreInputError`, `ClienteNaoEncontradoError`),
  nunca `Exception` genérica.
- Logging estruturado em `logs/app.log` com `cpf` mascarado (`***.***.789-01`).
  Nunca logar CPF completo nem data de nascimento.

---

## 7. Testes

- `pytest`, arquivos em `tests/`, nomes em português descrevendo a regra.
- Núcleo (`src/core/`) exige teste unitário para caminho feliz, borda e entrada
  inválida. Sem exceção.
- Testes de conversa com LLM mockado — determinísticos, rodam em CI sem chave.
- Rodar `pytest -q` antes de considerar qualquer tarefa concluída.
- **Nunca ajuste um número esperado de teste para fazê-lo passar.** Se o valor
  divergiu, ou a regra mudou (atualize o ADR) ou há um bug.

---

## 8. O que não fazer

- Não colocar fórmula, faixa de score ou limite numérico dentro de prompt.
- Não deixar o LLM decidir se o cliente está autenticado.
- Não usar `st.session_state` como banco de dados — ele é só cache de UI.
- Não criar um quinto agente, um orquestrador extra ou uma camada de abstração
  "para o futuro". O escopo são quatro agentes.
- Não reescrever `src/core/score.py` sem antes ler o ADR-001.
- Não commitar `data/*.csv` alterados durante testes manuais — o seed em
  `data/seed/` é a referência.
