# ADR-004: sandbox sem rede, e o gateway como única saída

**Status:** aceita · **Data:** 2026-09-20 · **PR:** `feat/sandbox`

## Contexto

A frase do briefing §21 que esta ADR existe para honrar: **container não é sandbox seguro
por padrão**. Kernel compartilhado com o host, capabilities default, rede habilitada, root
dentro. Sandbox é o conjunto de restrições que você aplica **e testa**.

O agente executa código e comandos que o modelo escolheu. A premissa do projeto é que o
modelo será comprometido em algum momento, então a pergunta não é "o agente vai tentar
exfiltrar?", é "quando tentar, o que existe no caminho?".

## Decisão

**O sandbox não tem rede. `network_mode=none`, sem exceção configurável.**

Toda ação externa (abrir PR, buscar URL, chamar API) é uma **tool do gateway**, executada
pelo control plane, sujeita ao policy engine, com credencial emitida pelo broker.

Isso troca um problema difícil por um fácil. Controle de egresso com proxy e allowlist de
domínio exige inspecionar tráfego, lidar com TLS, DNS e resolução dinâmica, e ainda assim
erra. Com `--network none` a única saída é código que você escreveu, e a superfície de
egresso vira uma lista de funções em vez de uma lista de domínios.

**As restrições aplicadas, cada uma com teste em `tests/test_sandbox.py`:**

| Restrição | O que impede |
|---|---|
| `network_mode=none` | exfiltração direta, download de payload |
| `user=10001` | escrita como root, uid que não existe no host nem na imagem |
| `read_only=True` no rootfs | modificar binário ou config da imagem |
| `cap_drop=ALL` | chown, mount, raw socket, ptrace |
| `no-new-privileges` | escalar via binário setuid |
| `pids_limit`, `mem_limit`, `cpus` | derrubar o host por consumo |
| socket do Docker não montado | root no host, que é o escape completo |

## Alternativas consideradas

**Proxy de egresso com allowlist de domínio.** Recusada por ora, e está no §54 como
"próximo passo de hardening". Mais superfície, mais código, e não fecha DNS exfiltration.

**Bind mount do workspace em vez de cópia.** Recusada. No Windows a tradução de caminho e a
semântica de permissão do bind mount são imprecisas, e um mount dá ao container um caminho
de volta para o disco do host. A cópia torna o sistema de arquivos do host irrelevante.

**gVisor ou Firecracker.** Fora do escopo do MVP (§54), documentado como próximo passo. O
que eles resolvem é o kernel compartilhado, que continua sendo o risco residual conhecido
desta decisão.

## Duas descobertas que mudaram a implementação

Ambas verificadas contra o daemon, não deduzidas da documentação, e ambas mudariam o
resultado se eu tivesse confiado na leitura:

1. **`put_archive` recusa em container com rootfs `read_only`.** A verificação do Docker
   olha a flag do rootfs, não o mount de destino. Erro 400 explícito.
2. **Com tmpfs no destino e rootfs gravável, `put_archive` retorna sucesso e o conteúdo
   desaparece.** Ele grava na camada do rootfs, e o tmpfs monta por cima escondendo. Falha
   silenciosa, que é pior que o erro.

Daí o desenho final: volume nomeado montado em `/sandbox`, com o workspace num
subdiretório criado pelo próprio tar. O subdiretório não é enfeite: a raiz de um volume
sempre monta como root com modo 755, e o usuário do sandbox não consegue criar arquivo
nela. O tar cria `workspace/` já com o dono certo.

## Consequências

- Nenhuma tool do agente alcança a rede. Semana 9, quando o MCP gateway existir, a saída
  continua sendo só ele.
- O volume precisa ser removido no `destroy()`, senão vaza e sobrevive à tarefa.
- **Risco residual aceito:** kernel compartilhado. Um escape via vulnerabilidade de kernel
  não é coberto por nenhuma flag acima. gVisor é a mitigação conhecida e está no roadmap.
- **Risco a decidir depois:** quem cria containers precisa de acesso ao socket do Docker, o
  que equivale a root no host. Hoje quem faz isso é o processo de teste. Quando o worker
  assumir, isso vira decisão própria, com ADR-012.
