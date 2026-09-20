# ADR-016: o `Completion` carrega o conteúdo do provider como opaco

**Status:** aceita · **Data:** 2026-09-19 · **PR:** `feat/providers`

## Contexto

O `ModelProvider` existe para que o agent loop não dependa de um fornecedor específico. A
semana 8 acrescenta um segundo adapter (OpenAI-compatible, cobrindo Ollama, vLLM e
OpenRouter), então o contrato precisa aguentar mais de um formato de resposta.

Só que a API da Anthropic impõe uma restrição que atravessa essa abstração: os blocos de
conteúdo de uma resposta precisam voltar **verbatim** na requisição seguinte. Isso inclui
blocos de thinking, que carregam assinatura própria. Reescrever, reordenar ou perder um
campo deles invalida o replay, e no Warden o replay não é detalhe: `resume` depois de crash
é reconstruir as mensagens a partir do event log (briefing §12 e §16).

Ou seja, a camada que conhece o formato do provider e a camada que persiste o histórico são
a mesma coisa na prática, e o `core/` fica no meio.

## Decisão

`Completion` normaliza **apenas** o que o loop e o policy engine leem para decidir:
`stop_reason`, `tool_calls`, `usage` e `text`. Todo o resto viaja em `raw_content`, um valor
opaco que o `core/` grava no `task_events.payload` e devolve intocado ao **mesmo** provider
na rodada seguinte.

Duas regras derivadas:

1. Nada fora do provider que produziu um `raw_content` pode inspecioná-lo. O `core/` trata
   como bytes.
2. `raw_content` precisa ser JSON-serializável, porque atravessa uma coluna `JSONB`. O
   adapter da Anthropic faz `model_dump(mode="json", exclude_none=True)` nos blocos, e a
   API aceita esses dicts de volta sem alteração.

## Alternativas consideradas

**Normalizar os blocos no domínio** (`TextBlock | ToolUseBlock | ThinkingBlock`, com
conversão nos dois sentidos em cada adapter). Permitiria trocar de provider no meio de uma
tarefa. Recusada: significa reimplementar o modelo de conteúdo da Anthropic e assumir o
risco de perder campo que ela adicionar numa versão futura. O bloco de thinking é o caso
mais delicado, e é exatamente o que mais dói perder.

**Guardar os objetos da SDK direto no evento.** Recusada: não são JSON-serializáveis, e
amarrariam o formato do event store à versão da biblioteca.

**Não persistir o histórico e remontar a conversa a cada iteração.** Recusada: contraria o
princípio do §12 de que a tarefa é um event log, e torna o resume impossível.

## Consequências

- **Trocar de provider no meio de uma tarefa é proibido por contrato.** O router da semana 8
  escolhe por tarefa, não por iteração. A estratégia `CheapFirstEscalate` do §18 continua
  possível porque ela reexecuta a tarefa inteira com o modelo forte, não continua a mesma.
- O `core/` fica genuinamente livre de detalhe de provider, e a fronteira do §10
  ("`core` não faz: chamar provider direto") vale na prática, não só no diagrama.
- Blocos de thinking sobrevivem ao resume sem código específico.
- O teste do adapter monta respostas com os tipos Pydantic da própria SDK. Mudança de
  formato quebra na atualização da dependência, não em produção.
- Se um dia a troca de provider no meio da tarefa virar requisito, esta ADR é substituída
  por uma que normaliza os blocos, e o custo será escrever os conversores dos dois lados.
