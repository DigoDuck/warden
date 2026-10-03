# ADR-031: o Planner é conselho, não gate

**Status:** aceita · **Data:** 2026-10-03 · **PR:** `feat/planner`

## Contexto

O briefing (§8, E10) descreve o fluxo "issue vira PR com evidência" como Planner → Worker →
Verifier → PR. Até aqui o Planner não existia: o coder recebia a spec crua e decidia sozinho o
que ler e o que mudar. O critério do MVP "Planner → worker → verifier → PR" estava aberto, e o
briefing (§22) pede saída estruturada: passos, arquivos prováveis, riscos e testes a criar.

A pergunta de desenho é a mesma da ADR-010, com a resposta oposta: o verdict do reviewer **decide**
o status da tarefa; o plano deve decidir alguma coisa?

## Decisão

### 1. O plano é conselho, nunca gate

`Planner.plan(spec) -> PlanResult` espelha `Reviewer.review` (`plan/planner.py`): uma chamada ao
**mesmo provider** do worker, system prompt próprio, resposta como tool call estruturada
`submit_plan {steps, likely_files, risks, tests_to_add}` validada com Pydantic (`steps` com ao
menos um item; as outras listas podem ser vazias, porque "sem riscos" é uma resposta).

Ao contrário do verdict, **um plano malformado não derruba a tarefa**. Sem tool call, tool com
outro nome ou argumentos com formato errado: grava `plan.recorded {malformed_reason, cost_usd}` e o
coder segue sem plano. Nada a jusante confia no plano: os checks e o reviewer julgam o diff, não a
intenção. Falhar uma tarefa porque o modelo errou o formato de um conselho trocaria um custo
pequeno (um plano a menos) por um grande (uma tarefa perdida).

### 2. Onde ele entra no loop

No ramo de uma tarefa nova (`core/loop.py`), depois do checkpoint (a) e antes da iteração 1:

```text
checkpoint → planner.plan(spec) → model_calls(purpose="planner") + plan.recorded → checkpoint
```

- **Sem transação aberta durante a chamada** (ADR-019): tudo é commitado antes. A linha de
  `model_calls` e o evento `plan.recorded` commitam juntos depois, como no verdict.
- **O coder lê o plano como segunda `UserMessage`**, depois da spec, rotulado como gerado pelo
  planner e não verificado, para ser usado como sugestão e não como instrução. A mensagem é montada
  por `plan_message` a partir do dict gravado, o mesmo texto no run ao vivo e no replay.
- **O custo soma em `spent`.** O `max_usd` já vale a partir da iteração 1: o teto é checado depois
  da primeira chamada do coder, e a do planner já está na conta.
- **O planner só enxerga a spec.** Não tem tools nem acesso ao repositório, então `likely_files`
  é palpite. Dar tools ao planner seria um segundo agent loop; a pergunta que ele responde ("por
  onde eu começaria?") cabe numa chamada.

### 3. Replay e crash

`core/replay.py` reconstrói a mesma segunda mensagem a partir de `plan.recorded` e soma o custo
de volta. `ResumeState.plan_recorded` marca que a chamada já foi paga.

| Estado do log quando o worker morre | O que o resume faz |
|---|---|
| só `task.created` (morreu dentro da chamada) | planeja de novo, **uma** vez; a chamada perdida não deixou rastro |
| `plan.recorded` gravado | reaproveita o plano e o custo; não chama o planner |
| já há turno do coder (planner ausente na época) | **não** planeja: um plano no meio de uma conversa que ele nunca moldou é pior que nenhum |

Provado matando um processo de worker de verdade dentro da chamada do planner
(`tests/test_durability.py`), não levantando exceção.

### 4. Quando roda

`run_task(planner=None)`: sem planner o loop é idêntico ao de antes (os testes de loop não mudam).
O worker e o `demo.py` passam um `ProviderPlanner` sobre o provider da tarefa, e como passam
sempre um verifier, na prática o planner roda sempre que há verificação.

### 5. Onde o plano aparece

- **Corpo do PR** (`_publish_report`, briefing §23): seção "Plan", depois da tabela de evidência e
  antes do relato do agente, marcada como gerada pelo planner e não verificada. O que o control
  plane mediu vem primeiro.
- **API/UI:** `TaskOut.plan`, lido do log como o resumo do coder (sem coluna), exibido na aba Spec
  com o selo "Gerado" (Parte D do briefing: o que veio do modelo é marcado). Plano ausente ou
  malformado aparece como "Sem plano", sem selo.

### 6. FakeProvider

Quando `submit_plan` é oferecida e o roteiro não tem esse passo, o FakeProvider devolve um plano
padrão **sem avançar o cursor**: só os roteiros que querem testar o conteúdo do plano ganham um
passo, e os demais continuam com o primeiro turno do coder no passo 0. No modo `resume_aware`, o
índice por turno ignora os passos `submit_plan` e `submit_verdict`: eles não são turnos do
diálogo do coder, e contá-los entregava ao coder o plano como primeiro turno (um off-by-one que
só aparece quando um roteiro tem plano).

## Alternativas consideradas

**Plano como gate** (reprovar se o plano estiver malformado, ou se o diff ignorar `likely_files`).
Recusado: o plano é palpite de um modelo que não leu o repositório. Reprovar por divergir dele
puniria o coder por corrigir o palpite.

**Plano como tool do coder** (`submit_plan` no registry dele). Seria só um passo a mais do mesmo
agente, sem contexto separado, e o plano nasceria depois de o coder já ter lido o código.

**Gravar o plano em tabela própria.** Sem migração na leva: o log de eventos é a fonte, como o
resumo do coder, e `model_calls.purpose` não tem CHECK.

## Consequências

- Uma chamada de modelo a mais por tarefa (visível em `model_calls`, `purpose = 'planner'`, e no
  custo exibido). A tabela de custo dos evals de capacidade passa a incluí-la.
- O texto do plano é saída de modelo entrando no contexto de outro modelo. Ele não ganha
  autoridade com isso: toda chamada do coder continua passando pela policy.
- Um plano ruim custa tokens, nunca uma tarefa. A medição de "o plano ajuda?" (comparar taxa de
  sucesso com e sem planner nos evals de capacidade) fica como trabalho futuro, depois do
  `v0.1.0`.
