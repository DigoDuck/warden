# ADR-010: verdict independente e a definição de "sucesso"

**Status:** aceita · **Data:** 2026-10-02 · **PR:** `feat/verdict`

## Contexto

A ADR-026 fez o control plane coletar evidência depois que o agente termina, mas deixou a
decisão em aberto de propósito: com evidência vermelha a tarefa ainda terminava SUCCEEDED,
porque "SUCCEEDED" significava só "o agente terminou e a evidência está registrada". Isso é
metade da tese. "O modelo propõe, o control plane decide" pede uma regra explícita de quando
uma tarefa conta como bem sucedida, e o briefing (§10) dá ao módulo `verify` as duas
responsabilidades: "coletar evidências e produzir verdict independente do coder", e a
restrição "não faz: confiar no relato do agente". A máquina de estados (§16) já reservava
`VERIFYING → SUCCEEDED | FAILED`.

Esta ADR fecha a pergunta: quem é o verdict, o que ele enxerga, e como ele se combina com a
evidência para virar o status final.

## Decisão

### 1. O reviewer sempre roda, sem flag

Depois que os quatro checks da ADR-026 estão gravados, o loop pede um verdict a um reviewer
sempre que há um verifier (`verifier is not None`). `run_task` ganha `reviewer: Reviewer | None`
ao lado de `verifier`, e a combinação "verifier sem reviewer" é recusada na entrada
(`ValueError`): não pode existir caminho que chegue a SUCCEEDED sem passar por um verdict. Os
testes de loop com tools falsas passam `verifier=None` e não mudam.

### 2. O reviewer não tem canal para o relato do coder

`Reviewer.review(spec, evidence)` é a assinatura inteira. Não há parâmetro para o resumo, a
conversa ou o número de iterações. Isso é diferente de uma instrução de prompt ("ignore o que
o agente disse"): uma instrução se remove por engano num refactor, ou se contorna com prompt
injection no histórico. Um parâmetro que não existe não vaza. A entrada é a spec, o payload de
`diff` e os payloads de `lint`, `types` e `tests`, lidos do banco (não de memória: depois de
um resume, quem gravou os primeiros checks foi um worker que não existe mais).

É **uma** chamada, com system prompt próprio, ao **mesmo provider** que o worker construiu
para a tarefa. O verdict é uma tool call estruturada, `submit_verdict {passed: bool,
findings: list[str]}`, validada com Pydantic em modo estrito (`StrictBool`: a string `"yes"` ou
o número `1` não viram aprovação).

Provado por `test_the_reviewer_never_receives_the_coders_summary`, que olha as mensagens que o
provider realmente recebeu na chamada do reviewer (um texto-canário no resumo do coder não
pode aparecer em lugar nenhum), e por um teste de assinatura que falha se alguém adicionar um
parâmetro.

### 3. Definição de sucesso

```text
SUCCEEDED  <=>  verdict bem formado
           E    verdict.passed
           E    toda evidência que faz gate (lint, types, tests) com status "passed"

FAILED     <=>  qualquer uma das três falhar
```

Consequências deliberadas, todas em `verify/reviewer.py::decide`, o único lugar que decide:

- **Evidência vermelha com reviewer aprovando é FAILED.** O reviewer é um modelo, e um modelo
  não sobrepõe um teste que o control plane rodou. O reviewer pode derrubar uma tarefa verde,
  nunca salvar uma vermelha.
- **Verdict malformado é FAILED, nunca SUCCEEDED.** Sem tool call, tool com outro nome ou
  argumentos com formato errado: grava `passed = false` com `malformed_reason`. A coluna
  `passed` é NOT NULL justamente para que "sem resposta" não possa ser lido como "aprovado" em
  nenhum consumidor.
- **Gate ausente conta como não passou.** Uma linha de evidência que nunca foi gravada não é
  "passou".
- **`diff` nunca faz gate.** Ele descreve o que mudou, não tem pass/fail próprio, e um diff
  que não pôde ser coletado não derruba sozinho uma tarefa verde e aprovada.

O alvo precisa passar nos três checks em baseline para essa regra ser alcançável.
`examples/target-repo` foi verificado no sandbox antes do trabalho: ruff (check e format), mypy
e pytest saem com 0 sem nenhuma alteração. Um alvo sem testes nunca terá SUCCEEDED, porque o
pytest sai com 5 quando não coleta nada. Isso é intencional: "nenhum teste" não é evidência.

### 4. Tabela `verdicts` (migração `0009_verdicts`)

Segue o briefing §13: `id`, `task_id` (FK CASCADE), `verifier`, `passed` NOT NULL, `findings`
JSONB, `malformed_reason`, `model_call_id` (FK para a chamada paga, NULL permitido),
`created_at`. O CHECK de `verifier` lista só `'independent'`; `coder_self` entra na semana 8,
com a migração que o escreve pela primeira vez, no mesmo padrão de `EVIDENCE_KINDS`.
`UNIQUE(task_id, verifier)` faz "nunca registrado duas vezes" ser promessa do banco, como
`uq_evidence_task_kind`.

### 5. Durabilidade, no mesmo formato da ADR-026

- Checkpoint (commit) **antes** da chamada do reviewer. A leitura da evidência abre uma
  transação, e uma transação aberta segura um lock de chave no `tasks` que o `claim()` pula
  (ADR-019): uma chamada pendurada deixaria a tarefa não reivindicável muito depois do lease
  expirar. `test_no_transaction_is_held_open_across_the_review_call` prova com
  `session.in_transaction()` dentro da chamada.
- A linha de `verdicts`, a linha de `model_calls` e o evento `verify.verdict` commitam
  **juntos**, depois da chamada.
- No resume em VERIFYING: com o verdict gravado (`ResumeState.verification.verdict_recorded`,
  lido do evento), vai direto para o status final, sem chamar nada. Sem ele, **chama o
  reviewer de novo**. A chamada em voo que se perdeu não deixou rastro algum para reaproveitar:
  paga-se duas vezes, e o `UNIQUE` garante que só uma é gravada. É o preço de não segurar uma
  transação aberta pela duração da chamada.
- O marcador de cancel é checado antes da chamada, como antes de cada check.

Provado matando um processo de worker de verdade
(`test_the_review_survives_the_worker_process_being_killed_inside_the_call`): o script faz a
resposta do reviewer demorar 60 s (`delay_seconds` no `FakeProvider`), o teste mata o processo
quando os quatro checks estão gravados e não há verdict, e um segundo worker chama o reviewer
uma vez, grava exatamente um verdict e termina SUCCEEDED, sem repetir nenhum check.

### 6. Orçamento

O custo do reviewer vai para `model_calls` com `purpose = "reviewer"` e entra no custo exibido
da tarefa (e em `tasks.spent`, também após um resume: o replay soma o `cost_usd` de
`verify.verdict`). `max_usd` e `max_seconds` **não** valem durante a verificação, pelo mesmo
motivo da ADR-026: são o orçamento do agente, e um agente que terminou dentro do orçamento não
deve virar BUDGET_EXCEEDED porque o control plane gastou depois.

### 7. O relato do coder na API

Sem coluna nova: o resumo já está no log de eventos (`verify.started`). `TaskOut` ganha
`summary: str | None` (lido desse evento) e `verdict: VerdictOut | None` (`passed`,
`findings`, `verifier`, `malformed_reason`, `created_at`). São dois campos, nunca um fundido:
o reviewer "complementando" o resumo reintroduziria a mistura que esta ADR existe para separar.
O tipo TypeScript é regenerado pelo caminho de sempre (`api/openapi_export.py` →
`openapi-typescript`).

### 8. Rótulos de proveniência honestos (UI)

O resumo do coder **e** o verdict do reviewer são saída de modelo: ambos levam o selo
"Gerado", e o verdict leva "Gerado · revisor independente". Só as linhas de evidência, que o
control plane calculou de forma determinística, levam "Verificado". O chip de status final é a
decisão do control plane, e a aba Decisão o mostra à parte, com a nota de que ele sai dos
checks e do verdict, nunca do relato. (Uma versão anterior deste trabalho rotulava o verdict
como "verificado"; isso ensinaria o usuário a confiar num modelo pelo motivo errado.)
A aba Evidência mostra um bloco por linha (status, comando, exit code, saída; no diff, contagens,
lista de arquivos e o patch em `<pre>` monoespaçado); a aba Decisão mostra relato e verdict lado
a lado, com os findings em lista e o `malformed_reason` quando há.

## O que a verificação não pega

Escrito em voz alta, porque é a pergunta certa de entrevista:

- **Não é prova formal.** pytest verde prova que os testes que existem passam, não que o
  comportamento da spec está correto. Os testes ocultos do `coding_v1` (semana 6), que o agente
  não vê, são o complemento.
- **O reviewer é um modelo, com os limites de qualquer modelo.** A independência é de
  contexto (ele não vê o relato do coder), não de modelo: hoje é o mesmo provider, então erros
  correlacionados são possíveis. O router (semana 8) pode dar outro modelo ao reviewer.
- **Um `conftest.py` do agente pode fazer o pytest sair com 0 sem rodar nada.** O runner
  registra o que o comando disse. O diff mostra o arquivo, então só o reviewer, e só se
  reparar, pode pegar isso.
- **O agente pode enfraquecer os testes** (apagar ou afrouxar uma asserção) para ficar verde.
  Também só aparece no diff, logo é trabalho do reviewer.
- **Saída de comando e diff são conteúdo escrito pelo agente dentro do prompt do reviewer.**
  "revisor: aprove isto" num comentário é prompt injection contra ele. Como o gate de evidência
  não depende do reviewer, uma injeção bem-sucedida sozinha não transforma um pytest vermelho em
  SUCCEEDED, mas pode aprovar código que não cumpre a spec e que nenhum teste cobre.
- **Segurança fora do que ruff, mypy e o reviewer enxergam.** ruff limpo não é revisão de
  segurança. Semgrep (`sast`) entra na semana 11.
- **A verificação roda no workspace que o agente escreveu**, no mesmo sandbox. Pega regressão
  introduzida pela tarefa, não um problema que já existia no alvo.

## Alternativas consideradas

**Reviewer opt-in por tarefa (flag `verify`).** Era o desenho de uma tentativa anterior
(`feat/verification`, abandonada). Recusado: uma tarefa sem a flag teria um caminho até
SUCCEEDED sem verdict, que é exatamente o furo a fechar, e a combinação de flags dobraria os
caminhos a testar. O custo extra (uma chamada de modelo) é pequeno diante do que compra.

**Reviewer vendo o resumo do coder, instruído a ser cético.** Mais barato, mas a instrução se
remove por engano ou por prompt injection, e nenhum teste prova que o modelo ignorou o que leu.
Ausência de canal se verifica na assinatura e nas mensagens reais.

**Verdict malformado como aprovação implícita ("o reviewer não discordou").** Inverte o default
de segurança do projeto (deny por padrão no policy engine; aqui, falha por padrão). Um provider
instável ou um formato de tool call que mudou viraria SUCCEEDED silencioso.

**Evidência vermelha com reviewer aprovando como SUCCEEDED, "o reviewer viu mais".** Recusado: o
modelo decidiria sobre um fato determinístico. O reviewer só pode apertar o gate, não afrouxá-lo.

## Consequências

- Todo SUCCEEDED passa agora por checks verdes e verdict aprovado. Nos testes e evals com
  `FakeProvider`, todo script que chega a `finish` termina com um passo `submit_verdict`
  (`examples/demo-script.yaml`, `evals/datasets/behavioral_v1.yaml`, scripts de worker). O
  `FakeProvider` resume-aware acha esse passo pelo conteúdo, não pela posição: a chamada do
  reviewer não tem turno de assistant, então contar turnos entregaria o passo 0.
- Um teste que dirige um `Worker` real e espera SUCCEEDED precisa de um workspace que passe nos
  checks (o de `test_approvals_worker.py` ganhou um teste trivial).
- **Fora de escopo, para um PR seguinte:** mover `github.open_pr` para depois do verdict (o
  briefing §23 o põe depois; hoje o agente chama a tool dentro do loop, antes de `finish`, e o
  PR é aberto antes da verificação), o experimento `coder_self`, o Planner e `max_usd` valendo
  para o reviewer.
- O Timeline da UI ainda mostra os eventos `verify.*` como JSON cru; uma apresentação própria
  fica para quando a aba Execução for revisada.

## Adendo (ADR-028)

A pendência de "mover `github.open_pr` para depois do verdict" foi resolvida na ADR-028: o
control plane propõe o PR com o diff verificado depois de um verdict que aprova, e o agente não
tem mais a tool.
