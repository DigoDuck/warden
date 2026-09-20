# ADR-007: auditoria tamper-evident com hash chain e privilégios de banco

**Status:** aceita · **Data:** 2026-09-20 · **PR:** `feat/audit-log`

## Contexto

O briefing (§25) lista "adulteração de auditoria: apagar rastro" como um risco explícito, com
mitigação prevista de hash chain mais revogação de `UPDATE`/`DELETE` no banco. A pergunta que
guia esta ADR é a que um entrevistador faz primeiro: se alguém com acesso ao Postgres reescrever
uma linha, como o sistema percebe?

`audit_log` é intencionalmente independente do resto do modelo (`task_id`, `actor_id` guardados
como texto em `target_id`/`actor_id`, sem foreign key): apagar ou reescrever dados de outra
tabela nunca pode arrastar a trilha de auditoria junto, e a trilha continua fazendo sentido para
atores que nunca tiveram linha própria em outro lugar (um approver, um token já revogado).

## Decisão

**Hash chain em Python, mais privilégio negado no banco. As duas partes fazem trabalhos
diferentes e nenhuma sozinha resolve o problema.**

A hash chain (`warden/audit/log.py`) torna adulteração *detectável*: cada linha guarda
`hash = sha256(prev_hash + JSON canônico dos seus próprios campos)`, então mudar qualquer coisa
numa linha, mesmo escrita há meses, quebra o elo que a linha seguinte depende dela ter. A
migração 0003 torna a escrita de `UPDATE`/`DELETE`/`TRUNCATE` *impossível* para o role da
aplicação (`warden_app`): `REVOKE` no Postgres, não uma checagem em código que um bug pode pular.

Decisões de implementação que valem a pena registrar:

- **JSON canônico** (chaves ordenadas, separadores compactos) e **`ts` serializado em Python
  antes do INSERT**, nunca com `server_default=now()`: o valor precisa existir antes de ser
  hasheado, e sem canonicalização a mesma linha podia gerar hashes diferentes dependendo da
  ordem das chaves no dict.
- **`pg_advisory_xact_lock` numa chave fixa** serializa quem faz append. Sem ele, duas sessões
  concorrentes leem o mesmo `hash` da última linha, cada uma calcula seu `prev_hash` a partir
  dele, e as duas inserem linhas que alegam continuar a cadeia: a cadeia bifurca em vez de
  crescer, e `verify()` teria dois "últimos elos" válidos e nenhum jeito de saber qual é real.
  **Consequência que fica documentada na docstring de `append()`:** é um lock de *transação*,
  liberado só quando a transação do chamador termina (commit ou rollback), não quando a função
  retorna. Por isso um append pertence a uma transação curta; chamar `append()` de dentro da
  transação longa de uma execução de agente prenderia todo outro appender do processo, inclusive
  de tarefas sem relação nenhuma, pelo tempo que aquela transação ficasse aberta.
- **`REVOKE` explícito em vez de confiar em nunca ter dado `GRANT`.** A migração concede
  `SELECT, INSERT, UPDATE, DELETE` em bloco para `warden_app` em todas as tabelas (presentes e
  futuras, via `ALTER DEFAULT PRIVILEGES`), porque a maioria das tabelas precisa mesmo disso.
  Em `audit_log` especificamente, revoga `UPDATE`/`DELETE` de volta. Isso sobrevive a alguém
  esquecendo o caso especial numa tabela nova: o privilégio amplo é a regra, `audit_log` é a
  exceção nomeada, não o contrário.
- **Role de banco criado com `DO $$ ... EXCEPTION WHEN duplicate_object ...`.** Um role é global
  no cluster, não por banco, e várias bases rodam esta migração (dev e uma base de teste por
  checkout, às vezes ao mesmo tempo em CI). Checar `pg_roles` antes de criar reduz a janela de
  corrida, mas não fecha: duas migrações podem ver "ainda não existe" ao mesmo tempo. Capturar
  `duplicate_object` fecha de fato.

## O que isto NÃO protege (a pergunta que a entrevista faz)

- **Tamper-evident não é tamper-proof.** Um superusuário do Postgres pode reescrever a linha
  adulterada *e* recalcular `hash`/`prev_hash` de todas as linhas seguintes para a cadeia
  continuar batendo. `verify()` não pega isso, porque não há nada fora do próprio banco para
  comparar contra. A mitigação de livro didático é ancorar o `hash` da última linha em algum
  lugar fora do alcance de quem administra o Postgres (um log write-once externo, um commit
  assinado, um serviço de timestamping) periodicamente. Não implementado nesta PR.
- **Apagar as ÚLTIMAS linhas (tail truncation) não é detectável só pela cadeia.** `verify()`
  detecta uma linha faltando no MEIO porque a linha seguinte aponta, via `prev_hash`, para um
  hash que não existe mais. Não existe "linha seguinte" para as últimas linhas apagadas: a
  cadeia termina mais cedo e parece perfeitamente válida até ali. A mesma mitigação de ancorar o
  hash da cabeça da cadeia fora do banco, com frequência suficiente, é o que fecha esse buraco;
  também não implementado.
- **Hoje a aplicação ainda conecta como dona do banco, não como `warden_app`.** A migração cria o
  role e os privilégios certos, mas nenhum código do projeto ainda abre conexão com ele: o
  `DATABASE_URL` de desenvolvimento aponta para o usuário `warden`, que é superusuário. Ou seja,
  a imutabilidade que este PR prova é sobre o *role*, testada com `SET LOCAL ROLE warden_app`,
  não ainda sobre o *processo* que roda em produção. Trocar a conexão de runtime para
  `warden_app` exige duas URLs (uma para migração, que precisa criar tabela e role; outra para a
  aplicação, que não deveria ter esse poder), e fica para o próximo passo, não para esta PR.
- **O advisory lock serializa TODOS os appenders do banco**, não só os concorrentes numa mesma
  tarefa. Isso é aceitável aqui porque um append é uma escrita de poucas linhas de JSON, não uma
  chamada de modelo nem uma execução de tool: mesmo serializado, o tempo total gasto dentro do
  lock por tarefa é da ordem de milissegundos. Viraria problema se `audit_log` acumulasse uma
  escrita por segundo por tarefa concorrente numa escala que este projeto não tem; a saída
  documentada nesse cenário seria particionar o lock (uma chave por faixa de tempo ou por
  shard), não abandonar a serialização.

## Alternativas consideradas

**Confiar só no `REVOKE`, sem hash chain.** Recusada. Impede escrita do role da aplicação, mas
não dá nenhum jeito de auditar depois se um superusuário (ou uma migração futura mal escrita que
concede privilégio demais) alterou uma linha. A hash chain é o que torna a adulteração visível
independente de quem a fez.

**Confiar só na hash chain, sem `REVOKE`.** Recusada. Uma hash chain sem controle de escrita no
banco só prova adulteração depois do fato; o objetivo aqui é que a aplicação normal *não consiga*
escrever por engano ou por bug, não só que dê para provar que escreveu.

**Assinatura criptográfica por linha em vez de hash chain.** Mais forte contra um adversário que
não é superusuário do banco, mas exige gestão de chave privada e verificação de assinatura, que é
mais peça nova para um projeto de 12 semanas sem ganhar proteção contra o risco que mais importa
aqui (o próprio operador do banco). Hash chain encadeada já cobre "alguém sem acesso ao banco
alterou uma linha via SQL direto", que é o cenário descrito no briefing.

## Consequências

- `/audit/verify` (semana 4, endpoint ainda não existe) tem uma função pronta para expor: chamar
  `verify()` e devolver o resultado.
- Qualquer código que vier a chamar `audit.append()` (loop do agente, aprovação, emissão de
  token) precisa fazê-lo dentro de uma transação curta, pelo motivo do advisory lock acima. Isso
  fica para quando esse código for escrito (fora do escopo desta PR).
- O teto documentado da imutabilidade é o role do Postgres, não o processo, até a troca de
  `DATABASE_URL` da aplicação acontecer.
