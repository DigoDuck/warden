# ADR-020: primeira superfície HTTP, autenticação por bearer e escopos

**Status:** aceita · **Data:** 2026-09-22 · **PR:** `feat/api-skeleton`

## Contexto

Briefing §14 lista o conjunto final de endpoints; esta PR entrega o subconjunto que não depende
de nada ainda não construído: `POST /tasks`, `GET /tasks/{id}`, `GET /tasks/{id}/events`,
`GET /audit/verify`, `GET /healthz`. Fora do escopo por dependerem de peças de semanas
seguintes: SSE (`/tasks/{id}/stream`), `cancel`, aprovações, `POST /auth/login` (semana 4, junto
do frontend que o chamaria).

`api` (briefing §10) valida entrada, autentica usuário e expõe estado; não decide lógica de
agente. Esta ADR registra as decisões que não são óbvias a partir dessa frase.

## Decisão

**Autenticação: bearer JWT de usuário, emitido por `make user-token`, não por login.**
`identity/jwt.py` (ADR-005) já emite e valida token de usuário; faltava só um jeito de alguém
ter um token na mão sem UI. `make user-token email=... scopes="..."` faz o-cria-se-não-existir do
`users` e chama `identity.issue_user_token`. Alternativa descartada: escrever `/auth/login`
agora. Não há tela para chamá-lo antes da semana 4, e um endpoint de senha sem UI de cadastro
é herança que sobra sem uso; a linha de corte do projeto (briefing, "maior risco não é técnico")
é exatamente não construir isso. O CLI é descartável: quando `/auth/login` existir, `make
user-token` continua servindo para gerar token de teste sem depender de uma senha real.

TTL do token gerado: 1 hora (`user_token.DEFAULT_TTL_SECONDS`), configurável por
`--ttl-seconds`, sob o teto de `identity.USER_TTL_CAP_SECONDS` (1 dia, ele mesmo um `ponytail:`
por não haver refresh de sessão ainda). Curto o bastante para limitar o estrago de um token de
desenvolvedor vazado, longo o bastante para não expirar no meio de uma sessão manual de `curl`.

**Só token `typ == "user"` é aceito nesta API, e o motivo de rejeitar com 403.** Um token de
agente (`identity.issue_agent_token`) é escopado para uma execução de uma tarefa; nada aqui
deveria aceitá-lo como credencial de quem opera o control plane. A escolha entre 401 e 403 para
esse caso: **403**. A assinatura e as claims já se provaram válidas (`identity.verify()` não
levantou `InvalidToken`), então isto não é "quem é você" (autenticação), é "este tipo de
credencial não serve para esta audiência" (autorização) — a mesma distinção que `InsufficientScope`
já usa para escopo faltante. Na prática, um token de agente quase sempre já cairia em 403 por
não ter os escopos `tasks:*`/`audit:*` (os dele são `repo:read` etc., vindos de decisão de
policy); a checagem explícita de `typ` existe para o caso em que, por coincidência de nomes, ele
tivesse o escopo certo — defesa em profundidade, não o caminho comum.

**Escopos:** `tasks:write` (POST /tasks), `tasks:read` (GET task e events), `audit:read` (GET
/audit/verify). Um por verbo/recurso, não um `tasks:*` genérico: a separação write/read já
paga por si em `make user-token` (um script de leitura só nunca ganha permissão de enfileirar
tarefa) e no princípio de menor privilégio do briefing.

**404, não 403, para a tarefa de outro usuário — a menos que o token tenha o escopo `admin`.**
Responder 403 confirmaria que o id existe; 404 não distingue "não existe" de "não é seu".
`admin` é só mais um escopo (não um campo `role` separado em `users`, que também existe mas
esta API não lê): mantém a checagem de visibilidade no mesmo lugar que todas as outras
(`Claims.scopes`), sem uma segunda fonte de verdade para "quem pode ver tudo".

**Idempotência: criado-vs-repetido sem alterar `core/queue.py`.** `core.queue.enqueue` só devolve
a `Task`, criada ou pré-existente, não diz qual. Não dá para tocar `core/*` nesta PR (trilha
paralela dona do módulo). A saída: depois de `enqueue()`, uma consulta simples decide — existe
uma linha `audit_log` com `action="task.submitted"` e `target_id=<task.id>`? Se não, esta
chamada é quem criou, grava a linha e retorna 201; se sim, é replay, retorna 200. Isso não é uma
heurística torcendo os dedos: o índice único de `idempotency_key` faz o Postgres **serializar**
duas inserções concorrentes com a mesma chave (a segunda trava dentro do próprio `enqueue()` até
a transação da primeira terminar), então, quando a segunda chega a este ponto, a auditoria da
primeira (se ela de fato committou) já existe. A pergunta "esta é a primeira vez?" tem sempre uma
resposta correta no momento em que ela é feita.

**Chave de idempotência de outro usuário: 409, nunca a tarefa dele.** O índice único de
`tasks.idempotency_key` é global, não por usuário, então `enqueue()` devolve a tarefa de quem
quer que já tenha usado a chave. Devolvê-la vazaria a tarefa (e o `spec`) de outro usuário para
quem adivinhasse ou reutilizasse a chave. A rota compara `task.user_id` com o chamador e responde
409 sem corpo útil. O 409 ainda confirma que a chave existe; aceitável, porque a chave é um valor
aleatório escolhido pelo cliente e não identifica nada. O conserto de raiz seria um índice único
em `(user_id, idempotency_key)`, que é migração de `models.py` e fica para quem for dono dele.

**`GET /tasks/{id}/events` devolve o `payload` exatamente como gravado, e isso inclui argumento
de tool sem redação.** `core/loop.py::_request_tools` grava `tool.requested` com
`"arguments": call.arguments` completo — diferente de `tool_calls.args_safe`, que redige
segredos (`core/events.py::redact_args`). O mesmo vale para a saída de tool (`tool.completed`)
e o `raw_content` do modelo: o log de eventos guarda exatamente o que o modelo viu e produziu,
porque é disso que o replay precisa. Hoje isso não é um vazamento vivo: nada no desenho do
projeto entrega segredo ao modelo (o broker nunca passa credencial para ele, briefing §10), então
não há segredo para aparecer nesses campos para começar (ADR-018 tira os segredos
do sandbox, e com isso do que as tools devolvem). Mas é a fronteira que passaria a
importar no dia em que isso mudasse, e por isso `tasks:read` — não "sem autenticação" — guarda
este endpoint.

**Dependências novas, e por que nenhuma cabe na stdlib:**

- `fastapi`: roteamento tipado sobre ASGI, validação Pydantic v2 na fronteira e OpenAPI de
  graça. Já é a escolha fechada do briefing (§11); esta PR só a instala.
- `uvicorn[standard]`: servidor ASGI. O extra `[standard]` traz `httptools`/`watchfiles` (parse
  HTTP mais rápido, `--reload` em dev); sem ele o pacote básico ainda funciona, mas o padrão
  documentado do próprio FastAPI é `[standard]`, e o custo é só um pacote a mais no lockfile.
- `httpx` (dev): já era dependência transitiva de `anthropic`, mas não instalada como direta —
  os testes importam `httpx.ASGITransport`/`AsyncClient` diretamente, então precisa estar
  declarada, não só presente por acidente de outra biblioteca.

**Limites de corpo:** `spec` até 20.000 caracteres (é uma descrição de tarefa, não um lugar para
colar um repositório inteiro) e `target_repo` até 512, o mesmo tamanho da coluna
(`tasks.target_repo`, `String(512)`) — validar antes evita um erro feio de driver por string
longa demais, em vez de um 422 legível.

**Corpo do 422 sem o `input` rejeitado.** O handler padrão do FastAPI devolve o valor que falhou
em cada erro. Isso reflete conteúdo da requisição na resposta (até 20.000 caracteres de `spec`
hoje, uma senha quando `/auth/login` existir), e um `max_usd: Infinity`, que o parser JSON do
Python aceita, nem serializa e vira 500. `create_app` troca o handler por um que devolve só
`type`, `loc` e `msg`, o bastante para o cliente corrigir a requisição.

## Alternativas consideradas

**Checar `users.role == "admin"` em vez de um escopo `admin`.** Duas fontes de verdade para
"pode ver tudo": o token carregaria escopos de tarefa e o banco carregaria papel de usuário, e
um seria esquecido ao mudar o outro. Um escopo mantém tudo que autoriza vindo do mesmo lugar.

**401 para token de agente.** Descartado acima: a credencial é válida, só não serve aqui.

## Consequências

- Sem `/auth/login`, todo acesso à API depende de alguém rodar `make user-token` com acesso ao
  banco e à chave privada — aceitável para uma API ainda sem UI, mas quer dizer que **não existe
  fluxo de logout ou de troca de senha** até a semana 4.
- `GET /tasks/{id}/events` sem paginação por tempo, só por `seq`: suficiente porque é o cursor
  que já ordena o log (briefing §12), e evita reintroduzir um segundo critério de ordenação.
- O que esta PR não cobre, de propósito: SSE, cancelamento, aprovações. Cada um pede um design
  próprio (streaming, ou um efeito colateral em `core`) que não cabe no "primeiro esqueleto".
