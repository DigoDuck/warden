# ADR-026: verifier determinístico, baseline no host e VERIFYING como status com lease

**Status:** aceita · **Data:** 2026-09-29 · **PR:** `feat/verify-runner`

## Contexto

Até este PR, o sucesso de uma tarefa era o relato do próprio coder. O agente chamava `finish`, e
`core/loop.py` gravava SUCCEEDED com o `summary` que o modelo escreveu. O briefing (§10) define
`verify` como o módulo que coleta evidência "sem confiar no relato do agente". A máquina de
estados (§16) prevê `RUNNING → VERIFYING → SUCCEEDED|FAILED`. `VERIFYING` existia no CHECK de
`tasks.status` desde a migração inicial, mas nada o usava, e a tabela `evidence` (§13) não
existia.

Este PR entrega só o runner: checks determinísticos gravados como evidência. O reviewer, o
verdict e a definição de "sucesso" (ADR-010) ficam para o PR seguinte.

## Decisão

### Quando o agente termina, o control plane verifica

Os dois caminhos que davam SUCCEEDED passam a ir para `_verify_and_finish`: `finish` explícito e
`end_turn` sem `finish`. Nesse ponto a tarefa vira VERIFYING e o control plane roda, por conta
própria, quatro checks fixos no sandbox da tarefa. Cada um vira uma linha em `evidence`:

| kind | o que roda |
|---|---|
| `diff` | baseline do host × workspace exportado do container (`difflib`, stdlib) |
| `lint` | `ruff check --no-cache .` e `ruff format --check --no-cache .` |
| `types` | `mypy --cache-dir=/dev/null --explicit-package-bases .` |
| `tests` | `python -m pytest -q -p no:cacheprovider` (o mesmo argv da tool `run_tests`) |

Nenhum argumento vem do modelo nem do `summary`. A lista é fixa pelo mesmo motivo de
`RUN_TESTS_TIMEOUT` ser fixo: evidência que o avaliado escolhe como produzir não é evidência.

### O baseline do diff fica no host

O "antes" é o diretório que semeou o container, filtrado pelos mesmos `is_ignored` e
`never_readable(policy)` que `_workspace_tar` usa. Sem o mesmo filtro, todo `.env` excluído pela
ADR-018 apareceria como "removido". O "depois" sai do container pelo endpoint de archive do
daemon (`Sandbox.export_workspace`), sem executar nada dentro dele.

A alternativa descartada foi um baseline git dentro do volume, commitado na criação do sandbox. É
mais simples e dá `git diff` de graça, mas mora onde o código do agente alcança: `pytest` executa
o que o agente escreveu, e um `conftest.py` poderia reescrever o baseline e esconder uma mudança.
O coder não consegue falsificar o que não consegue tocar.

O tar exportado é entrada não confiável: o agente escolheu cada nome e cada link. Por isso ele é
lido em memória e nunca extraído em disco. Um nome que não normaliza sob `workspace/` é
descartado, e um symlink entra no diff como `<link to …>`, sem ser seguido. O export tem teto de
50 MB; acima disso o `diff` é gravado com `status: "error"`.

### A ordem é parte da garantia: diff, lint, types, tests

Só `tests` executa código do agente. O diff é lido antes de qualquer execução, e ruff e mypy só
fazem parse dos arquivos. Assim, nada que um teste faça ao workspace enquanto roda influencia os
três primeiros checks. `test_the_diff_is_taken_before_any_agent_code_runs` prova isso com um
`conftest.py` que planta um arquivo.

### Evidência não é verdict

Com evidência vermelha (testes falhando, mypy com erro, timeout), a tarefa termina **SUCCEEDED**
do mesmo jeito. Aqui SUCCEEDED significa "o agente terminou e a evidência está registrada". Se a
evidência é boa o bastante, quem decide é o verdict (ADR-010). Decidir aqui também definiria
"sucesso" em dois lugares, e a transição `VERIFYING → FAILED` fica para quando o verdict existir.

### VERIFYING tem lease, como RUNNING

A verificação roda dentro do mesmo worker e pode morrer do mesmo jeito. Por isso, `queue.claim`
(reclaim de lease expirado), `heartbeat` e `release` tratam `('RUNNING', 'VERIFYING')` igual. Em
`cancel.request_cancel`, VERIFYING recebe o marcador, como RUNNING: há um worker para cooperar.

### Durável e idempotente, no mesmo formato do loop

- `verify.started` é commitado primeiro, com `summary` e `iterations`. Um worker que morre daí
  em diante é retomado direto na verificação (`ResumeState.verification`), sem nova chamada de
  modelo e sem tocar na conversa.
- Um checkpoint por check: a linha de `evidence` e o evento `verify.recorded` commitam juntos. O
  resume pula os kinds já registrados.
- `UNIQUE(task_id, kind)` torna "nunca registrado duas vezes" uma promessa do banco, não só do
  loop.

`test_verification_survives_the_worker_process_being_killed` mata o processo do worker durante o
`pytest` e confere, com outra conexão, que o segundo worker grava só o `tests` e não chama o
modelo.

### Cancel sim, deadline não

A verificação checa o marcador de cancel antes e depois de cada check. Se o `_watch_cancel`
matou o container no meio de um check, o resultado ("error") não é gravado: seria evidência do
kill, não do trabalho. O `max_seconds` **não** vale durante a verificação. Ele é o orçamento do
agente, e um agente que terminou aos 299 s de 300 não deve virar TIMED_OUT porque os checks do
control plane levaram tempo. Cada check tem seu próprio `kill_after` fixo (120 s), então nenhum
pendura a tarefa.

Isso diverge do plano do PR, que previa `_check_stoppable` (cancel e deadline) antes de cada
check. A troca foi feita durante a implementação, pelo motivo acima.

## Consequências

- `run_task` ganha `verifier: EvidenceCollector | None`. O worker e o demo sempre passam um. Os
  testes de loop com tools falsas passam `None` e não mudam. O `Protocol` deixa
  `tests/test_verify_loop.py` testar a contabilidade (ordem, cancel, resume) sem Docker.
- `GET /tasks/{id}/evidence` expõe as linhas, e a UI ganha o status "Verificando". As abas Diff
  e Evidence vêm depois.
- **Limitação conhecida:** um `conftest.py` do agente pode fazer o `pytest` sair com 0 sem rodar
  nada. O runner registra o que o comando disse, e o diff mostra o `conftest.py`. Pegar isso é
  trabalho do reviewer (ADR-010) e dos testes ocultos do `coding_v1` (semana 6), que o agente
  não vê.
- **Ordem com `open_pr`:** hoje o agente chama `github.open_pr` dentro do loop, antes de
  `finish`, então o PR é aberto antes da verificação. O briefing (§23) põe o PR depois do
  verdict. Isso será corrigido no PR do verdict, não aqui.
- Semgrep (`sast`) não está na imagem do sandbox. Ele entra na semana 11, com a migração que
  adiciona o kind ao CHECK.

## Adendo (ADR-010)

O verdict e a definição de sucesso chegaram na ADR-010: a partir dela, a tarefa só termina
SUCCEEDED se o verdict do reviewer independente for bem formado e aprovar **e** os checks
`lint`, `types` e `tests` tiverem passado; do contrário termina FAILED. O trecho "Evidência não é
verdict" acima descreve o estado deste PR e foi superado.
A correção da ordem com `open_pr` ("será corrigido no PR do verdict") ficou para um PR
seguinte; ver "Consequências" da ADR-010.

## Adendo (ADR-028)

A "ordem com `open_pr`" descrita em "Consequências" foi corrigida pela ADR-028: o PR só é
proposto depois do verdict, pelo control plane. A evidência `diff` ganhou `sha256` por arquivo
para que a publicação prove que o workspace não mudou desde a verificação.
