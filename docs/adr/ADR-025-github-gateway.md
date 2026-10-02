# ADR-025: identidade por chamada, gateway de tools e `github.open_pr`

**Status:** aceita · **Data:** 2026-09-27 · **PR:** `feat/github-gateway`

## Contexto

Até este PR, `identity/jwt.py` e `identity/broker.py` existiam com teste próprio, mas
nenhum código de produção os chamava: os dois docstrings diziam isso explicitamente ("nada em
`core/loop.py` chama isto ainda"). A policy engine decide ALLOW/DENY/REQUIRE_APPROVAL, mas a
decisão nunca virava um credencial que uma tool pudesse gastar; `registry.execute()` rodava a
tool direto, sem nada entre a decisão e a execução além do próprio `if decision.effect is
ALLOW`. Enquanto toda tool ficava dentro do sandbox sem rede (ADR-004), essa lacuna não tinha
como doer. `github.open_pr` muda isso: é a primeira ação do agente que sai do control plane, e
sai como uma chamada HTTP autenticada com um segredo real.

Duas lacunas fechavam junto (ver o addendum na ADR-006): `broker.redact()` existia sem
chamador, e a distinção 401/403 que a ADR-006 previu para "a wave 4" não tinha onde morar.

## Decisão

### Token por chamada, não por tarefa

Cada chamada de tool cuja decisão final é ALLOW (incluindo uma aprovada por humano) ganha um
`identity.issue_agent_token` só para ela: `task_id` da tarefa rodando, `scopes` da decisão da
policy (ou `["tool:<nome>"]` quando a regra não nomeia nenhum, o caso comum hoje, já que a
maioria das regras em `policies/default.yaml` não precisa de escopo mais fino que "esta tool
pode rodar"), TTL de 120s, bem abaixo do teto de 900s que `identity.jwt.AGENT_TTL_CAP_SECONDS`
já impunha. O token nasce e morre dentro da mesma chamada: é emitido, verificado e gasto em
sequência, no mesmo `await`, então uma janela de vida de minutos não compra nada, só amplia a
superfície se ele vazasse. `run_task`/`Worker` passam a exigir a `KeyPair` de assinatura como
parâmetro obrigatório (nunca `keys=None`): sem ela não existe forma de uma tarefa rodar,
porque não existe forma de emitir o token que toda chamada agora precisa.

Por que por chamada e não um único token por tarefa (o que a ADR-005 já permitia, já que
`issue_agent_token` sempre existiu por task): um token de vida longa por tarefa é exatamente a
"credencial ampla" que o briefing lista como ameaça (§21). Emitir um por chamada, com o escopo
exato daquela decisão, é o que faz "token expirado ou sem scope falha com 401/403" (checklist
da semana 3) significar alguma coisa: não há um token guarda-chuva que uma tool errada também
conseguiria usar.

Todo token que uma tarefa emitiu é revogado (`identity.revoke_all_for_task`) no `_finish`,
qualquer que seja o status final. Nenhum sobrevive ao fim da tarefa que o emitiu.

### `tools/gateway.py`: o único caminho para `registry.execute`

`core/loop.py` não chama mais `registry.execute` diretamente; chama `gateway.execute`, que:
1. `identity.verify()` no token — leitura fresca de `issued_tokens`, nunca confia num `Claims`
   que o próprio loop já tem em memória (mesma razão da ADR-005: um `Claims` construído à mão
   passaria por qualquer verificação que não voltasse ao banco).
2. Exige `claims.task_id == task_id` da tarefa rodando — `verify()` prova que o token é
   válido, não que foi emitido *para esta execução*; um token de outra tarefa é tão errado
   quanto um forjado.
3. Se a tool declarou `required_scope` no registro (`registry.register(..., required_scope=
   "github:pr:open")`), exige que o token carregue esse escopo.
4. Fecha a transação que a leitura de `verify()` abriu, com o commit cercado do loop
   (`checkpoint`), e só então chama `registry.execute`, passando um
   `ToolContext(claims, session, checkpoint)` para as tools que pediram identidade
   (`needs_identity=True` no registro — hoje só `github.open_pr`; toda tool existente mantém a
   assinatura de um argumento só).

**Nenhuma transação aberta durante a tool (correção da revisão).** A primeira versão deixava
aberta a transação da leitura de `verify()` durante toda a execução da tool, e
`github.open_pr` gravava o `credential.granted` do broker e seguia para o sandbox e para o
GitHub sem commitar. `audit.append` segura o advisory lock da cadeia de audit até o fim da
transação (ADR-007), então cada chamada ao GitHub travava a escrita de audit de todas as outras
tarefas pelo tempo que o GitHub levasse para responder: exatamente o defeito que a ADR-019
proíbe. Agora o gateway commita depois de verificar, e `open_pr` chama `context.checkpoint()`
logo depois de receber a credencial, antes de qualquer leitura no sandbox ou request HTTP. O
checkpoint é o cercado (`_checkpoint` com `holder`): um worker que perdeu o lease para ali,
antes de a tool rodar. Os testes observam o estado real de uma segunda conexão, de dentro do
GitHub falso: o lock livre e o grant já commitado.

Qualquer recusa (`InvalidToken` → 401/`tool.auth_failed`, escopo faltando → 403/
`tool.forbidden`) grava uma linha de audit antes de devolver o erro, e a tool nunca roda. As
duas classes de recusa que a ADR-006 previu para "o gateway" são estas; a exceção de que
`get_credential` faz é uma camada abaixo (sobre o segredo do GitHub, não sobre o token do
agente) e `tools/github.py` a relança como `ToolError` comum, nunca como um crash da tarefa.

Mantido fora de `core/loop.py`: assim os testes do loop continuam sem precisar forjar um JWT
para o caminho feliz (o loop emite e gasta o próprio token), e `tools/gateway.py` ganha um
teste unitário direto para os quatro jeitos de um token não servir (expirado, revogado, de
outra tarefa, sem escopo) — cada um causado de verdade contra uma linha real de
`issued_tokens`, nunca simulado.

### Git Data API sobre HTTP, nunca `git`

`github.open_pr` fala com o GitHub só pela API REST Git Data (`git/blobs`, `git/trees`,
`git/commits`, `git/refs`) e pela API de Pulls, com `httpx.AsyncClient` rodando no processo do
control plane. **Nunca** um `git` CLI, nunca um checkout do repo alvo em disco, nunca o token
como variável de ambiente ou argumento de um subprocesso, e nunca dentro do sandbox (que não
tem rede — ADR-004 — então a ação teria que passar por uma tool do control plane de qualquer
jeito).

Por que não `git push`: um `git push` real precisa do token em algum lugar que um processo
consiga ler para autenticar — `.git-credentials`, uma variável de ambiente, a URL remota
(`https://token@github.com/...`, que aparece inteira em `ps`, num log de subprocess, ou num
arquivo `.git/config`). Cada um desses lugares é uma superfície que `redact()` teria que
alcançar depois, e nenhuma delas é um único choke point. A API HTTP tem exatamente um lugar
onde o segredo existe em texto claro: o header `Authorization` desta chamada, montado uma vez
(`credential.reveal()`) e nunca copiado para uma string que este módulo loga ou levanta.

Sequência: a branch `warden/<task_id[:8]>-<slug>`, se já existe, senão a branch base → o commit
dela → a tree dele; um blob por arquivo (lido do sandbox da tarefa via `sandboxed.read_file`,
sem código novo de container); uma tree nova em cima dessa; um commit cujo pai é a ponta da
branch; a branch criada, ou avançada em fast-forward (`force: false`); por fim a PR, ou a PR
aberta já existente para essa branch.

### Idempotência: a branch é a chave, não uma tabela nova

Mesma tarefa, mesmo `branch_slug` → mesmo nome de branch (`_branch_name` é determinístico:
`task_id` truncado, sem aleatoriedade). Antes de abrir uma PR, `open_pr` pergunta ao GitHub se
já existe uma PR aberta para essa `head`; se sim, devolve ela em vez de abrir uma segunda. Não
existe uma segunda estrutura de idempotência (uma tabela, uma chave) porque o nome da branch já
é a chave, e o GitHub já é a fonte de verdade de "existe uma PR para esta branch".

**Fast-forward, nunca force (correção da revisão).** A primeira versão refazia o commit em cima
da base a cada chamada e movia a branch com `force: true`. Uma segunda publicação da mesma
tarefa (o agente corrigiu algo depois de abrir a PR) apagava da PR, sem aviso, os arquivos da
primeira. Agora o commit novo tem como pai a ponta da própria branch e a atualização é
fast-forward; a API real recusa qualquer outra coisa com 422. E como trees são endereçadas por
conteúdo, republicar os mesmos arquivos devolve a mesma tree do pai: nesse caso nenhum commit é
criado e a branch fica onde está. É o que torna idempotente o replay da janela at-least-once da
ADR-019 (o GitHub respondeu, o processo morreu antes do `tool.executed`).

### Escopo dos paths, igual a `apply_patch`

`args.paths` passa por um `path_inspector` (`open_pr_paths`, devolve a lista como veio) e cada
path é julgado pela policy como se fosse um path de `apply_patch` (`core/loop.py::_decide`
+ `combine`, ADR-017). Um path que `never-read-secrets` nega derruba a chamada inteira antes de
qualquer request HTTP: `combine()` já toma o efeito mais restritivo entre todos os paths de uma
chamada, e isso valia para `apply_patch` sem precisar de código novo para `github.open_pr`.

### `combine()` também precisou de um ajuste

`open-pr-needs-human` (REQUIRE_APPROVAL) casa com toda chamada de `github.open_pr`, então o
`scopes` que `combine()` devolve para essa decisão importa: é o que o token emitido depois da
aprovação vai carregar. `combine()` só preservava `scopes` quando o efeito combinado era ALLOW,
zerando também para REQUIRE_APPROVAL — certo para DENY (não vazar escopo de uma chamada
recusada), errado aqui, porque apagava o `github:pr:open` da regra antes mesmo de o loop saber
que precisava dele. Corrigido para zerar só em DENY; ALLOW e REQUIRE_APPROVAL preservam.

### `redact()` como choke point único do log de eventos

`core/events.py::append_event` e `record_tool_call` agora passam todo payload/`result_summary`/
`error` por `broker.redact()` (recursivo: string solta, lista, dict), antes de qualquer commit.
Isto é o "falta a wave 4 chamar de fato" que a ADR-006 deixou pendente, e é o que fecha o item
"`grep` em logs e em `task_events` não encontra o token do GitHub" do checklist da semana 3 —
verificado por um teste que roda a tarefa inteira e faz exatamente esse grep contra o banco.

### `httpx` de dev para runtime

Só o teste FastAPI usava `httpx` antes; `github.open_pr` agora o usa em produção, então ele sai
do grupo `dev` em `pyproject.toml` para as dependências normais. Nenhuma outra biblioteca HTTP
teria comprado algo: `httpx` já era a escolha do projeto para testar a API, tem cliente async
nativo (`AsyncClient`) e suporte de primeira classe a transporte substituível
(`httpx.MockTransport`), que é o que `tests/test_github_tool.py` usa para nunca tocar a rede.

## Alternativas consideradas

**Um token por tarefa em vez de por chamada.** Mais simples de implementar (uma emissão em vez
de uma por chamada ALLOW), mas é a credencial ampla que a tese do projeto existe para evitar:
qualquer tool que rodasse depois de `github.open_pr` na mesma tarefa herdaria um token que já
provou carregar `github:pr:open`, mesmo sem a policy ter decidido isso para ela.

**Verificar o token dentro de `github.open_pr` em vez de num gateway comum.** Funcionaria só
para esta tool, mas duplicaria a lógica de verificação em cada tool futura que precisasse de
identidade, e deixaria a distinção 401/403 (que a ADR-006 já havia atribuído a "o gateway")
sem um lugar único para viver.

**`git push` via um binário `git` dentro de um container com rede liberada só para essa
chamada.** Reabre exatamente a superfície que ADR-004 fechou (o sandbox não tem rede), e ainda
precisaria decidir onde o token mora para o `git` autenticar — de volta ao problema que a API
HTTP evita por completo.

**Uma tabela `github_prs(task_id, branch, pr_number)` para idempotência.** Mais uma fonte de
verdade para manter sincronizada com o GitHub (o que acontece se a PR for fechada e reaberta
manualmente, ou a tabela e o GitHub divergirem?). A branch determinística mais uma consulta ao
GitHub é uma fonte de verdade só, e é a que já manda.

## Consequências

- `run_task`/`run_claimed_task`/`Worker` exigem `keys: KeyPair` agora; todo call site (o loop,
  o worker, `demo.py`, ~50 pontos de teste) foi atualizado. Um teste ou script que rodava a
  tarefa sem chave simplesmente não compila mais, o que é a intenção: não existe caminho para
  rodar uma tarefa sem a chave que assina os tokens que ela vai precisar emitir.
- Toda chamada ALLOW agora grava uma linha em `issued_tokens` além do que já gravava; o volume
  cresce proporcional ao de `tool_calls`. Aceitável: é uma tabela pequena, já indexada por
  `jti`, e `revoke_all_for_task` a limpa (marca `revoked_at`, não deleta — ADR-005 já decidiu
  isso) ao fim de cada tarefa.
- `github.open_pr` só aparece no registry quando `github_repo` e `github_token` estão
  configurados (`build_registry`, mesma forma "ausente, não recusando" que `SecretNotConfigured`
  já usa). Um deploy sem essas duas variáveis nunca oferece a tool ao modelo.
- **O que isto não protege:** um segredo colado dentro de um arquivo que `github.open_pr`
  publica (o conteúdo do arquivo vira o corpo de um blob, e nada aqui varre conteúdo de
  arquivo por padrão de segredo — esse é o trabalho de um scanner no repo alvo, gitleaks já
  está no plano do CI). Nem uma colisão dos 8 primeiros caracteres do `task_id` com o mesmo
  slug (32 bits, improvável mas possível): a segunda tarefa avançaria a branch da primeira e
  receberia a PR dela. Nada é reescrito (só fast-forward), mas as duas tarefas se misturam; o
  remédio, se aparecer, é um prefixo maior do `task_id` no nome da branch.
- O relatório da PR (`_pr_report`) inclui o `task_id` e a lista de arquivos; não inclui nada do
  `body` além do texto que o próprio agente escreveu, que já passou pela política antes de
  chegar aqui (é o resumo da tarefa, não uma tool call nova).

## Adendo (ADR-028)

`github.open_pr` deixou de ser oferecida ao modelo. `build_registry` não a registra mais; ela
vive em `build_publish_registry`, que o control plane usa na fase de publicação depois do
verdict, com os mesmos `path_inspector`, escopo e token por chamada descritos aqui. O trecho
"só aparece no registry quando `github_repo` e `github_token` estão configurados" vale agora
para esse registry: sem as duas variáveis não há fase de publicação.
