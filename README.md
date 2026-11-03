# Sal0 AutoStart

Painel local para recuperar contêineres que ficaram parados, especialmente quando o HD demora a ser reconhecido depois de reiniciar o servidor. Visual da família **Sal0 Apps**, com os mesmos tons azuis, cartões e navegação do Sal0 Voz e Sal0 Karaokê.

## Instalação no ZimaOS ou Portainer

Importe [`docker-compose.yml`](docker-compose.yml) como aplicativo personalizado ou Stack e abra:

```text
http://IP_DO_SERVIDOR:8788
```

Crie uma senha na primeira abertura. Se você já usa o AutoStart anterior, atualize o mesmo aplicativo/Stack com este Compose. O nome do contêiner e o volume `auto-iniciar-containers-config` foram preservados: mantenha esse volume para conservar senha, escolhas e tempos.

A imagem é `ghcr.io/sal0-apps/sal0-autostart:latest`. A versão publicada também recebe uma tag fixa igual ao arquivo [`VERSION`](VERSION). O fluxo de publicação constrói a imagem, executa os testes e um teste real com Docker antes do envio ao registro. Plataforma: `linux/amd64`.

Se a porta 8788 já estiver ocupada, altere somente o primeiro número do mapeamento, por exemplo `8789:8788`. Não é necessário abrir portas no roteador para usar pela rede local ou VPN.

## O que você controla

- Recuperação da **stack inteira** ou de um contêiner individual.
- Preferência para itens novos que ainda não têm uma escolha.
- Pausa global e verificação imediata.
- Intervalo de verificação, espera inicial, estabilidade do disco e pausa entre partidas.
- Tema claro ou escuro, busca, filtros, estado dos contêineres e atividade do monitor.

As escolhas são salvas automaticamente. Uma escolha individual prevalece sobre a regra da stack. Ao alternar a stack inteira, todos os seus serviços atuais e futuros recebem a mesma regra, substituindo as escolhas individuais dessa stack.

As regras acompanham projeto/serviço/réplica do Compose, ou o nome de um contêiner avulso. Trocar o ID durante uma atualização não apaga sua escolha; renomear projeto, serviço ou contêiner avulso cria uma identidade nova.

**Desativar a recuperação não para o contêiner e não altera sua política `restart` do Docker.** A escolha controla apenas as tentativas deste monitor. Um contêiner selecionado que você parar manualmente será iniciado novamente; para manutenção, desative sua recuperação ou pause o monitor.

## Reconhecimento de stacks

Stacks Compose, incluindo as criadas pelo Portainer, são agrupadas pelas labels `com.docker.compose.project` e `com.docker.compose.service`. Contêineres sem essas labels aparecem em **Sem stack**. Não é necessário informar credenciais do Portainer.

O monitor trabalha com contêineres existentes; não recria stacks apagadas nem executa `docker compose up`. Tarefas temporárias, itens marcados com `autostart.ignore=true` e contêineres de serviços Swarm aparecem protegidos. O próprio Swarm é responsável por seus serviços.

## HD atrasado

O monitor confere os caminhos usados pelo contêiner, os links simbólicos do host e os pontos de montagem usuais do ZimaOS (`/media`, `/DATA`, `/DATA/.media`, `/var/lib/casaos_data/.media`, `/mnt` e `/run/media`). Uma pasta vazia no lugar de um HD nesses caminhos não é tratada como disco montado.

Para um disco montado em outro caminho, configure `REQUIRED_MOUNTS` com uma lista JSON de pontos **exatos de montagem**, por exemplo:

```yaml
REQUIRED_MOUNTS: '["/srv/MeuHD"]'
```

Esses pontos adicionais são exigidos para todos os contêineres selecionados. O Docker continua responsável por montar volumes de drivers externos, NFS e CIFS.

O ZimaOS precisa reconhecer e montar o HD. O aplicativo não monta nem repara discos e não reinicia um serviço que já esteja usando uma pasta vazia. Se o armazenamento do próprio Docker estiver indisponível, o Docker precisa voltar a funcionar primeiro. Uma desconexão entre a checagem e a partida não pode ser evitada atomicamente pelo monitor.

Os padrões iniciais são: espera de 45 segundos, ciclo de 60 segundos, estabilidade de 20 segundos e intervalo de 3 segundos entre partidas. A estabilidade é confirmada nos ciclos, podendo acrescentar um ciclo completo à espera. Depois da primeira partida, os tempos salvos pelo painel prevalecem sobre os valores iniciais do Compose.

## Configuração e diagnóstico

O volume `auto-iniciar-containers-config`, montado em `/config`, guarda senha e preferências. Inclua esse volume em seus backups. Histórico e contadores de tentativas pertencem à execução atual e reiniciam com o monitor.

```bash
docker logs --tail 100 auto-iniciar-containers
```

O painel usa senha com hash PBKDF2 e sessão com cookie HttpOnly/SameSite; alterações exigem uma sessão e um token de proteção. Ele usa o socket Docker local para consultar e iniciar os contêineres. Use sua rede local ou VPN para acessar. Não publique esse painel diretamente na internet.

Se esquecer a senha, siga a recuperação em [`docs/RECUPERAR-SENHA.md`](docs/RECUPERAR-SENHA.md). O nome exibido no painel pode ser alterado com `SERVER_NAME`; no Compose de exemplo é `VictorServer`.

## Desenvolver e testar

O aplicativo usa somente a biblioteca padrão do Python, sem pip, CDN, fonte externa ou download durante a partida.

```bash
python -m unittest discover -s tests -v
docker compose -f docker-compose.yml -f compose.build.yml up -d --build
```

O teste `tests/smoke_docker.py` é destinado a um runner Docker descartável: ele cria contêineres e um volume próprios, restringe a recuperação aos seus itens de teste, confirma seleção/persistência e remove somente esses recursos de teste.

O aplicativo tem limites de 0,5 CPU e 160 MiB de RAM no Compose; esses valores são tetos, não reservas. A descoberta é atualizada a cada dez segundos e o painel lê o inventário em memória.
