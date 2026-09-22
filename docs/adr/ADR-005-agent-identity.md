# ADR-005: identidade de agente, JWT RS256 curto por run, revogação por jti

**Status:** aceita · **Data:** 2026-09-20 · **PR:** `feat/identity-jwt`

## Contexto

O briefing (seção 12 e tabela de riscos) já fecha a forma: "identity.token(run) → JWT 15 min,
scopes, jti revogável". A pergunta que esta ADR responde é a que fica depois de aceitar essa
forma: por que RS256 e não HS256, por que existe uma tabela `issued_tokens` se um JWT é para ser
stateless, e o que acontece quando um token bate certinho na assinatura mas ninguém aqui se
lembra de ter emitido ele.

`identity` (briefing, seção 10) emite e valida token de agente e de usuário, e mais nada:
não decide se uma tool call é permitida (isso é `policy`), não guarda segredo em texto no banco,
e nesta PR não está ligado a `core/loop.py` nem a `identity/broker.py`. É biblioteca com teste,
ainda não é fronteira imposta.

## Decisão

**RS256, chave de assinatura própria, `kid` no header, e uma tabela `issued_tokens` como
segunda verdade além da assinatura. Nenhuma das duas partes sozinha resolve o problema.**

### RS256 em vez de HS256

HS256 assina e verifica com o mesmo segredo. Todo processo que precisa *verificar* um token
com HS256 precisa ter esse segredo em mãos, e um segredo que mais de um processo conhece é um
segredo que vaza mais fácil. RS256 separa os dois papéis: a chave privada assina, só quem tem
essa chave emite token; a chave pública verifica, e chave pública não é segredo. Hoje só o
próprio control plane verifica, então a diferença ainda não aparece na prática. Ela importa a
partir do momento em que o gateway MCP (briefing, seção 8) ou uma tool executando fora deste
processo também precisa verificar um token: com RS256 essa tool recebe a chave pública e nunca
precisa da chave que assina nada. Com HS256, distribuir verificação seria distribuir o segredo
de emissão junto.

### 15 minutos, e é teto, não só default

`AGENT_TTL_CAP_SECONDS = 900` no código, não um valor que `issue_agent_token` aceita e confia
que quem chama vai respeitar. Um token de agente autoriza uma tool call dentro de uma execução;
15 minutos é tempo mais que suficiente para isso e curto o bastante para que um token vazado
(log, crash dump, replay de rede) pare de valer sozinho, sem precisar de revogação ativa. Token
de usuário usa um teto próprio, mais alto (`USER_TTL_CAP_SECONDS`, um dia): o briefing não fixa
esse número para sessão de usuário, então ele é um placeholder deliberado até este projeto
desenhar login de verdade, e está marcado como tal no código (comentário `ponytail:`).

### `issued_tokens`: por que um JWT "stateless" precisa de linha no banco

JWT foi desenhado para que verificar não precise consultar nada: a assinatura já garante que o
conteúdo não mudou. Esse desenho parte de um pressuposto que este projeto rejeita de propósito,
o de que o emissor nunca vai precisar invalidar um token antes do `exp` chegar. O briefing
descreve exatamente esse caso (seção 25, contenção de incidente): um comportamento suspeito
cancela a tarefa e revoga todos os `jti` dela, sem esperar 15 minutos. Sem uma tabela para
marcar "isto não vale mais", revogação simplesmente não existe. `issued_tokens` é esse estado
guardado ao lado da assinatura, não no lugar dela.

**Consequência que fica documentada aqui**: `verify()` faz uma consulta ao banco por token
verificado (`session.get(IssuedToken, jti)`), sempre, mesmo para o caminho feliz. Isso é o
custo de ter revogação de verdade em vez de só confiar no `exp`. Aceitável neste projeto, a
mesma escala que faz o Postgres servir de fila (ADR-002) serve aqui: dezenas de tarefas por dia,
não milhares de verificações por segundo.

**E essa mesma consulta fecha um segundo buraco, não relacionado a revogação**: um `jti`
assinado corretamente mas que não existe em `issued_tokens` é recusado (`InvalidToken`), do
mesmo jeito que um token revogado. Um token assinado com a chave certa só existe se essa chave
assinou ele, ou seja, um `jti` desconhecido com assinatura válida só acontece se a chave privada
vazou, ou se algum bug em outro lugar do sistema passou a assinar token por fora deste módulo.
Em qualquer um dos dois casos, confiar na assinatura sozinha seria o erro: a política aqui é
fail closed, não "assinatura bateu, deixa passar".

### O token tem que dizer exatamente o que foi emitido

Recusar `jti` desconhecido não basta contra a chave vazada, e a primeira versão parava aí. Quem
tem a chave não precisa inventar um `jti`: pega um **vivo** e re-assina com o que quiser. Foi
provado na revisão com um token de `['repo:read']` re-assinado com `admin`, e
`verify(required_scope='admin')` aceitou, porque os scopes saíam do payload.

A linha de `issued_tokens` é o registro do que foi emitido, então é ela que responde: `sub`,
`scopes`, `exp` e `typ` do payload são comparados com a linha, e qualquer divergência é
`InvalidToken`. Duas decisões dentro disso:

- **Recusar, não "corrigir".** A alternativa era aceitar o token forjado e devolver os claims
  da linha. Funciona, e transforma uma chave vazada num token que continua funcionando com as
  permissões originais, sem ninguém notar. Divergência entre token e linha só acontece por
  falsificação, então é tratada como o incidente que é.
- **`exp` e `typ` entram na comparação.** O PyJWT só conhece o `exp` que o próprio token
  afirma, e um token re-assinado afirma o que quiser: sem comparar, um `jti` de 15 minutos
  virava um de um ano. `typ` não tem coluna; a linha responde por ele pelo `subject` que este
  módulo mesmo gravou (`agent:task:<id>` ou `user:<id>`).

O que continua fora do alcance disto: com a chave vazada e um `jti` vivo, o atacante ainda pode
re-assinar o token **idêntico**, o que não lhe dá nada além do que o token original já dava,
por no máximo 15 minutos, e `revoke_all_for_task` corta.

### Revogar é um UPDATE com guarda, não ler e depois escrever

`revoke()` faz `UPDATE ... WHERE revoked_at IS NULL RETURNING`, e só audita as linhas que o
banco devolveu. A versão de ler a linha e conferir `revoked_at` em Python errava de dois jeitos,
os dois achados na revisão causando a situação de verdade: uma session que já tinha a linha em
memória via a própria cópia velha (a mesma armadilha do identity map que `verify()` documenta),
e duas sessions revogando ao mesmo tempo viam `NULL` as duas. Nos dois casos a mesma revogação
era auditada duas vezes, num log que não se corrige depois, e o primeiro `revoked_at` era
sobrescrito. É a regra do projeto aplicada: quando o banco pode garantir, a garantia mora nele.

O primeiro teste dessa corrida disparava dois `revoke` com `gather` e passava também no código
errado, porque os dois simplesmente rodavam em sequência. O teste atual força o cruzamento: A
revoga e segura a transação aberta, B começa enquanto A não commitou.

### O ataque de confusão de algoritmo, e por que a lista de algoritmos é fixa

`jwt.decode(..., algorithms=["RS256"])`, nunca lendo o campo `alg` do header do próprio token
para decidir como validar. O ataque clássico contra bibliotecas JWT mal configuradas é exatamente
isso: gerar um token com `alg: HS256` e assinar com HMAC usando a **chave pública RS256** como
segredo. Se o verificador confia no `alg` do token e troca de RS256 para HS256 dinamicamente,
ele acaba comparando um HMAC calculado com um segredo que, por definição, é público, e qualquer
um que conheça a chave pública consegue forjar um token válido. Fixar `algorithms=["RS256"]`
elimina essa decisão: PyJWT recusa o header `HS256` antes de chegar perto de comparar qualquer
assinatura. `tests/test_identity.py` cobre isso construindo esse token à mão (PyJWT se recusa a
*assinar* HS256 com uma chave em formato PEM, então o ataque tem que ser montado byte a byte
para o teste existir).

### `kid`, hoje sem uso real

`kid` no header é um hash curto da chave pública em DER. Com uma chave só, ele não decide nada,
`verify()` sempre usa a mesma chave pública. Ele existe porque a alternativa (trocar de chave
sem `kid`) obriga a saber, fora do token, qual chave verificar cada um, o que quebra no instante
em que duas chaves coexistem durante uma rotação. Com `kid` já no header desde o início, rotação
de chave vira "`verify()` passa a aceitar um dicionário de `kid` para chave pública" em vez de
mudar o formato do token que já está circulando. Comentário `ponytail:` no código nomeia esse
teto explicitamente.

### Sem OIDC externo

Um provedor OIDC (Auth0, Keycloak, Google) resolveria autenticação de usuário sem este projeto
gerenciar chave nem senha. Não usado aqui porque o objetivo declarado do projeto (briefing,
introdução) é aprender e demonstrar exatamente o que um provedor OIDC esconde: claims, `exp`,
`jti`, rotação de chave, verificação de assinatura. Terceirizar isso reduziria trabalho e reduziria
também o que há para defender em entrevista. Fica como leitura comparativa depois de ter a versão
própria funcionando, não como próximo passo.

## O que isto NÃO faz ainda (a pergunta que a entrevista faz)

- **A chave privada mora em disco, sem HSM nem KMS.** `.keys/jwt-private.pem`, permissão de
  arquivo do sistema operacional é toda a proteção que existe hoje. Aceitável para uma máquina
  de desenvolvimento de um projeto solo; a mitigação de livro didático (HSM, KMS, ou pelo menos
  a chave cifrada em repouso com uma senha fora do repositório) fica para quando houver ambiente
  de produção de verdade.
- **Uma chave só, sem rotação.** O `kid` deixa a rotação possível sem trocar o formato do token,
  mas `verify()` ainda aceita uma `KeyPair`, não um mapa de `kid` para chave pública. Implementar
  rotação de fato é a próxima peça, não esta.
- **Nada em `core/loop.py` chama isto.** Emitir um token por tool call, na hora de decidir
  `allow` na policy, é o próximo PR. Hoje `identity` é uma biblioteca testada isoladamente, o
  briefing descreve o loop chamando `identity.token(run)`, mas essa chamada ainda não existe.
- **`identity/broker.py` (Secret Broker) é outra PR.** Este módulo prova identidade ("quem é
  você"), não credencial de terceiro ("aqui está seu token do GitHub"). ADR-006 vai cobrir isso
  quando existir.

## Alternativas consideradas

**HS256 com um segredo compartilhado.** Recusada pelo motivo já descrito: verificação distribuída
exigiria distribuir o segredo de emissão, e o briefing já aponta o gateway MCP como um segundo
verificador futuro.

**Confiar só no `exp`, sem tabela de revogação.** Recusada porque o briefing exige contenção de
incidente que revoga token antes do prazo (seção 25). Sem estado, "revogar" não significa nada.

**OIDC externo (Auth0/Keycloak) para usuário, JWT próprio só para agente.** Recusada por ora:
duas fontes de identidade dobra a superfície a entender e a explicar, para um projeto de portfólio
solo. Reavaliar se este projeto algum dia precisar de login social ou SSO corporativo de verdade.

## Consequências

- `core/loop.py` (próxima PR) passa a chamar `issue_agent_token` no caminho `allow` da policy, e
  `sandbox`/`mcp` (também próximas PRs) passam a chamar `verify()` antes de executar qualquer
  tool. Até lá, um token emitido por este módulo não é checado por mais ninguém no sistema.
- `incidents` (briefing, seção 25) chama `revoke_all_for_task` na contenção; a função já existe e
  já tem teste, só falta o detector que a aciona.
- Cada `verify()` bem-sucedido é uma consulta a mais no banco por tool call. Se o volume de
  tool calls por segundo algum dia deixar de ser desprezível perto do resto do sistema (chamada
  de modelo, execução em sandbox), cachear tokens não revogados por alguns segundos é a saída
  documentada, não abandonar a checagem de `jti` desconhecido.
