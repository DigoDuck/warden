# Roteiro de teste da UI

Uma tarefa que passa por todas as decisões do control plane: um `allow`, um `deny` com regra
e um `require_approval` que pausa a tarefa na fila de aprovações. Serve para fechar por clique
os itens da semana 4 que os testes automatizados (jsdom, `fetch` falso) não alcançam.

## Preparar

```bash
docker compose up -d db
make migrate
make keys          # só na primeira vez
make sandbox-image
```

Três terminais, na raiz do repositório:

```bash
make api
make frontend-dev
uv run --directory backend python -m warden.core.worker --script ../examples/walkthrough/script.yaml --policy ../examples/walkthrough/policy.yaml
```

O worker roda o FakeProvider com `resume_aware`, então a tarefa retomada depois da aprovação
continua o roteiro do passo 4 em vez de recomeçar.

Token (escopos de tarefa e de aprovação), colado na tela Configurações:

```bash
make user-token email=voce@exemplo.com scopes="tasks:write tasks:read approvals:read approvals:decide audit:read"
```

## O que conferir

1. **Nova tarefa:** submeter qualquer texto. A tela vai para o detalhe, na aba Execução.
2. **Eventos ao vivo:** os eventos aparecem um a um. `read_file .env` aparece como **Negado**,
   com a regra `never-read-secrets` em fonte mono.
3. **Pausa:** a tarefa fica em **Aguardando aprovação** e o item Aprovações da barra lateral
   mostra 1.
4. **Aprovar:** em Aprovações, aprovar a escrita de `NOTES.md`. O card some, o contador zera,
   e a tarefa volta a executar e termina **Concluída**.
5. **Rejeitar (segunda tarefa):** o botão fica desabilitado até existir uma nota, pede
   confirmação, e a tarefa continua com a recusa em vez de parar.
6. **Teclado:** do topo, Tab mostra "Pular para o conteúdo". Na lista, Tab chega a cada linha
   e Enter abre o detalhe. No detalhe, as setas trocam de aba.
7. **Rede:** no DevTools, só requisições `woff2` da própria origem, nenhuma URL com token, e a
   requisição `/stream` reconectada leva o cabeçalho `Last-Event-ID`.
