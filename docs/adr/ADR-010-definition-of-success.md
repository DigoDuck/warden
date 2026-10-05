# ADR-010: verificação independente e definição de "sucesso"

**Status:** aceita · **Data:** 2026-10-05 · **PR:** `feat/reviewer-verdict`

## Contexto

A ADR-026 fez o control plane coletar evidência própria depois do `finish` (diff, lint, types,
tests), mas deixou a tarefa terminar SUCCEEDED com qualquer evidência. Faltava responder: **o que
conta como sucesso?** O briefing pede um reviewer independente que lê spec, diff e evidência e
emite um `verdict` (§22 passo 8, §23), e a máquina de estados prevê `VERIFYING → SUCCEEDED |
FAILED` (§16).

## Decisão

### Sucesso = gate determinístico E reviewer independente

`verdict.passed = gate.passed AND review.passed`. Só com o verdict aprovado a tarefa termina
SUCCEEDED; caso contrário, FAILED, com o `reason` dizendo qual metade decidiu.

**Gate:** `tests` e `lint` precisam estar `passed`. É o espelho do CI do target-repo (ruff +
pytest), e foi decisão do Diogo.
- `types` (mypy) é registrado e mostrado ao reviewer, mas não bloqueia. Um repo sem anotações de
  tipo reprovaria toda tarefa, para sempre.
- `failed`, `timeout`, `error` e ausência de evidência bloqueiam. Ausência de evidência não é
  evidência de aprovação.
- Pytest com exit 5 ("no tests collected") vira o status `no_tests`, que **não** bloqueia. Repo
  sem testes não é repo com testes vermelhos. Se a spec pedia testes, quem cobra é o reviewer.

**Reviewer:** julga o que os checks não alcançam: o diff faz o que a spec pediu?
- Conversa nova, que nunca viu a do coder, com prompt próprio (`review-v1`, gravado em
  `model_calls.prompt_version`) e uma única tool, `submit_verdict`.
- Os argumentos são validados contra `ReviewOut` com `extra="forbid"` e `passed: StrictBool`. O
  teste do PR mostrou que, em modo lax, o Pydantic aceita `"yes"` como `True`; numa fronteira de
  saída de modelo, só um `true` JSON de verdade aprova.

### O reviewer pode vetar, nunca aprovar por cima do gate

A combinação é feita em código (`verify/reviewer.py::decide`), depois da resposta do modelo, e não
pelo modelo. Um reviewer que diz sim a testes vermelhos, por erro ou porque o diff o convenceu,
produz um verdict reprovado. Essa é a regra que torna o verdict defensável: a metade que lê prosa
pode ser enganada, a outra não lê prosa.

### Falha fechada

Resposta sem `submit_verdict`, com duas chamadas, com tool desconhecida, com argumentos que não
validam ou com refusal vira `status="error"` e `passed=false`. A constraint
`ck_verdicts_error_never_passes` repete a regra no banco: nenhum caminho de código consegue gravar
um erro que pareça aprovação.

### Diff e resumo do coder são dados não confiáveis

O prompt cerca o diff, o resumo do agente e o output dos checks em tags (`<untrusted_diff>` etc.).
Qualquer ocorrência da tag de fechamento dentro do conteúdo é neutralizada antes, para o texto não
"sair" da cerca. O system prompt manda ignorar instruções ali dentro. Isso reduz o risco, mas não o
elimina: uma injeção ainda pode enviesar o reviewer. O que limita o estrago é a regra anterior,
porque o pior caso é enviesar a metade que pode vetar, não a que pode aprovar sozinha.

### Sem reviewer configurado, o verdict é só o gate, e diz isso

`reviewer=None` produz um verdict com `reviewer="none"`. É o caso dos testes de worker e de um
script fake sem seção `review:`. O verdict existe, e a linha mostra que nenhum modelo revisou.
Nunca há reviewer fake que aprova por default: `FakeProvider.from_yaml(section="review")` sem a
seção é erro.

### Durabilidade, custo e cancel

- Gate, revisão, linha em `verdicts`, linha em `model_calls` (`purpose="review"`) e o evento
  `verify.verdict` fecham num checkpoint, depois dos checks. Nenhuma transação fica aberta durante
  a chamada ao modelo.
- `UNIQUE(task_id, verifier)` garante um verdict por tarefa. No resume com `verify.verdict` no log,
  a tarefa termina com o verdict gravado e não revisa de novo.
- Janela aceita: um crash entre a resposta do reviewer e o commit faz pagar a revisão outra vez. É
  a mesma janela que o loop já aceita para o `model.called`.
- O custo da revisão entra no `spent` da tarefa (via o evento, também no replay), mas não dispara
  BUDGET_EXCEEDED. O orçamento é do agente, mesmo racional do `max_seconds` na ADR-026.
- Cancel é checado antes e depois da revisão. Se chegar durante a revisão, a tarefa fica CANCELLED
  sem verdict.

## Consequências

- Uma tarefa que termina com testes vermelhos agora é **FAILED**. Antes deste PR, era SUCCEEDED.
- `GET /tasks/{id}` traz `verdict`, com o gate e a opinião do reviewer separados. Os selos
  "generated" (resumo do coder) e "verified" (verdict) entram na UI no PR das abas.
- `verifier` aceita só `independent`. `coder_self` entra com o experimento da semana 8 (self-review
  × independente, escaped defects), junto da migração que alarga o CHECK.
- **Limitações conhecidas:**
  - Um `conftest.py` que força o pytest a sair com 0 passa no gate. Só o reviewer, que vê o
    `conftest.py` no diff, e os testes ocultos do `coding_v1` (semana 6) pegam isso.
  - O `open_pr` ainda roda dentro do loop, antes do verdict. A inversão (o control plane abre o PR
    só com verdict aprovado, com o relatório no corpo) é o próximo PR, por decisão do Diogo.
