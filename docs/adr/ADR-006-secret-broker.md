# ADR-006: Secret Broker, credencial só para claims de agente já verificadas

**Status:** aceita · **Data:** 2026-09-22 · **PR:** `feat/secret-broker`

## Contexto

ADR-005 fechou identidade: `identity.verify()` prova quem está pedindo algo, um `Claims` com
`typ`, `scopes` e `task_id` que vêm da linha `issued_tokens`, não do payload que o token afirma
sobre si mesmo. O que faltava, e que a própria ADR-005 deixou explícito ("`identity/broker.py`
é outra PR"), é o segundo passo: **identidade provada não é credencial**. Provar que um agente é
quem diz ser não entrega a ele um token do GitHub; alguma coisa ainda precisa decidir se essa
identidade, com esse escopo, ganha esse segredo, e fazer isso sem nunca deixar o segredo passar
pelo contexto do modelo (briefing §21, tabela de ameaças: "Agente com credencial ampla →
exfiltração → Secret Broker: modelo nunca vê credencial").

## Decisão

**`identity.broker.get_credential(session, claims, scope)` devolve um `Credential` só para uma
`Claims` de agente, vinculada a uma tarefa, cujos `scopes` já incluem o `scope` pedido.** O
segredo em si vive só no ambiente do control plane (`Settings.github_token`, `SecretStr`), nunca
no banco. Toda chamada, concedida ou recusada, grava uma linha de auditoria.

### Por que o modelo nunca vê a credencial

O modelo produz tool calls; o control plane decide o que executar (a tese do projeto, CLAUDE.md).
Se o valor do token do GitHub aparecesse em qualquer mensagem que volta para o modelo, prompt
injection deixaria de precisar quebrar nada: bastaria pedir educadamente para o modelo repetir o
que já tem. A tool `github.open_pr` (wave 4, fora do escopo desta PR) recebe um `Credential` e
chama `.reveal()` **dentro do processo do control plane**, no gateway MCP, nunca dentro do
sandbox e nunca em texto que vira parte do prompt. `Credential` (este PR) é o que torna esse uso
único verificável: `repr`/`str` nunca mostram o valor, então qualquer lugar que efetivamente lê o
segredo grita `.reveal()` no código, e é `grep`ável.

### Por que exigir `Claims` verificada em vez de confiar num `task_id`

A alternativa óbvia e mais simples de escrever seria `get_credential(task_id, scope)`: qualquer
código com um UUID de tarefa em mãos pediria o segredo dela. Rejeitada porque um `task_id` não
prova nada, é um valor que aparece em log, em URL, em evento; **quem** está pedindo importa tanto
quanto **para qual tarefa**. Exigir uma `Claims` já passada por `identity.verify()` amarra a
concessão à mesma cadeia de prova que a ADR-005 construiu: assinatura RS256 válida, `jti` vivo
em `issued_tokens`, `scopes` que batem com o que foi realmente emitido. Um chamador só chega ao
broker com uma `Claims` de agente depois de passar pelo mesmo caminho que autoriza uma tool call
qualquer, então o broker herda de graça as garantias que `verify()` já fechou (payload que
diverge da linha é recusado, `jti` revogado é recusado) em vez de reimplementar uma versão mais
fraca delas.

Duas checagens adicionais, deliberadamente redundantes com o que `verify()` já faz, porque o
broker não pode assumir que todo chamador futuro passou por ele:

- **`typ == "agent"` e `task_id is not None`.** Um token de usuário nunca deveria trocar por
  credencial de terceiro; a UI autentica com o próprio login, não com um segredo de agente. Um
  `typ == "agent"` sem `task_id` não é algo que `issue_agent_token` consegue produzir (o
  parâmetro é obrigatório), então vê-lo aqui significa que alguém montou uma `Claims` por fora do
  módulo de identidade, e a resposta correta é recusar, não confiar.
- **`scope in claims.scopes`.** O broker não decide *quais* scopes um agente ganha, isso é
  responsabilidade de quem emite o token (`policy`, no caminho `allow`, briefing §22 item 6). Ele
  só confere que o pedido bate com o que já foi concedido, a mesma separação que `identity` já
  tem entre autenticar (`verify`) e autorizar (`required_scope`).

### Por que ambiente, não banco

O briefing (`identity`, §10: "não faz: armazenar segredos em texto no DB") e ADR-018 já tomaram
essa posição para segredo em geral: `.env` nunca entra no sandbox porque não pode ser lido de
lugar nenhum que o agente alcance. Guardar o token do GitHub numa coluna, mesmo cifrada,
adicionaria uma chave de cifragem para gerenciar e um caminho de leitura a mais para auditar, sem
comprar nada que o ambiente do processo já não dê: o control plane já é o único processo que
precisa do valor, e o ambiente já é onde `ANTHROPIC_API_KEY` mora hoje. `SecretStr` é a única
camada nova, e existe só para que um `print(settings)` ou uma exceção não capturada não
imprima o valor por acidente.

### A tabela scope → segredo

```python
_SCOPE_TO_SETTING: dict[str, str] = {
    "github:pr:open": "github_token",
    "github:repo:read": "github_token",
}
```

Uma tabela só, no código, não em `policies/*.yaml`: ela não decide *quem* recebe um scope (isso é
a policy engine), só *qual variável de ambiente* um scope já autorizado resolve para. Um scope
fora da tabela é `UnknownScope`, recusado antes de qualquer tentativa de ler configuração. Hoje
os dois scopes apontam para o mesmo `github_token` porque este control plane só fala com um
provedor externo; um segundo provedor (ou um GitHub App por instalação, no lugar de um PAT único)
vira uma linha nova aqui, não uma reestrutura.

### `redact()`: por que existe nesta PR sem nenhuma tool ainda usando

O item da semana 3 "grep em logs e em `task_events` não encontra o token do GitHub" só é
verificável quando existir alguma coisa que escreva o token nesses dois lugares, e isso é a tool
`github.open_pr` da wave 4. `redact()` é construído e testado agora, isolado, porque a função em
si (substituição de substring literal, não regex, para não interpretar caracteres do próprio
segredo como metacaracteres) não depende de a tool existir, e adiar a escrita para quando a
integração for cobrada juntaria duas mudanças (a tool e a garantia de que ela não vaza) num PR só.

## O que isto NÃO protege

**Um processo do control plane comprometido.** Se um atacante ganha execução de código dentro do
processo que já tem `Settings.github_token` no ambiente, ele lê a variável de ambiente
diretamente, sem precisar passar pelo broker, pela mesma razão que qualquer segredo de processo
é indefensável contra o próprio processo. O broker defende contra o **agente rodando no
sandbox** (que nunca tem esse ambiente, ADR-018) e contra **código que tenta pedir o segredo sem
provar identidade correta** (task id sozinho, claims de usuário, scope não concedido). Não
defende contra RCE no control plane em si; isso é superfície do host Docker e do próprio
processo Python, fora do que um broker de aplicação consegue resolver. Mitigação de livro
didático (Vault, KMS com credencial de curtíssimo prazo emitida por chamada, não por processo
inteiro) fica para quando houver ambiente de produção de verdade, a mesma ressalva que ADR-005
já faz para a chave privada de assinatura.

**Rotação e escopo real por chamada do PAT do GitHub.** O `github_token` de hoje é um Personal
Access Token único, com o escopo que ele tiver no GitHub, não um escopo que este control plane
consiga restringir por baixo. "TTL lógico" (briefing item 17) significa que o broker controla
por quanto tempo o *processo do agente* tem acesso ao valor, não que o token do GitHub em si
expire nesse intervalo. Um GitHub App com token de instalação de curta duração fecharia essa
lacuna; fica registrado como o próximo passo óbvio, não implementado aqui.

## Alternativas consideradas

**`get_credential(task_id, scope)`, lendo a tarefa do banco para achar quem a pediu.** Mais
simples de escrever, mas move a prova de identidade para uma tabela que qualquer código com
acesso ao banco já lê, em vez de exigir a cadeia de assinatura e revogação que ADR-005 construiu
para exatamente este propósito.

**Guardar o token cifrado em `secrets` no Postgres.** Recusada pelo mesmo motivo do briefing
citado acima: não compra isolamento novo (o control plane ainda precisa da chave de decifrar, no
mesmo ambiente onde o valor em claro já poderia estar) e adiciona uma tabela e uma chave para
gerenciar.

**Uma exceção só (`BrokerError`) para toda recusa.** Mais curto, mas esconde a diferença entre
"o chamador não tem permissão" (`CredentialDenied`, 403), "o chamador pediu algo que não existe"
(`UnknownScope`, erro de programação, não de segurança) e "está tudo certo mas falta configurar"
(`SecretNotConfigured`, problema operacional). A wave 4 (gateway MCP) precisa tratar essas três
de formas diferentes; três classes pequenas custam menos do que reconstruir essa distinção depois
a partir de uma string de mensagem.

## Consequências

- `core/loop.py` e o gateway MCP (waves futuras) chamam `get_credential` no caminho de uma tool
  como `github.open_pr`, com a `Claims` que `identity.verify()` já produziu para aquela tool
  call. Até lá, nada no sistema chama este módulo além dos próprios testes.
- Toda concessão e toda recusa vira uma linha em `audit_log` (`credential.granted` /
  `credential.denied`), na mesma cadeia hash que `token.issued`/`token.revoked` já usam.
  `audit.verify()` cobre estas linhas do mesmo jeito.
- `redact()` fica pronta e testada isolada; falta a wave 4 chamá-la de fato em `structlog` e em
  `task_events` antes que o item de checklist correspondente feche.
- Um segundo segredo (segundo provedor, ou um GitHub App por instalação) adiciona um campo em
  `Settings`, uma linha em `_SCOPE_TO_SETTING` e uma entrada em `_configured_secrets`; não pede
  mudança de forma no broker.
