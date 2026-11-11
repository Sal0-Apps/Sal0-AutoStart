# Sal0 AutoStart

Painel local para recuperar contêineres que ficaram parados, especialmente quando o HD demora a ser reconhecido depois de reiniciar o servidor. Visual da família **Sal0 Apps**, com os mesmos tons azuis, cartões e navegação do Sal0 Voz e Sal0 Karaokê.

## Instalação no ZimaOS ou Portainer

Importe [`docker-compose.yml`](docker-compose.yml) como aplicativo personalizado ou Stack e abra:

```text
http://IP_DO_SERVIDOR:8788
```

O painel abre diretamente, sem pedir senha. Isso também vale ao atualizar uma instalação que já tinha senha. Para exigir autenticação, defina `AUTH_REQUIRED: "true"`; nesse modo você cria a senha na primeira abertura ou usa a senha já salva. Atualize o mesmo aplicativo/Stack e mantenha o volume `auto-iniciar-containers-config` para conservar suas escolhas e tempos.

O Compose inclui os metadados `x-casaos` para exibir **Sal0 AutoStart**, o ícone do aplicativo e o atalho do painel no ZimaOS/CasaOS. O ícone também está na label `icon` do contêiner. A arte segue o padrão Sal0: fundo azul-marinho, símbolo azul e sem moldura clara. O compose usa `assets/icon-512.png`; `assets/icon.svg` continua atualizado para instalações anteriores. Para renovar um ícone salvo pelo ZimaOS, atualize o campo Ícone nas configurações do aplicativo com a URL PNG do compose.

A imagem é `ghcr.io/sal0-apps/sal0-autostart:latest`. A versão publicada também recebe uma tag fixa igual ao arquivo [`VERSION`](VERSION). O fluxo de publicação constrói a imagem, executa os testes e um teste real com Docker antes do envio ao registro. Plataforma: `linux/amd64`.

Se a porta 8788 já estiver ocupada, altere somente o primeiro número do mapeamento, por exemplo `8789:8788`, e ajuste `x-casaos.port_map` para `"8789"` para manter o atalho correto. Não é necessário abrir portas no roteador para usar pela rede local ou VPN.

## O que você controla

- Recuperação da **stack inteira** ou de um contêiner individual.
- Preferência para itens novos que ainda não têm uma escolha.
- Pausa global e verificação imediata.
- **Iniciar todos**: partida manual dos contêineres parados, incluindo os desmarcados para recuperação automática. Também funciona com o monitor automático pausado.
- **Iniciar** no contêiner ou na stack para um pedido específico.
- Intervalo de verificação, espera inicial, estabilidade do disco e pausa entre partidas.
- Tema claro ou escuro, busca, filtros, estado dos contêineres e atividade do monitor.

As escolhas são salvas automaticamente. Uma escolha individual prevalece sobre a regra da stack. Ao alternar a stack inteira, todos os seus serviços atuais e futuros recebem a mesma regra, substituindo as escolhas individuais dessa stack.

As regras acompanham projeto/serviço/réplica do Compose, ou o nome de um contêiner avulso. Trocar o ID durante uma atualização não apaga sua escolha; renomear projeto, serviço ou contêiner avulso cria uma identidade nova.

**Desativar a recuperação não para o contêiner e não altera sua política `restart` do Docker.** A escolha controla as tentativas automáticas. Um contêiner selecionado que você parar manualmente será iniciado novamente; para manutenção, desative sua recuperação ou pause o monitor. O ciclo funciona em segundo plano, mesmo com o painel fechado e sem sessão de navegador.

Uma partida manual respeita a disponibilidade dos discos e a ordem das dependências, mas dispensa a espera inicial e o tempo de estabilidade. O pedido retenta por até cinco minutos e mostra quantos iniciaram, quantos aguardam e quantos não concluíram. Ele preserva as escolhas de recuperação automática. A partida só é contada como iniciada depois de o Docker confirmar o contêiner em execução; uma aplicação que sai imediatamente mostra o erro/código de saída.

## Nomes, ícones e stacks do ZimaOS

A partir da versão 2.4.0, os cartões mostram o nome personalizado e o ícone configurados no ZimaOS. Stacks com vários serviços começam recolhidas e lembram a expansão neste navegador. A busca aceita o nome do aplicativo, a stack, o serviço, o nome técnico e a imagem; ao encontrar um serviço, abre o grupo com seu contexto. Contêineres avulsos aparecem como aplicativos separados.

A leitura usa `/host/var/lib/casaos/apps`, disponível pela montagem de leitura `/host` já presente no compose. A atualização da imagem funciona com o compose anterior. Para um caminho diferente, a variável opcional `SAL0_ZIMA_APPS` indica a pasta de aplicativos visível dentro do contêiner.

Links absolutos da pasta de aplicativos são resolvidos no host, dentro da montagem `/host`. A consulta de metadados roda em segundo plano: uma pasta lenta não bloqueia a recuperação dos contêineres. Só nomes, ícones, serviços e associação dos aplicativos são aproveitados; variáveis e credenciais dos composes não são enviadas ao navegador. Sem metadados, o painel usa os nomes e ícones das etiquetas Docker e, na falta de imagem, as iniciais.

As identidades das regras e os limites das stacks de recuperação continuam baseados no Compose. Renomear um aplicativo no ZimaOS não apaga suas escolhas. Uma associação visual não une projetos Docker diferentes nem amplia o alcance do botão Iniciar ou da chave de recuperação.

## Reconhecimento de stacks

Stacks Compose, incluindo as criadas pelo Portainer, são agrupadas pelas labels `com.docker.compose.project` e `com.docker.compose.service`. Contêineres sem essas labels aparecem em cartões individuais. Não é necessário informar credenciais do Portainer.

O monitor lê `com.docker.compose.depends_on`, inicia os serviços selecionados na ordem das dependências e aguarda a condição declarada: `service_started`, `service_healthy` ou `service_completed_successfully`. Assim, uma stack como o Immich pode aguardar PostgreSQL e Redis saudáveis antes de iniciar o servidor. Uma dependência parada e desmarcada permanece parada; o painel explica qual serviço está aguardando. Tarefas de inicialização já concluídas com código zero não são repetidas quando usadas por `service_completed_successfully`.

A identificação depende das labels presentes nos contêineres existentes. Sem a label de dependências, o monitor mantém a recuperação individual. Labels antigas que contêm apenas o nome do serviço aguardam saúde quando há healthcheck, ou estado em execução quando não há. A label do Compose não preserva `required: false`; uma dependência listada, mas ausente, é exibida como pendente.

O monitor trabalha com contêineres existentes; não recria stacks apagadas nem executa `docker compose up`. Tarefas temporárias, itens marcados com `autostart.ignore=true` e contêineres de serviços Swarm aparecem protegidos. O próprio Swarm é responsável por seus serviços.

## HD atrasado

O monitor confere os caminhos usados pelo contêiner, os links simbólicos do host e os pontos de montagem usuais do ZimaOS (`/media`, `/DATA`, `/DATA/.media`, `/var/lib/casaos_data/.media`, `/mnt` e `/run/media`). Uma pasta vazia no lugar de um HD nesses caminhos não é tratada como disco montado.

A tabela de montagem visível em `/host` é usada junto à informação do host, pois o PID 1 pode enxergar outro namespace. Remontagens consideram a montagem superior, e um bind da coleção `/media` reconhece os discos montados dentro dela. Cada verificação roda em processo separado com limite de `STORAGE_PROBE_TIMEOUT_SECONDS` (padrão: 5 segundos); um disco ou FUSE travado não congela o ciclo inteiro. Contêineres sem caminhos de armazenamento podem iniciar sem aguardar essa checagem.

Para um disco montado em outro caminho, configure `REQUIRED_MOUNTS` com uma lista JSON de pontos **exatos de montagem**, por exemplo:

```yaml
REQUIRED_MOUNTS: '["/srv/MeuHD"]'
```

Esses pontos adicionais são exigidos para todos os contêineres selecionados. O Docker continua responsável por montar volumes de drivers externos, NFS e CIFS.

O ZimaOS precisa reconhecer e montar o HD. O aplicativo não monta nem repara discos e não reinicia um serviço que já esteja usando uma pasta vazia. Se o armazenamento do próprio Docker estiver indisponível, o Docker precisa voltar a funcionar primeiro. Uma desconexão entre a checagem e a partida não pode ser evitada atomicamente pelo monitor.

Os padrões iniciais são: espera de 10 segundos, ciclo de 15 segundos, estabilidade de 5 segundos e intervalo de 1 segundo entre partidas. As verificações antecipam o próximo ciclo quando precisam confirmar estabilidade ou dependências. O agendamento usa um relógio monotônico, de modo que um acerto de horário no boot não interrompe as tentativas. Depois da primeira partida, os tempos salvos pelo painel prevalecem sobre os valores iniciais do Compose.

## Configuração e diagnóstico

O volume `auto-iniciar-containers-config`, montado em `/config`, guarda senha e preferências. Inclua esse volume em seus backups. Histórico e contadores de tentativas pertencem à execução atual e reiniciam com o monitor.

Em **Detalhes**, veja cada caminho de armazenamento, as dependências, o último erro do Docker, o código de saída e um eventual encerramento por falta de memória. O painel diferencia contêiner ativo, saúde em verificação e falha de saúde; um processo ativo não garante que a aplicação esteja funcionando. Contêineres em execução ou reiniciando continuam sob responsabilidade do Docker: o AutoStart retoma as tentativas quando estiverem parados.

Em **Atividade → Baixar diagnóstico**, exporte o inventário e os resultados das verificações em JSON. O relatório não inclui a configuração de autenticação do painel nem o ambiente dos contêineres. Em aplicativos com vários discos e bibliotecas externas, todos os caminhos precisam estar disponíveis; os caminhos pendentes aparecem individualmente, sem liberar a partida sobre uma pasta vazia.

```bash
docker logs --tail 100 auto-iniciar-containers
```

O painel usa o socket Docker local para consultar e iniciar os contêineres. Use sua rede local ou VPN para acessar. Mesmo com o acesso sem senha, alterações usam cookie HttpOnly/SameSite e token CSRF, renovados automaticamente quando o aplicativo reinicia. No modo opcional com senha, o hash usa PBKDF2. Não publique esse painel diretamente na internet.

Se esquecer a senha, siga a recuperação em [`docs/RECUPERAR-SENHA.md`](docs/RECUPERAR-SENHA.md). O nome exibido no painel pode ser alterado com `SERVER_NAME`; no Compose de exemplo é `VictorServer`.

## Desenvolver e testar

O motor de recuperação usa a biblioteca padrão do Python. PyYAML é instalado na construção da imagem para interpretar os metadados do ZimaOS. Os ícones configurados podem ser carregados pelo navegador; a partida do monitor funciona sem downloads de dependências.

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
docker compose -f docker-compose.yml -f compose.build.yml up -d --build
```

O teste `tests/smoke_docker.py` é destinado a um runner Docker descartável. Ele comprova a partida automática antes da primeira chamada HTTP, recupera uma nova parada e um reinício do monitor sem `/check`, verifica uma instalação antiga com senha, testa partida manual durante pausa e aguarda dependências com healthcheck sem um segundo clique. As partidas manuais são restritas às stacks criadas pelo teste. Os testes unitários incluem disco tardio, FUSE travado, relógio corrigido no boot, namespaces diferentes e preservação das escolhas.

O aplicativo tem limites de 0,5 CPU e 160 MiB de RAM no Compose; esses valores são tetos, não reservas. A descoberta é atualizada a cada dez segundos e o painel lê o inventário em memória.
