# ADR-012: o worker tem acesso ao socket do Docker

**Status:** aceita, com risco declarado · **Data:** 2026-09-20 · **PR:** `feat/sandboxed-tools`

## Contexto

A partir deste PR o worker cria containers: um sandbox por tarefa, com o workspace dentro.
Para isso ele fala com a Docker Engine API, o que significa acesso ao socket do Docker.

**Acesso ao socket do Docker equivale a root no host.** Quem fala com o daemon pode subir
um container privilegiado montando `/` e ler ou escrever qualquer coisa. Não é uma
permissão parcial que dá para apertar: é total.

Existe uma ironia que vale nomear em voz alta, porque ela vai aparecer em entrevista: o
componente que existe para conter o agente roda com o privilégio mais alto da máquina.

## Decisão

**O worker tem o socket. O agente não, nunca, em nenhuma circunstância.**

A separação é a fronteira do container, e ela é testada:

- o sandbox roda com `cap_drop=ALL`, non-root, rootfs read-only e `--network none`
- **nenhum bind mount**, então em particular nunca o socket: `test_the_docker_socket_is_not_mounted`
  verifica que o caminho não existe e que o único mount é o volume do workspace
- o agente só alcança o daemon se escapar do container, e aí o socket é o menor dos problemas

O que o modelo pode fazer continua passando pelo policy engine antes de virar `exec`. O
socket não amplia a superfície do agente; ele amplia a do **worker**, que é código nosso.

## Alternativas consideradas

**Docker rootless.** A mitigação real, e está no §54 como próximo passo de hardening.
Recusada agora por custo de ambiente: exige configuração no host, muda o caminho do socket,
e no Docker Desktop do Windows a história é diferente da do Linux da VPS. Adotar sem ter os
dois ambientes iguais trocaria um risco conhecido por um comportamento divergente entre
desenvolvimento e produção.

**Um proxy de socket com allowlist de endpoints** (estilo `docker-socket-proxy`). Reduz de
verdade: o worker só precisa de `containers`, `volumes` e `exec`, não de `system` ou
`images/build`. Adiado, não recusado. É a mitigação mais barata do roadmap e não depende de
mudar o host.

**Executar o sandbox por outro runtime** (Podman sem daemon, gVisor). Muda o modelo de risco
mas troca a base do projeto no meio, e o §54 já classificou como pós-v1.

**Dar o socket à API em vez do worker.** Pior: a API tem superfície de rede e recebe entrada
de usuário. O worker não escuta em porta nenhuma.

## Consequências

- **Risco residual aceito e escrito:** comprometer o processo do worker é comprometer o
  host. A defesa é que o worker não expõe porta, não interpreta entrada de usuário, e nunca
  executa texto vindo do modelo. Tudo que vem do modelo vira argumento de tool validado por
  schema e julgado pela policy antes de virar `exec`.
- O worker tem que rodar isolado da API. No Compose já são serviços separados, e no deploy
  da semana 12 continuam.
- Se o proxy de socket entrar, esta ADR ganha um adendo em vez de ser substituída: a decisão
  de o worker criar containers não muda, só o quanto de API ele alcança.
- Vale para o README e para entrevista: **não existe sandbox sem alguém privilegiado para
  construí-lo**. A pergunta certa não é "como evitar isso", é "quem é esse alguém, o que
  exatamente ele consegue fazer, e o que separa ele do código que não é confiável".
