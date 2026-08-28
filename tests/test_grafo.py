"""
Testes do grafo de atendimento.

Rodam com um LLM roteirizado — sem chave, sem rede, determinísticos. O que se
verifica aqui é o que NÃO depende do modelo: roteamento, autorização, fiação
das tools e a transferência implícita do ADR-004.

Sobre o alcance destes testes: com um roteiro fixo, o texto das falas é
escolhido pelo próprio teste, então a varredura de marcas de transferência não
prova que o Gemini vai se comportar. O que ela protege de verdade é a
combinação de duas outras coisas testadas aqui: os prompts continuam proibindo
essas marcas, e o grafo não emite nenhuma mensagem própria ao trocar de agente.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from langchain_core.messages import AIMessage

from src.agents import cambio, credito, entrevista, triagem
from src.agents.base import DIRETORIO_PROMPTS, carregar_prompt
from src.agents.grafo import (
    AGENTES,
    PROVEDORES,
    Atendimento,
    _max_tentativas,
    _temperatura_configurada,
    construir_grafo,
    criar_llm,
    texto_da_mensagem,
)
from src.data.repositories import RepositorioBancoAgil
from src.session import SessionState
from src.tools.base import ContextoAtendimento
from tests.apoio import LLMRoteirizado, chama, fala, marcas_de_transferencia

RAIZ = Path(__file__).resolve().parents[1]
SEED = RAIZ / "data" / "seed"

CPF_ANA = "005.534.793-26"
NASC_ANA = "14/03/1988"
# Giovana Sarti: score 150, limite 300, teto da faixa 500.
CPF_GIOVANA = "975.524.877-39"
NASC_GIOVANA = "17/12/1984"


@pytest.fixture()
def contexto(tmp_path: Path) -> ContextoAtendimento:
    destino = tmp_path / "data"
    destino.mkdir()
    for nome in (
        "clientes.csv",
        "score_limite.csv",
        "solicitacoes_aumento_limite.csv",
    ):
        (destino / nome).write_bytes((SEED / nome).read_bytes())
    return ContextoAtendimento(SessionState(), RepositorioBancoAgil(destino))


def atendimento(contexto: ContextoAtendimento, roteiro: list) -> Atendimento:
    return Atendimento(contexto, LLMRoteirizado(roteiro=roteiro))


ROTEIRO_AUTENTICACAO = [
    fala("Olá! Bem-vindo ao Banco Ágil. Pode me informar seu CPF?"),
    chama("autenticar_cliente", cpf=CPF_ANA, data_nascimento=NASC_ANA),
    fala("Tudo certo, Ana. Como posso ajudar você hoje?"),
]


# --------------------------------------------------------------------------- #
# 1. Escopo de cada agente — "nenhum agente atua fora do escopo"
# --------------------------------------------------------------------------- #


def _nomes_das_tools(modulo, contexto) -> set[str]:
    return {t.name for t in modulo.construir_tools(contexto)}


def test_triagem_e_a_unica_com_a_tool_de_autenticacao(contexto):
    assert "autenticar_cliente" in _nomes_das_tools(triagem, contexto)
    for modulo in (credito, entrevista, cambio):
        assert "autenticar_cliente" not in _nomes_das_tools(modulo, contexto)


def test_cambio_nao_alcanca_nada_de_credito(contexto):
    """O agente de Câmbio não fala de limite (regra inviolável nº 5).

    Converter um montante é câmbio, não crédito: a tool recebe o número já
    informado e nunca consulta a conta.
    """
    tools = _nomes_das_tools(cambio, contexto)
    assert "consultar_cotacao" in tools
    assert "converter_valor" in tools
    for proibida in (
        "consultar_limite",
        "solicitar_aumento_limite",
        "registrar_resposta_entrevista",
        "finalizar_entrevista",
    ):
        assert proibida not in tools


def test_credito_nao_conduz_entrevista(contexto):
    tools = _nomes_das_tools(credito, contexto)
    assert "solicitar_aumento_limite" in tools
    assert "registrar_resposta_entrevista" not in tools
    assert "finalizar_entrevista" not in tools
    assert "consultar_cotacao" not in tools
    assert "converter_valor" not in tools


def test_entrevista_nao_aprova_limite(contexto):
    tools = _nomes_das_tools(entrevista, contexto)
    assert "finalizar_entrevista" in tools
    assert "solicitar_aumento_limite" not in tools
    assert "consultar_cotacao" not in tools


def test_todos_os_agentes_podem_transferir_e_encerrar(contexto):
    for modulo in (triagem, credito, entrevista, cambio):
        tools = _nomes_das_tools(modulo, contexto)
        assert "direcionar_atendimento" in tools
        assert "encerrar_atendimento" in tools


def test_o_sistema_tem_exatamente_quatro_agentes():
    """Sem quinto agente e sem orquestrador extra (seção 8 do CLAUDE.md)."""
    assert set(AGENTES) == {"triagem", "credito", "entrevista", "cambio"}


# --------------------------------------------------------------------------- #
# 2. Roteamento — decidido em código, não pelo modelo
# --------------------------------------------------------------------------- #


def test_sem_autenticacao_so_a_triagem_roda(contexto):
    llm = LLMRoteirizado(roteiro=[fala("Pode me informar seu CPF?")])
    at = Atendimento(contexto, llm)
    # O estado sugere 'credito', mas a sessão não está autenticada: o roteador
    # de entrada ignora o estado e manda para a triagem assim mesmo.
    at.estado["agente"] = "credito"
    at.enviar("quero aumentar meu limite")

    # Quem rodou foi a triagem — o prompt de sistema visto é o dela.
    assert "porta de entrada do atendimento" in llm.prompts_vistos[-1]
    assert "Sua tarefa agora" in llm.prompts_vistos[-1]


def test_agente_ativo_muda_apenas_via_direcionar(contexto):
    at = atendimento(
        contexto,
        [
            *ROTEIRO_AUTENTICACAO,
            chama("direcionar_atendimento", assunto="cambio"),
            fala("O dólar está em R$ 5,42."),
        ],
    )
    at.enviar("oi")
    at.enviar(f"{CPF_ANA}, {NASC_ANA}")
    assert at.agente_ativo == "triagem"
    at.enviar("qual a cotação do dólar?")
    assert at.agente_ativo == "cambio"


def test_assunto_invalido_no_direcionar_nao_troca_de_agente(contexto):
    at = atendimento(
        contexto,
        [
            *ROTEIRO_AUTENTICACAO,
            chama("direcionar_atendimento", assunto="emprestimo_consignado"),
            fala("Consigo ajudar com limite de crédito ou cotação de moedas."),
        ],
    )
    at.enviar("oi")
    at.enviar(f"{CPF_ANA}, {NASC_ANA}")
    at.enviar("quero um empréstimo")
    assert at.agente_ativo == "triagem"


def test_sessao_bloqueada_encerra_o_grafo(contexto):
    """Três falhas: o grafo para de rodar e nenhuma tool responde."""
    roteiro = []
    for _ in range(3):
        roteiro.append(
            chama("autenticar_cliente", cpf=CPF_ANA, data_nascimento="01/01/1990")
        )
        roteiro.append(fala("Os dados não conferem. Pode conferir e tentar de novo?"))

    at = atendimento(contexto, roteiro)
    for _ in range(3):
        at.enviar(f"{CPF_ANA}, 01/01/1990")

    assert contexto.sessao.bloqueado is True
    # A partir daqui o grafo não chama mais o modelo — o roteiro vazio provaria
    # o contrário levantando AssertionError.
    at.enviar("deixa eu tentar de novo")
    assert contexto.sessao.autenticado is False


def test_encerramento_dá_exatamente_um_turno_de_despedida(contexto):
    """Encerrar concede UMA fala final ao agente, e nada além dela.

    O enunciado pede tanto a chamada da ferramenta de encerramento quanto uma
    despedida amigável; o turno extra é o que permite as duas coisas.
    """
    at = atendimento(
        contexto,
        [
            *ROTEIRO_AUTENTICACAO,
            chama("encerrar_atendimento", motivo="pedido_do_cliente"),
            fala("Foi um prazer, Ana. Até a próxima!"),
        ],
    )
    at.enviar("oi")
    at.enviar(f"{CPF_ANA}, {NASC_ANA}")
    assert at.enviar("obrigado, é só isso") == "Foi um prazer, Ana. Até a próxima!"

    assert contexto.sessao.encerrado is True
    # Depois disso nada mais é processado; o roteiro vazio confirmaria uma
    # chamada extra ao modelo.
    assert at.enviar("ainda está aí?") == ""


# --------------------------------------------------------------------------- #
# 3. Autorização atravessando o grafo
# --------------------------------------------------------------------------- #


def test_tool_de_credito_recusa_sem_autenticacao(contexto):
    """Mesmo forçando o agente de crédito, a tool recusa."""
    at = atendimento(contexto, [chama("consultar_limite"), fala("...")])
    at.estado["agente"] = "credito"
    # Contorna o roteador de entrada para provar que a defesa é da tool, e não
    # apenas do roteamento.
    grafo = construir_grafo(
        contexto,
        LLMRoteirizado(
            roteiro=[
                chama("consultar_limite"),
                fala("Preciso confirmar seus dados antes."),
            ]
        ),
    )
    estado = grafo.invoke(
        {"messages": [("user", "qual meu limite?")], "agente": "credito"}
    )
    mensagens_de_tool = [m for m in estado["messages"] if m.type == "tool"]
    assert mensagens_de_tool
    assert "nao_autenticado" in str(mensagens_de_tool[0].content)


def test_tool_inexistente_nao_derruba_o_grafo(contexto):
    """O modelo alucina uma tool; o grafo devolve erro e segue."""
    at = atendimento(
        contexto,
        [
            chama("consultar_saldo_bancario", conta="123"),
            fala("Consigo ajudar com limite de crédito e cotação de moedas."),
        ],
    )
    resposta = at.enviar("qual meu saldo?")
    mensagens_de_tool = [m for m in at.estado["messages"] if m.type == "tool"]
    assert "tool_inexistente" in str(mensagens_de_tool[0].content)
    assert resposta  # o atendimento continuou


# --------------------------------------------------------------------------- #
# 4. Fluxo completo: crédito -> entrevista -> crédito
# --------------------------------------------------------------------------- #


def test_fluxo_completo_rejeitado_entrevista_aprovado(contexto):
    """O caminho central do enunciado, de ponta a ponta.

    Giovana tem score 150 (teto R$ 500). Pede R$ 3.000, é rejeitada, faz a
    entrevista, o score sobe e o mesmo pedido é aprovado.
    """
    at = atendimento(
        contexto,
        [
            # --- triagem ---
            fala("Olá! Bem-vindo ao Banco Ágil. Pode me informar seu CPF?"),
            chama("autenticar_cliente", cpf=CPF_GIOVANA, data_nascimento=NASC_GIOVANA),
            fala("Tudo certo, Giovana. Como posso ajudar você hoje?"),
            chama("direcionar_atendimento", assunto="credito"),
            # --- crédito: pedido rejeitado ---
            chama("solicitar_aumento_limite", novo_limite="3000"),
            fala(
                "Não consegui aprovar esse valor agora. Hoje o disponível para "
                "você é de R$ 500,00. Posso fazer algumas perguntas rápidas "
                "sobre sua situação financeira para reavaliar? Leva menos de "
                "um minuto."
            ),
            # --- entrevista ---
            chama("direcionar_atendimento", assunto="entrevista"),
            fala("Qual é a sua renda mensal hoje?"),
            chama("registrar_resposta_entrevista", renda_mensal="9000"),
            fala(
                "E você trabalha com carteira assinada, como autônomo, ou "
                "está sem emprego?"
            ),
            chama("registrar_resposta_entrevista", tipo_emprego="CLT"),
            fala("Quanto você gasta por mês com despesas fixas?"),
            chama("registrar_resposta_entrevista", despesas_fixas="1000"),
            fala("Você tem dependentes? Quantos?"),
            chama("registrar_resposta_entrevista", num_dependentes="0"),
            fala("E hoje você tem alguma dívida ativa?"),
            chama("registrar_resposta_entrevista", tem_dividas="não"),
            # Encadeado sem falar no meio: uma fala encerraria o turno e a
            # reavaliação só aconteceria na mensagem seguinte do cliente.
            chama("finalizar_entrevista"),
            chama("direcionar_atendimento", assunto="credito"),
            # --- crédito de novo: aprovado ---
            chama("solicitar_aumento_limite", novo_limite="3000"),
            fala(
                "Seu score foi atualizado e o limite de R$ 3.000,00 já está "
                "liberado."
            ),
        ],
    )

    at.enviar("oi")
    at.enviar(f"{CPF_GIOVANA}, {NASC_GIOVANA}")
    at.enviar("quero aumentar meu limite para 3000")
    assert at.agente_ativo == "credito"

    at.enviar("sim, quero fazer as perguntas")
    assert at.agente_ativo == "entrevista"

    for resposta in ("9000", "CLT", "1000", "nenhum", "não"):
        at.enviar(resposta)

    # A entrevista terminou e devolveu o controle ao crédito.
    assert at.agente_ativo == "credito"

    cliente = contexto.repositorio.obter_cliente("97552487739")
    assert cliente.score > 700, "a entrevista deveria ter elevado o score"
    assert cliente.limite_atual == 3000.00, "o limite aprovado deveria estar vigente"

    # A trilha de auditoria: dois pedidos, com desfechos diferentes.
    solicitacoes = contexto.repositorio.listar_solicitacoes()
    assert [s.status_pedido for s in solicitacoes] == ["rejeitado", "aprovado"]


def test_o_cliente_nao_e_perguntado_duas_vezes_pelo_mesmo_dado(contexto):
    """Depois de autenticar, o CPF não é pedido de novo (ADR-004)."""
    at = atendimento(
        contexto,
        [
            *ROTEIRO_AUTENTICACAO,
            chama("direcionar_atendimento", assunto="credito"),
            chama("consultar_limite"),
            fala("Seu limite atual é de R$ 8.000,00."),
        ],
    )
    at.enviar("oi")
    at.enviar(f"{CPF_ANA}, {NASC_ANA}")
    at.enviar("qual meu limite?")

    # A tool de autenticação foi chamada uma única vez em toda a conversa.
    chamadas_de_auth = [
        c
        for m in at.estado["messages"]
        if isinstance(m, AIMessage)
        for c in (m.tool_calls or [])
        if c["name"] == "autenticar_cliente"
    ]
    assert len(chamadas_de_auth) == 1


# --------------------------------------------------------------------------- #
# 5. Transferência implícita — ADR-004
# --------------------------------------------------------------------------- #


def test_transferencia_nao_gera_mensagem_visivel_ao_cliente(contexto):
    """O grafo não emite texto próprio ao trocar de agente.

    A única saída de `direcionar_atendimento` é uma ToolMessage, que a UI não
    exibe. Nenhuma fala do assistente é produzida pela transferência em si.
    """
    at = atendimento(
        contexto,
        [
            *ROTEIRO_AUTENTICACAO,
            chama("direcionar_atendimento", assunto="cambio"),
            fala("O dólar está em R$ 5,42."),
        ],
    )
    at.enviar("oi")
    at.enviar(f"{CPF_ANA}, {NASC_ANA}")
    at.enviar("qual a cotação do dólar?")

    mensagem_da_transferencia = [
        m
        for m in at.estado["messages"]
        if m.type == "tool" and m.name == "direcionar_atendimento"
    ]
    assert len(mensagem_da_transferencia) == 1
    # A fala seguinte do assistente já é do novo assunto, sem preâmbulo.
    assert at.ultima_resposta == "O dólar está em R$ 5,42."


def test_nenhuma_fala_do_assistente_contem_marca_de_transferencia(contexto):
    at = atendimento(
        contexto,
        [
            *ROTEIRO_AUTENTICACAO,
            chama("direcionar_atendimento", assunto="credito"),
            chama("consultar_limite"),
            fala("Seu limite atual é de R$ 8.000,00. Posso ajudar em algo mais?"),
        ],
    )
    at.enviar("oi")
    at.enviar(f"{CPF_ANA}, {NASC_ANA}")
    at.enviar("qual meu limite?")

    for mensagem in at.estado["messages"]:
        if isinstance(mensagem, AIMessage) and str(mensagem.content).strip():
            encontradas = marcas_de_transferencia(str(mensagem.content))
            assert not encontradas, (
                f"fala do assistente denuncia a transição: {encontradas} "
                f"em {mensagem.content!r}"
            )


def test_mensagens_das_tools_nao_contem_marca_de_transferencia(contexto):
    """As mensagens geradas pelo CÓDIGO também não podem entregar a transição."""
    from src.tools import (
        autenticar_cliente,
        consultar_cotacao,
        consultar_limite,
        solicitar_aumento_limite,
    )

    autenticar_cliente(contexto, "00553479326", "1988-03-14")
    respostas = [
        consultar_limite(contexto),
        solicitar_aumento_limite(contexto, 99999),
        consultar_cotacao(contexto, "batata"),
    ]
    for resposta in respostas:
        encontradas = marcas_de_transferencia(resposta["mensagem"])
        assert not encontradas, f"{encontradas} em {resposta['mensagem']!r}"


# --------------------------------------------------------------------------- #
# 6. Prompts — a defesa real contra a quebra do ADR-004
# --------------------------------------------------------------------------- #


def test_todos_os_prompts_existem_e_carregam():
    for nome in AGENTES:
        prompt = carregar_prompt(nome)
        assert len(prompt) > 500, f"prompt de {nome} suspeitosamente curto"


def test_prompt_comum_proibe_anunciar_transferencia():
    comum = (DIRETORIO_PROMPTS / "comum.md").read_text(encoding="utf-8").lower()
    for proibicao in ("transferir", "setor", "especialista", "se apresente de novo"):
        assert proibicao in comum, f"o prompt comum não proíbe {proibicao!r}"


def test_prompt_comum_proibe_o_llm_de_calcular():
    """Regra inviolável nº 1 precisa estar dita ao modelo, além de garantida
    em código."""
    comum = (DIRETORIO_PROMPTS / "comum.md").read_text(encoding="utf-8").lower()
    assert "nunca" in comum
    assert "calcula" in comum
    assert "fórmula" in comum


def test_nenhum_prompt_contem_a_formula_do_score():
    """A fórmula não pode viver em prompt (seção 8 do CLAUDE.md).

    Citar o NOME de um campo devolvido por uma tool é legítimo e necessário —
    o agente precisa saber o que ler. O que não pode vazar é o cálculo.
    """
    proibidos = [
        "peso_renda",
        "peso_emprego",
        "peso_dependentes",
        "peso_dividas",
        "despesas + 1",
        "* 30",
    ]
    for arquivo in DIRETORIO_PROMPTS.glob("*.md"):
        texto = arquivo.read_text(encoding="utf-8").lower()
        for termo in proibidos:
            assert termo not in texto, (
                f"{arquivo.name} contém {termo!r} — a fórmula do score não "
                f"pode estar em prompt"
            )


def test_nenhum_prompt_contem_as_faixas_da_tabela_de_limites():
    """As bordas de `score_limite.csv` são dado, não instrução.

    Se aparecerem num prompt, a tabela foi duplicada num lugar que ninguém vai
    lembrar de atualizar quando o CSV mudar.
    """
    bordas = ["299", "499", "699", "849", "850", "score_min", "score_max"]
    for arquivo in DIRETORIO_PROMPTS.glob("*.md"):
        texto = arquivo.read_text(encoding="utf-8")
        for borda in bordas:
            assert borda not in texto, (
                f"{arquivo.name} contém {borda!r} — a faixa de score não pode "
                f"estar em prompt"
            )


def test_prompt_de_cada_agente_inclui_o_comum():
    comum_inicio = (
        (DIRETORIO_PROMPTS / "comum.md").read_text(encoding="utf-8").strip()[:80]
    )
    for nome in AGENTES:
        assert comum_inicio in carregar_prompt(nome)


# --------------------------------------------------------------------------- #
# 7. Extração de texto — content nem sempre é str
# --------------------------------------------------------------------------- #


def test_texto_de_content_string_simples():
    assert texto_da_mensagem(AIMessage(content="Olá!")) == "Olá!"


def test_texto_de_blocos_do_gemini_3():
    """Gemini 3.x devolve blocos; um str() ingênuo vazaria a assinatura."""
    mensagem = AIMessage(
        content=[
            {
                "type": "text",
                "text": "Tudo certo, Giovana. Como posso ajudar?",
                "extras": {"signature": "EoIECv8DARFNMg9EyPD5v8Vol..."},
            }
        ]
    )
    texto = texto_da_mensagem(mensagem)
    assert texto == "Tudo certo, Giovana. Como posso ajudar?"
    assert "signature" not in texto
    assert "extras" not in texto


def test_blocos_nao_textuais_sao_ignorados():
    """Raciocínio interno e chamadas de ferramenta não vão para a conversa."""
    mensagem = AIMessage(
        content=[
            {"type": "thinking", "thinking": "o cliente pediu o limite"},
            {"type": "text", "text": "Seu limite é de R$ 8.000,00."},
            {"type": "tool_use", "name": "consultar_limite", "input": {}},
        ]
    )
    assert texto_da_mensagem(mensagem) == "Seu limite é de R$ 8.000,00."


def test_varios_blocos_de_texto_sao_concatenados():
    mensagem = AIMessage(
        content=[
            {"type": "text", "text": "Primeira."},
            {"type": "text", "text": "Segunda."},
        ]
    )
    assert texto_da_mensagem(mensagem) == "Primeira.\nSegunda."


@pytest.mark.parametrize("conteudo", ["", [], [{"type": "thinking"}]])
def test_conteudo_sem_texto_vira_string_vazia(conteudo):
    assert texto_da_mensagem(AIMessage(content=conteudo)) == ""


def test_content_de_tipo_inesperado_nao_quebra():
    """Defesa contra mudança de formato de provedor: mostra algo em vez de
    engolir a mensagem. `AIMessage` só aceita str ou list, então o caso é
    testado com um dublê."""

    class MensagemEstranha:
        content = 42

    assert texto_da_mensagem(MensagemEstranha()) == "42"


def test_lista_de_strings_puras():
    assert (
        texto_da_mensagem(AIMessage(content=["Olá", "tudo bem?"])) == "Olá\ntudo bem?"
    )


def test_ultima_resposta_usa_o_extrator(contexto):
    """A UI recebe texto limpo mesmo quando o modelo devolve blocos."""
    at = atendimento(contexto, [])
    at.estado["messages"] = [
        AIMessage(
            content=[
                {
                    "type": "text",
                    "text": "Seu limite atual é de R$ 8.000,00.",
                    "extras": {"signature": "abc123"},
                }
            ]
        )
    ]
    assert at.ultima_resposta == "Seu limite atual é de R$ 8.000,00."


# --------------------------------------------------------------------------- #
# 8. Seleção de provedor de LLM
# --------------------------------------------------------------------------- #


def test_provedor_desconhecido_falha_com_mensagem_clara(monkeypatch):
    monkeypatch.setenv("BANCO_AGIL_PROVEDOR", "openai")
    with pytest.raises(RuntimeError, match="Provedor de LLM desconhecido"):
        criar_llm()


def test_anthropic_sem_chave_orienta_o_operador(monkeypatch):
    monkeypatch.setenv("BANCO_AGIL_PROVEDOR", "anthropic")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        criar_llm()


def test_google_sem_chave_orienta_o_operador(monkeypatch):
    monkeypatch.setenv("BANCO_AGIL_PROVEDOR", "google")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    with pytest.raises(RuntimeError, match="GOOGLE_API_KEY"):
        criar_llm()


def test_nome_do_provedor_tolera_caixa_e_espacos(monkeypatch):
    monkeypatch.setenv("BANCO_AGIL_PROVEDOR", "  Anthropic  ")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    # Chega até a checagem de chave, ou seja, o nome foi reconhecido.
    with pytest.raises(RuntimeError, match="ANTHROPIC_API_KEY"):
        criar_llm()


def test_os_dois_provedores_estao_registrados():
    assert set(PROVEDORES) == {"anthropic", "google"}


def test_temperatura_vazia_nao_e_enviada(monkeypatch):
    """gemini-3.x ignora e avisa; Claude Opus 5 recusa com 400. O default é
    não mandar."""
    monkeypatch.setenv("BANCO_AGIL_TEMPERATURA", "")
    assert _temperatura_configurada() is None
    monkeypatch.setenv("BANCO_AGIL_TEMPERATURA", "0.2")
    assert _temperatura_configurada() == 0.2


def test_encerramento_produz_despedida_propria(contexto):
    """Regressão: o cliente se despedia e recebia a fala ANTERIOR repetida.

    O agente chamava encerrar_atendimento sem texto, o roteador cortava o loop
    em END, e `ultima_resposta` andava para trás até achar qualquer fala — a
    cotação do turno anterior. Agora o agente fala uma vez após encerrar.
    """
    at = atendimento(
        contexto,
        [
            *ROTEIRO_AUTENTICACAO,
            chama("direcionar_atendimento", assunto="cambio"),
            fala("O euro está em R$ 6,04."),
            chama("encerrar_atendimento", motivo="pedido_do_cliente"),
            fala("Obrigado pelo contato, Ana. Tenha um ótimo dia!"),
        ],
    )
    at.enviar("oi")
    at.enviar(f"{CPF_ANA}, {NASC_ANA}")
    assert at.enviar("qual a cotação do euro?") == "O euro está em R$ 6,04."

    despedida = at.enviar("só isso, obrigado")
    assert contexto.sessao.encerrado is True
    assert despedida == "Obrigado pelo contato, Ana. Tenha um ótimo dia!"
    assert "euro" not in despedida.lower(), "repetiu a fala do turno anterior"


def test_turno_sem_fala_nao_devolve_resposta_antiga(contexto):
    """`ultima_resposta` é limitada ao turno: melhor vazio que fala estranha."""
    at = atendimento(
        contexto,
        [
            fala("Olá! Pode me informar seu CPF?"),
            chama("encerrar_atendimento", motivo="pedido_do_cliente"),
            fala(""),
        ],
    )
    assert at.enviar("oi") == "Olá! Pode me informar seu CPF?"
    assert at.enviar("deixa pra lá") == ""


def test_encerramento_nao_deixa_o_loop_solto(contexto):
    """Depois de encerrar, o turno acaba mesmo que o modelo peça mais tools."""
    at = atendimento(
        contexto,
        [
            chama("encerrar_atendimento", motivo="pedido_do_cliente"),
            chama("consultar_cotacao", moeda="dolar"),
        ],
    )
    at.enviar("tchau")
    assert contexto.sessao.encerrado is True
    # O roteiro tinha 2 itens e o grafo consumiu os 2; uma terceira chamada
    # levantaria AssertionError no dublê, provando que o loop parou.


def test_tentativas_acima_do_padrao_dos_sdks(monkeypatch):
    """429 e 529 são rotina; duas tentativas não bastaram num teste real."""
    monkeypatch.delenv("BANCO_AGIL_MAX_TENTATIVAS", raising=False)
    assert _max_tentativas() >= 3


def test_tentativas_sao_configuraveis(monkeypatch):
    monkeypatch.setenv("BANCO_AGIL_MAX_TENTATIVAS", "9")
    assert _max_tentativas() == 9


def test_conversao_e_exclusiva_do_cambio(contexto):
    """Nenhum outro agente multiplica valor — nem o de crédito."""
    for modulo in (triagem, credito, entrevista):
        assert "converter_valor" not in _nomes_das_tools(modulo, contexto)


def test_prompt_de_cambio_distingue_cotacao_de_conversao():
    """Regressão da queixa: o agente devolvia a cotação unitária e dizia que
    não fazia a conta."""
    texto = (DIRETORIO_PROMPTS / "cambio.md").read_text(encoding="utf-8")
    assert "converter_valor" in texto
    assert "consultar_cotacao" in texto
    assert "Nunca multiplique você mesmo" in texto
