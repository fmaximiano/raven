# Deploy no Railway

A imagem é um `Dockerfile` padrão, sem nada específico do Railway. O Railway detecta o `Dockerfile` na raiz e o usa automaticamente. Não há `railway.json`: o "config as code" do Railway foi descontinuado e só vale para serviços antigos até 01/12/2026, então a configuração fica no painel (são poucos itens).

## Passo a passo (uma vez)

1. **Criar o projeto.** Em [railway.com](https://railway.com), clique em *New Project → Deploy from GitHub repo* e escolha `fmaximiano/raven`. Se for o primeiro uso, o Railway pede para instalar o app dele no GitHub, com acesso a esse repositório. O deploy usa a branch `main`.
2. **Definir as variáveis** (aba *Variables* do serviço):
   - `RAVEN_WEB_PASSWORD`: senha forte para entrar na interface. **Obrigatória**: sem ela, toda página responde 503 (exceto `/health`).
   - `RAVEN_WEB_USER`: opcional (padrão `raven`).
   - Chaves das fontes, quando houver (mesmos nomes do `.env.example`): `RAVEN_OPENAI_API_KEY`, `RAVEN_YOUTUBE_API_KEY`, `RAVEN_DATAFORSEO_LOGIN`, `RAVEN_DATAFORSEO_PASSWORD`, `RAVEN_OUTLIERKIT_API_KEY`, `RAVEN_VIDIQ_API_KEY`.
3. **Adicionar um volume** ao serviço com caminho de montagem **`/data`**. É onde ficam execuções, dossiês, custos e cache. Sem volume, tudo se perde a cada deploy. A imagem ajusta sozinha as permissões do volume (o Railway o monta como root), então **não** é preciso definir `RAILWAY_RUN_UID`.
4. **Gerar o endereço público** em *Settings → Networking → Generate Domain*. A aplicação escuta na porta que o Railway informa pela variável `PORT`.
5. **Configurar o health check** em *Settings → Deploy → Healthcheck Path*: `/health`. O Railway só ativa um deploy novo depois que essa rota responder 200. Ela também confirma que o volume aceita gravação.

Depois disso, cada merge na `main` gera um deploy novo. Com volume anexado há alguns segundos de indisponibilidade a cada deploy, porque o Railway não monta o mesmo volume em duas instâncias ao mesmo tempo.

## Uso

Abra o endereço gerado e entre com `RAVEN_WEB_USER` e `RAVEN_WEB_PASSWORD`.

- **Nova execução:** investigação de um assunto ou benchmark de fontes. Sem a chave da OpenAI, só o modo simulado fica disponível.
- **Execuções:** dossiês e relatórios renderizados, com download dos arquivos (evidências, custos, `labels.csv`).
- **Benchmark:** baixe `labels.csv`, preencha no Excel e envie de volta pela própria página. O relatório é recalculado na hora.
- **Fontes:** credenciais configuradas, teste de autenticação real e políticas de uso de cada fonte.
- **Custos:** gasto registrado por fonte.

Uma tarefa por vez. Se o serviço reiniciar no meio de uma execução, ela aparece como "interrompida" e precisa ser disparada de novo.

## Custo esperado

O plano Hobby custa US$ 5/mês e inclui US$ 5 de uso. Medido localmente, o container ocioso usa ~60 MB de memória; pela tabela do Railway (~US$ 10 por GB-mês de memória), o uso deve ficar dentro do incluído. O volume custa ~US$ 0,15 por GB-mês. Os gastos com APIs (OpenAI, DataForSEO etc.) são à parte e ficam registrados na página *Custos*.

## Migrar depois para outra hospedagem (ex.: VPS Orion)

A mesma imagem roda em qualquer host com Docker:

```bash
docker build -t raven .
docker run -d --name raven --restart unless-stopped -p 8000:8000 --env-file .env -v /srv/raven/data:/data raven
```

Depois, basta colocar um proxy HTTPS (Caddy) na frente e apontar o domínio. Quanto aos dados: execuções de fontes externas expiram em 30 dias por política, então começar do zero na nova hospedagem é aceitável na Fase 0. A partir da Fase 1, os dados vão para o PostgreSQL e a migração vira um `pg_dump`/`pg_restore`.

## Segurança

- Acesso por usuário e senha (HTTP Basic sobre HTTPS do Railway). Formulários protegidos contra CSRF.
- O conteúdo externo dos dossiês é exibido sem HTML bruto, então títulos de vídeos não executam código.
- Segredos só nas variáveis do Railway, nunca no repositório. A página *Fontes* mostra se a credencial existe, nunca o valor.
- Páginas marcadas como `noindex`.
