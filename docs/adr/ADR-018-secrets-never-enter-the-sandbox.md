# ADR-018: negação por caminho não sobrevive à execução de código

**Status:** aceita · **Data:** 2026-09-20 · **PR:** `feat/write-tools`

## Contexto

O policy engine nega por caminho. A regra `never-read-secrets` diz que `.env`, chaves e
`secrets/` nunca são legíveis pelo agente, e enquanto as tools só liam isso era verdade: toda
leitura passava por `read_file`, e `read_file` passava pela policy.

Este branch acrescenta `write_file` e o test runner. Com as duas coisas juntas a regra vira
aviso, e bastam duas chamadas que a policy default permite:

1. `write_file("tests/test_leak.py", ...)` com um teste que abre `.env` e levanta o conteúdo.
   Permitido por `write-source`, porque `tests/**` é onde um worker trabalha.
2. `run_command("pytest tests/test_leak.py")`. Permitido por `run-project-commands`.

O pytest executa o que o agente acabou de escrever, o segredo sai na mensagem de falha, e a
mensagem volta para o modelo como resultado de tool. Nenhuma das duas decisões de policy está
errada isoladamente. O furo é de modelo: **código executado lê o filesystem sem passar por tool
nenhuma**, então nenhuma regra sobre argumento de tool alcança o que ele faz.

Foi achado duas vezes de forma independente na revisão deste branch: pelo portão de revisão,
que rodou o exploit contra a policy real e um sandbox real e leu
`AssertionError: ANTHROPIC_API_KEY=sk-secret` na saída; e pela revisão final, lendo o que a
cópia do workspace excluía e percebendo que só excluía diretório de ruído.

## Decisão

**O que uma regra de deny nomeia nunca entra no container.**

Na cópia do workspace para o sandbox, todo arquivo que a policy **nega explicitamente** é
deixado de fora. O que não está no container não pode ser lido por nada que rode nele, seja
tool, teste, linter ou qualquer processo que o agente consiga iniciar.

Três escolhas dentro dessa decisão:

- **Uma lista só, dirigindo as duas camadas.** O filtro não tem lista própria de padrões de
  segredo. Ele pergunta à policy carregada (`never_readable(policy)`), então a regra
  `never-read-secrets` do YAML é a fonte única. Duas listas divergiriam, e a divergência seria
  silenciosa: um padrão novo no YAML protegeria a leitura direta e não a execução.
- **Deny explícito é diferente de default deny.** Um `.github/ci.yml` é ilegível via
  `read_file` porque ninguém escreveu allow para ele, mas não é segredo: é parte do projeto, e
  a suíte pode precisar dele em disco. Filtrar tudo que cai no default deny entregaria meio
  repositório ao sandbox. `Policy.explicitly_denies` responde só quando uma **regra** de deny
  casou.
- **O sandbox continua sem conhecer policy.** `Sandbox.create` recebe um predicado, e quem o
  constrói é quem chama (worker e demo). A fronteira do §10 fica intacta.

A regra `never-read-secrets` continua existindo, agora como segunda camada: cobre a leitura
direta e é a lista de onde a primeira camada sai.

## Alternativas consideradas

**Aceitar e documentar.** Honesta, mas deixaria uma regra chamada "never" que não é. Num
projeto cuja tese é que o control plane impõe em vez de pedir, não serve.

**Tirar o pytest da allowlist do `run_command`.** Não resolve: `run_tests` continua existindo
e executa o mesmo código. Tirar os dois é tirar a capacidade de verificar o próprio trabalho,
que é metade do que torna um coding agent útil.

**Rodar os testes em outro container, sem os segredos.** É a mesma decisão com um container a
mais. Se o segredo não precisa estar no container de teste, não precisa estar em nenhum.

**Interceptar `open()` dentro do sandbox** (seccomp, LD_PRELOAD, auditoria de syscall).
Frágil, contornável por qualquer binário estático, e muito mais código do que não copiar o
arquivo.

## Consequências

- **Um repo cujo teste precisa de `.env` real quebra dentro do sandbox.** É o comportamento
  correto, e é a mesma posição que o projeto já tinha para credencial: o modelo nunca vê
  segredo, o broker injeta na hora. Configuração de teste tem que vir de valor falso
  versionado, como um `.env.example`.
- **O filtro vale na criação do volume.** Um volume de tarefa que já existe não é recopiado
  (ADR-002 e PR anterior), então mudar a policy no meio de uma tarefa não retira um arquivo que
  já entrou. Aceitável: a policy de uma tarefa é fixada no início, e o `policy_hash` de cada
  decisão registra qual era.
- **O predicado é avaliado como `read_file` para o papel worker.** Uma regra de deny
  condicionada a outro papel não seria vista. Não existe nenhuma hoje; uma que viesse a existir
  pediria decisão própria.
- **Bug latente que esta decisão tornaria fatal, corrigido junto.** `tarfile` adiciona um
  diretório com tudo que há embaixo dele, e essa adição recursiva não passava pelos filtros por
  caminho. Pular `config/.env` não adiantava nada, porque adicionar `config` já o tinha levado
  junto, e um `pkg/node_modules` aninhado entrava no sandbox desde que a lista de ignorados foi
  escrita. Agora é `recursive=False`, com teste.
- **O que isto não cobre:** segredo que aparece *dentro* de arquivo permitido (uma chave colada
  num `settings.py`), e segredo em variável de ambiente do container. O primeiro é problema de
  scanner de segredo no repo alvo (gitleaks já está no CI do plano). O segundo não existe hoje,
  porque o profile do sandbox só define `HOME` e diretórios de cache, e deve continuar assim.
- Vale para entrevista: a pergunta "o agente pode ler X?" tem duas respostas diferentes
  conforme ele possa ou não **executar** código. Policy sobre argumento de tool responde a
  primeira. Só o conteúdo do filesystem responde a segunda.
