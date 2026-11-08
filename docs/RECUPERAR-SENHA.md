# Recuperar acesso ao painel

A senha só é exigida quando `AUTH_REQUIRED=true`. No padrão `false`, o painel abre diretamente e estes passos não são necessários.

Este procedimento mantém suas escolhas de recuperação e limpa somente a senha. Faça isso pelo terminal do servidor e depois reinicie apenas o Sal0 AutoStart:

```bash
docker exec auto-iniciar-containers python -c 'import json,os; p="/config/config.json"; d=json.load(open(p)); d["auth"]=None; d["revision"]+=1; q=p+".reset"; f=open(q,"w"); json.dump(d,f); f.close(); os.chmod(q,0o600); os.replace(q,p)'
docker restart auto-iniciar-containers
```

Reabra `http://IP_DO_SERVIDOR:8788` e crie uma nova senha. Evite alterar opções no painel entre o comando e o reinício, pois o aplicativo carrega a configuração em memória enquanto está executando.
