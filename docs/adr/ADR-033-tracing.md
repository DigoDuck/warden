# ADR-033: traces por tarefa, o que entra num span e o que nunca entra

**Status:** aceita · **Data:** 2026-10-11 · **PR:** `feat/tracing`

## Contexto

A semana 7 pede que cada tarefa vire um trace no Jaeger (briefing §20) sem que o backend de
traces passe a ser um lugar onde segredo ou código do repositório alvo vazam. Quem lê um trace
não é quem tem acesso à tarefa, e um span é copiado, indexado e retido por sistemas que o
control plane não controla. Três fatos do código pesaram:

1. A redação existente só conhecia o **valor configurado** do token do GitHub
   (`broker.redact`) e **nomes de chave** sensíveis (`redact_args`). Um token lido de um arquivo
   pelo agente, ou ecoado por uma ferramenta, passava para eventos e `args_safe` intacto.
2. Uma tarefa é reivindicada (claimed) mais de uma vez: depois de uma aprovação, depois de um
   crash. Cada claim entra de novo em `run_claimed_task`. Sem cuidado, cada claim seria um trace
   separado e a aprovação de uma publicação apareceria como uma tarefa nova.
3. O código não tinha nenhuma dependência de OpenTelemetry nem de logging estruturado.

## Decisão

### O que entra num span

**Uma allow-list de chaves, e só ela** (`telemetry.ALLOWED_ATTRIBUTES`). `safe_attributes` é o
único caminho de um valor até um span; uma chave fora da lista **levanta `ValueError`**, em vez de
ser descartada: acrescentar o que um span carrega é uma decisão revisada, uma linha num único
arquivo, nunca um efeito colateral de passar um dicionário adiante. Descartar em silêncio
esconderia o erro até alguém notar um atributo faltando.

Entra: ids (`task_id`), números (iteração, tokens, custo, latência, duração), nomes
(ferramenta, modelo, provedor, imagem, tipo de verificação), o efeito e as regras de uma decisão
de política, o `args_hash`, um status curto. Todo valor string passa por `broker.redact` e só
depois é truncado em 200 caracteres (cortar antes poderia partir um token ao meio e escondê-lo
do detector).

**Nunca entra, e não existe chave para isso:** argumentos de ferramenta, saída de ferramenta,
argv do agente, conteúdo de arquivo, `raw_content`, prompt, saída do modelo. O `args_hash` (o
mesmo SHA-256 dos argumentos já redigidos que `tool_calls.args_hash` guarda, com uma só
definição em `core.events.args_hash`) identifica uma chamada sem carregá-la, e deixa uma linha do
trace e uma linha do banco se encontrarem.

**Exceções registram o tipo, nunca a mensagem.** O padrão do OpenTelemetry copia
`str(exception)` para um evento do span, e esse texto é livre: um `ToolError` diz qual caminho
recusou, um erro de driver cita o comando. O span fica `ERROR` com `error.type`. Uma exceção com
`trace_expected = True` (o `_RunStopped` do loop, que é como uma tarefa estaciona em
`WAITING_APPROVAL`) é controle de fluxo e não é erro.

### Redator por forma

`broker.redact` passa a reconhecer, além do valor configurado, a **forma** de: tokens do GitHub
(`ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_`), chaves `sk-…` (Anthropic e estilo OpenAI),
chaves de acesso AWS (`AKIA`/`ASIA`), JWT e blocos PEM de chave privada (até o `END` ou, se o
texto foi truncado antes, até o fim da string). `redact_args` mantém a regra por nome de chave e
agora aplica a varredura por forma aos valores restantes, aninhados inclusive. Como é a mesma
função, eventos, `args_safe`, `tool_calls.result_summary`, evidência e atributos de span ganham os
detectores juntos.

### Um trace por tarefa

O primeiro claim cria o trace e o span raiz `task.run`, e grava `tasks.trace_id` (coluna que já
existia) e o id do span raiz em `tasks.state["root_span_id"]`. Cada claim seguinte reconstrói um
`SpanContext` remoto com esses ids e abre o seu próprio `task.run` **como filho** do primeiro. No
Jaeger a tarefa continua sendo um trace, com um `task.run` por claim, inclusive depois de uma
aprovação. Sem tracing ligado nada é gravado e a API devolve `trace_id: null` (a UI não mostra o
link).

### Árvore e pontos de instrumentação

`task.run` → `planner.plan`, `agent.iteration` → `model.generate`, `policy.evaluate`,
`tool.execute` → `sandbox.exec`; `verify.run` (um por verificação), `review`, `publish`. Os spans
são escritos à mão nos pontos do §20, sem auto-instrumentação, para que o que entra num span seja
decidido em um lugar só. `tool.execute` cobre a execução da ferramenta no gateway (depois da
verificação do token), não a decisão. Uma chamada negada tem `policy.evaluate` e não tem
`tool.execute`: o que não executou não aparece como execução.

### Exportação

OTLP sobre **HTTP/protobuf** (`opentelemetry-exporter-otlp-proto-http`), não gRPC: uma dependência
nativa a menos no worker, e o Jaeger recebe OTLP/HTTP na 4318. Configuração pelas variáveis
padrão `OTEL_*`. **Sem `OTEL_EXPORTER_OTLP_ENDPOINT` não há exportador** e todo span é no-op.
O `BatchSpanProcessor` só enfileira no `on_end` e exporta numa thread própria: coletor fora do ar
custa uma linha de log nessa thread, não atraso nem exceção na tarefa. Há um teste que aponta o
exportador para uma porta fechada e roda uma tarefa de verdade.

O provider fica num módulo (`telemetry.configure`), não no `set_tracer_provider` global do
OpenTelemetry, que só pode ser chamado uma vez por processo.

## Limites conhecidos

- **O redator por forma é heurística.** Não acha um segredo sem prefixo reconhecível. A defesa não
  depende só dele: o modelo nunca vê credencial (ADR-006) e span é allow-list de chaves.
- **Não há `context.build` nem `mcp.call`**, do §20: os módulos não existem ainda.
- **A API não tem spans.** Só o worker é instrumentado; `api/app.py` fica de fora desta trilha.
- O span raiz termina quando o primeiro claim termina; os `task.run` seguintes começam depois dele
  (Jaeger mostra assim: filhos fora do intervalo do pai).
- Sem amostragem: toda tarefa é exportada. Aceitável no volume do projeto.
- Logs estruturados com `trace_id` (a segunda metade do item da semana 7) ficam para a próxima
  onda, que depende do contexto de trace daqui.
