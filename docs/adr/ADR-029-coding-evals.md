# ADR-029: capability evals `coding_v1` com teste de aceitação oculto

**Status:** aceita · **Data:** 2026-10-02 · **PR:** `feat/coding-evals`

## Contexto

O ADR-027 prova o control plane com um modelo roteirizado. O que ele não mede é o modelo: dado
um issue de verdade, o agente entrega a mudança certa? A semana 6 pede um dataset de 10 tarefas
sobre `examples/target-repo`, com taxa de sucesso, categoria de falha, custo, latência e
**escaped defects** (a verificação do control plane aprova e o teste oculto reprova; briefing
§19). Dois cuidados moldam o desenho: o número só vale se a tarefa passou pelo caminho de
produção, e o critério de aceitação tem que estar fora do alcance do agente.

## Decisão

**Caminho de produção.** `evals/coding.py` enfileira o texto do issue, verbatim, como `spec` de
uma tarefa e roda um `Worker` de verdade (fila, sandbox, policy, gateway, verificação,
veredito) com um provider real. Reaproveita de `evals/runner.py` o bootstrap do banco, as chaves
efêmeras e o `Context`, sem editá-lo. O banco é próprio (`warden_evals_coding`, ou
`WARDEN_TEST_DB`), porque o bootstrap derruba e recria o banco.

**O workspace sobrevive à tarefa.** O worker joga fora o volume quando a tarefa fica terminal.
`Worker(keep_workspaces=True)` pula isso: o teste oculto precisa rodar sobre o que o agente
deixou. Quem liga a flag é dono do volume; o runner o descarta ao terminar o item (também em
caso de erro) e o janitor do worker fica de rede de segurança. `run_hidden_test` recusa rodar se
o volume sumiu: `Sandbox.create` criaria um volume novo a partir do repositório intocado, e um
teste rodado contra a baseline é uma resposta errada com cara de resposta certa.

**O teste oculto roda num sandbox novo, no volume da tarefa**, copiado pela API de archive do
daemon (nunca por comando) para `.warden_hidden/`, com um argv fixo e uma invocação do pytest
por item (o app guarda estado em globais; ver `evals/datasets/target_repo/README.md`):

```text
python -I -c "<programa fixo>" .warden_hidden/test_issue_NN.py
  programa: sys.path.append(<workspace>); import pytest;
            sys.exit(pytest.main(['-q', '-p', 'no:cacheprovider', '--noconftest',
                                  '-c', '/dev/null', sys.argv[1]]))
```

O agente controla o workspace (um teste que ele escreveu e rodou com `run_command pytest`
escreve em qualquer lugar do volume), então cada peça fecha uma porta:

- `-I` tira o diretório atual do `sys.path`. Sem isso, um `pytest.py` plantado na raiz seria
  importado no lugar do pytest de verdade e sairia com código 0, sem rodar nada.
- `sys.path.append` põe o workspace no **fim**: `import src.app` continua resolvendo, mas nenhum
  arquivo do workspace sombreia um pacote instalado (`fastapi`, `httpx`).
- `--noconftest` impede que um `conftest.py` plantado reescreva todo resultado para "passed".
- `-c /dev/null` é o que a especificação original não previa e os testes revelaram: sem um ini
  vazio, o pytest acha o `pyproject.toml` do workspace e obedece `pythonpath = ["."]`, que põe o
  workspace de volta no **começo** do `sys.path` e desfaz o `append` (o agente edita esse
  arquivo). A raiz do workspace dentro do container é `/sandbox/workspace`, não `/workspace`.
- O nome do teste vai como `sys.argv[1]`, nunca interpolado no programa.

Cada defesa tem um teste com **controle**: planta o arquivo hostil de verdade no volume, mostra
que o teste oculto continua falhando, e mostra que sem aquela defesa o arquivo plantado vence
(`test_coding_runner.py`). Sem o controle, um teste "passa" também quando o arquivo nunca
carregou.

**O que isso ainda não pega.** Um `sitecustomize.py`/`.pth` num diretório instalado está fora do
alcance (o volume é só o workspace). Código que o agente colocou em `src/` roda com os
privilégios do teste, que é o ponto do teste. E o teste julga comportamento, não estilo nem
mínimo de diff (para isso existe o reviewer).

**Resultado por item.** `status`, `verdict_passed`, `gating_green` (lint, types e tests
`passed`), `hidden_passed`, `success` (SUCCEEDED **e** teste oculto passou), `escaped_defect`
(veredito aprovou **e** gating verde **e** teste oculto reprovou), custo (soma de `model_calls`,
revisor incluído), tokens, latência (`finished_at - started_at`), iterações, chamadas de tool e
`forbidden_attempts`. As chamadas do modelo vêm dos eventos `tool.requested` cruzados com
`policy.decided`, não da tabela `tool_calls`: uma chamada que pausa a tarefa para aprovação não
tem linha lá até um humano responder, e o classificador precisa ver toda chamada que o modelo
fez.

**Categoria de falha** (só quando não houve `success`), regra determinística, a primeira que
casa vence, função pura em `evals/coding_checks.py` com teste em tabela:

1. `timeout`: `TIMED_OUT` pelo prazo (`max_seconds`).
2. `budget`: `BUDGET_EXCEEDED`.
3. `loop`: `TIMED_OUT` por `max_iterations`. O loop usa o mesmo status para as duas causas;
   só a razão (`reached max_iterations ...`) distingue, e um teste com um run real fixa essa
   string.
4. `hallucinated_api`: o modelo chamou uma tool que não existe (fora de `KNOWN_TOOLS`, que um
   teste compara com o registry real).
5. `policy_violation`: tentou algo que o item tenta (`forbidden`, ex.: ler `**/.env` no issue
   10), qualquer que tenha sido a decisão da policy.
6. `wrong_file`: a evidência de diff existe e não toca nenhum `expected_files` (diff vazio
   conta). Sem evidência de diff (a tarefa nem chegou à verificação) a regra não se aplica.
7. `tests_fail`: o resto (gating vermelho, veredito rejeitado, teste oculto reprovado).

Limites vêm primeiro: um run cortado por limite não diz nada confiável sobre o resto. Consequência
visível: o item 01 roteirizado para não mudar nada sai `wrong_file`, não `tests_fail`.

**Por que número com `FakeProvider` nunca é publicado.** O custo dele é zero e o "sucesso" é
roteirizado: publicar isso seria publicar uma não-medição com cara de medição. `--write-metrics`
recusa o provider `fake`, recusa `--only` (um subconjunto não é o dataset) e recusa uma execução
em que algum item terminou em `error` (erro do harness ou do provider, que não é resultado do
modelo). Uma tarefa que termina a rodada fora de um status terminal também é `error`: num
capability eval ninguém aprova nada, então uma chamada que a policy manda para aprovação deixa a
tarefa em `WAITING_APPROVAL` com o workspace pela metade, e pontuar isso seria uma falha que o
modelo não cometeu. O provider fake existe para provar o runner: o item 09 roteirizado conserta o bug de
verdade e tem que dar `success`; o 01 aprova sem mudar nada e tem que dar escaped defect.

**Custo.** Por tarefa `Budget(max_usd=1.00, max_seconds=900, max_iterations=30)`, sobrescrevível
por flag. `--max-usd-total` (padrão 5.00) para de **iniciar** itens quando o total corrente o
alcança; os restantes saem `pulado (budget cap)`. Um item em andamento pode estourar o teto total
por no máximo o próprio `max_usd`.

**GitHub desligado.** O runner força `GITHUB_REPO` e `GITHUB_TOKEN` vazios antes de carregar as
settings: `build_publish_registry` devolve `None` e a fase de publicação (ADR-028) não existe
nesta rodada, mesmo com diff verde. Difere de `runner.isolate_github`, que configura o GitHub de
propósito para o caso 3 do ADR-027. A URL da API aponta para uma porta de loopback onde nada
escuta, por garantia. O agente nunca tem `github.open_pr` (ADR-028): se o modelo chamar a tool
mesmo assim, `_decide` nega a chamada a uma tool fora do registry antes de ler qualquer regra, o
loop segue, e a rodada vira `hallucinated_api`.

## Alternativas consideradas

- **Rodar o teste oculto no volume com o argv "óbvio"** (`python -m pytest`). Importa o que o
  agente quiser: três das quatro defesas acima existem porque esse caminho foi testado e
  falhou. Rejeitada.
- **Extrair o workspace para o host e rodar lá.** Executaria código do agente fora do sandbox.
  Rejeitada.
- **Contar chamadas pela tabela `tool_calls`.** Perde a chamada que pausa para aprovação.
  Rejeitada em favor dos eventos.
- **Classificar falha com um modelo.** Não determinístico e custa token. Rejeitada.

## Consequências

- O comando real (`make evals-coding`, provider `anthropic`) **não foi executado** nesta
  máquina: não há `ANTHROPIC_API_KEY`. Tudo que o prova em escala (taxa de sucesso, custo,
  escaped defects de um modelo real) continua aberto; a seção de `docs/metrics.md` segue sem
  número até a primeira execução real.
- `make evals-behavioral` (e o job do CI que o roda) passa a executar também
  `evals/tests/test_coding_runner.py`, que precisa de Postgres e Docker mas não de chave.
- `core/worker.py` ganha o argumento `keep_workspaces`, desligado por padrão; nada além do
  runner de evals o liga.
