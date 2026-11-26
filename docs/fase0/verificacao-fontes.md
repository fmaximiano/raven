# Fase 0 — Verificação documental das fontes

**Data da consulta:** 26/11/2026. **Método:** leitura das páginas oficiais listadas em cada item. Nenhuma chamada autenticada foi feita: não havia credenciais no ambiente. Tudo abaixo é o que a **documentação** afirma, não o que foi observado.

Legenda: ✅ documentado · ⚠️ ambíguo/contraditório · ❌ não documentado.

## YouTube Data API v3

Fontes: [developer policies](https://developers.google.com/youtube/terms/developer-policies), [derived metrics policy](https://developers.google.com/youtube/terms/derived-metrics-policy), [quota](https://developers.google.com/youtube/v3/determine_quota_cost), [search.list](https://developers.google.com/youtube/v3/docs/search/list).

- ✅ Cota padrão: **100 chamadas `search.list`/dia** (bucket próprio) e 10.000 unidades/dia para os demais endpoints (1 unidade por chamada). Reinicia à meia-noite do Pacífico.
- ✅ `search.list` aceita `relevanceLanguage` (preferência: "resultados em outros idiomas ainda podem aparecer") e `regionCode` (vídeos **visíveis** no país, não público do país). Estatísticas exigem `videos.list`.
- ✅ Dados obtidos com chave de API são *Non-Authorized Data*: **estatísticas no máximo 30 dias**; demais dados devem ser excluídos ou atualizados após 30 dias (III.E.4).
- ✅ **Proibido criar dados ou métricas derivadas** a partir de dados da API (III.E.4.h); o exemplo da política é justamente "um score que considere likes, views ou outros dados da API".
- ✅ **Proibido agregar dados** exceto de canais do mesmo proprietário (III.E.2.a) e usar dados para obter insights sobre o próprio YouTube (III.E.2.b). Uma matriz "assunto × mercado" construída com dados da API se aproxima dessa restrição.
- ✅ Exceção: a emenda de *derived metrics* permite métricas como razões e scores próprios (e retenção de estatísticas por até 36 meses) **somente** para clientes auditados com caso de uso de *analytics* no YouTube, mediante o formulário de auditoria de conformidade (seção "Analytics & Reporting").
- ❌ Não encontrei regra específica sobre enviar dados da API a modelos de IA. Por prudência, o RAVEN envia apenas metadados públicos (título, canal, data) e nunca estatísticas.

**Implicação:** sem a emenda, o YouTube Data API serve para **localizar e exibir** equivalentes, não para pontuar. O pedido de auditoria (checklist, item 7) passa a ser crítico, não opcional.

## DataForSEO

Fontes: [Trends Explore Live](https://docs.dataforseo.com/v3/keywords_data/google_trends/explore/live/), [YouTube Organic Live](https://docs.dataforseo.com/v3/serp/youtube/organic/live/advanced/), [preços](https://dataforseo.com/pricing), [termos](https://dataforseo.com/terms-of-service/).

- ✅ HTTP Basic; corpo é array de tarefas; Live = 1 tarefa por chamada; campo `cost` em USD na resposta.
- ✅ Trends Explore aceita `type: youtube` (interesse de busca **no YouTube**), `location_code`, `language_code`, até 5 keywords, `time_range`. Valores 0–100 relativos à requisição; 0 = dados insuficientes.
- ✅ Preço: Trends Live **US$ 0,011/tarefa** (Standard US$ 0,0027, até 45 min). SERP YouTube Live **US$ 0,002 por bloco de 20 resultados**.
- ✅ Depósito mínimo **US$ 50**; crédito de teste de US$ 1; saldo não expira; até 250 tarefas Trends Live/min.
- ⚠️ Termos 7.1: dados de SERP não podem ser usados "para competir com ou prejudicar" os buscadores. Não há cláusula explícita de armazenamento ou de envio a IA. A SERP do YouTube é coleta de terceiros sobre o YouTube: risco jurídico a avaliar.
- ⚠️ Mapeamento de códigos de erro por faixa (401xx auth, 402xx pagamento) a confirmar no [apêndice de erros](https://docs.dataforseo.com/v3/appendix/errors). O teste de credencial usa `GET /v3/appendix/user_data` (sem custo de tarefa), também a confirmar.

## OutlierKit

Fontes: [API docs](https://outlierkit.com/api-docs), [planos](https://outlierkit.com/resources/choose-your-plan/), [termos](https://outlierkit.com/terms).

- ✅ REST `https://outlierkit.com/api/v1`, `Authorization: Bearer`. `POST /outliers/search` (2 créditos, índice em cache), `POST /outliers/refresh` (10 créditos, ao vivo, plano Pro+), canais, vídeos, transcrições, comentários e keywords.
- ✅ Métricas de outlier por vídeo: `outlierMultiplier`, `channelOutlierRatio`, `viewsVsChannelAvg` etc.
- ❌ **Nenhum filtro de idioma, país ou data** em qualquer endpoint. Para o RAVEN, isso significa que o idioma depende só do texto da consulta e que a pesquisa multilíngue fica mais fraca nessa fonte.
- ⚠️ Acesso à API no plano Hobby é contraditório entre páginas. Pro: US$ 99/mês, 500 créditos. Teste gratuito: 30 créditos (duração "7 dias" nos termos). Política de reembolso contraditória.
- ✅ **Observado pelo operador em 26/11/2026:** o plano gratuito **não dá acesso à API** (não é possível gerar chave). Testar o OutlierKit exige assinatura paga.
- ⚠️ Termos remetem às regras das plataformas de origem; nada sobre armazenamento, uso comercial ou envio a IA.

## vidIQ

Fontes: [MCP](https://vidiq.com/mcp/), [artigo de suporte](https://support.vidiq.com/en/articles/15082430-vidiq-mcp), [planos](https://vidiq.com/plans/).

- ✅ Servidor MCP remoto `https://mcp.vidiq.com/mcp`; OAuth ou chave **somente-leitura**. MCP disponível em todos os planos, inclusive o Free (150 créditos/mês). Pesquisa (keywords, outliers, estatísticas) custa 5 créditos/chamada. MAX: 6.000 créditos/mês (US$ 39/mês no anual).
- ❌ Não há REST documentado. Cabeçalho da chave, transporte exato, parâmetros das ferramentas, filtros de idioma/país e formato de resposta não estão documentados. O conector está como **somente interface** até `raven sources tools vidiq` revelar o catálogo real.
- ❌ Termos de armazenamento, uso comercial e automação não verificados (termos gerais não lidos).

## OpenAI

Fontes: [preços](https://developers.openai.com/api/docs/pricing), [modelos](https://developers.openai.com/api/docs/models/compare), [structured outputs](https://developers.openai.com/api/docs/guides/structured-outputs).

- ✅ Os IDs do plano existem: `gpt-6.1-terra` (US$ 1/5 por 1M entrada/saída), `gpt-6.2-sol` (2/10), `gpt-6.2-astra` (8/40). Não existe "6.2 Terra". `text-embedding-3-large`: US$ 0,13/1M.
- ✅ Responses API com `text.format` `json_schema` estrito; SDK Python `responses.parse(text_format=...)` (conferido no SDK 3.59 instalado, inclusive `usage.input_tokens_details.cached_tokens` e `cache_write_tokens`).
- ✅ Batch: 50% de desconto, janela de 24 h (útil para reprocessamentos, não para investigação interativa).

## Google Trends API (alpha) e YouTube Analytics API

- Trends API: acesso por solicitação; sem conector até haver concessão. O DataForSEO cobre o caso.
- Analytics API: Fase 4 (canais próprios via OAuth). Não antecipada.
