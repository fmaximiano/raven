# Fase 0 — Relatório de viabilidade (incremento 1)

**Data:** 26/11/2026 · **Status:** fundação pronta e testada offline; **critério de aceite da Fase 0 ainda não atingido**: nenhuma fonte foi testada com credenciais reais.

## 1. Resumo

O mecanismo de pesquisa está implementado de ponta a ponta: conectores, política de uso por fonte, LLM com orçamento, correspondência multilíngue, score/confiança, dossiê e protocolo de benchmark. Ele roda hoje em modo simulado e passa em 65 testes automatizados. O que falta para decidir a Fase 1 depende de você: credenciais e contratações de teste.

A verificação documental trouxe três achados que **mudam o plano**:

1. **O YouTube Data API não pode alimentar o score** sem a emenda de *derived metrics*. As políticas proíbem métricas derivadas e agregação entre canais de terceiros, e a emenda exige auditoria com caso de uso de analytics. Sem ela, a API serve para encontrar e exibir equivalentes, não para pontuar. A solicitação de auditoria (checklist, item 7) vira caminho crítico.
2. **O OutlierKit não filtra idioma nem país** em nenhum endpoint. Para pesquisa entre mercados, ele depende só do texto da consulta. Isso reduz o valor esperado dele para o RAVEN e precisa ser medido no benchmark antes de qualquer assinatura longa.
3. **O vidIQ só oferece MCP**, sem parâmetros documentados. O uso a partir de um backend é plausível (chave somente-leitura), mas não confirmado.

Ponto positivo: o **Trends do DataForSEO aceita `type: youtube`** (interesse de busca no próprio YouTube, por país e idioma) a US$ 0,011 por consulta. É o melhor sinal de demanda licenciável identificado, e barato.

## 2. Estado de cada parte

| Componente | Estado | Evidência |
|---|---|---|
| Contrato comum de conectores, erros tipados, retry, rate limit, cotas locais | Implementado e testado | `connectors/base.py`, `http.py`; testes de contrato |
| YouTube Data API (busca + detalhes, cota de 100 buscas/dia) | Implementado; **não testado com credencial** | 6 testes de contrato com respostas no formato documentado |
| DataForSEO (Trends YouTube + SERP YouTube) | Implementado; **não testado com credencial** | 5 testes de contrato |
| OutlierKit (outliers/search, refresh, vídeos) | Implementado; **não testado com credencial** | 3 testes de contrato |
| vidIQ (MCP) | **Somente interface**: descoberta de ferramentas pronta; mapeamento depende do catálogo real | 2 testes |
| Google Trends API alpha · YouTube Analytics | Não implementados (acesso não concedido · Fase 4) | — |
| Política de uso por fonte com herança (anti-"lavagem" via LLM) | Implementado e testado | `policy.py`, `pipeline/evidence.py`; 7 testes |
| LLM (Responses API, roteamento, cache, orçamento, custos) | Implementado; **não testado com credencial** | SDK conferido; 4 testes com SDK falso |
| Proteção contra prompt injection | Implementado e testado | `llm/safety.py`; validação de citações e URLs |
| Score com intervalo + confiança + filtro de direitos | Implementado e testado | `pipeline/scoring.py`; 11 testes |
| Investigação ponta a ponta + dossiê | Implementado; executado **apenas simulado** | `raven investigate --simulate`; 5 testes e2e |
| Protocolo de benchmark (30 assuntos × 6 idiomas) + rotulagem humana + relatório | Implementado; executado **apenas simulado** | `raven benchmark run --simulate`; 1 teste e2e |
| Retenção/expiração por fonte (execuções e cache de LLM) | Implementado e testado | `raven data purge` |
| Interface web (execuções, dossiês, disparo de tarefas, rotulagem, fontes, custos) | Implementada e testada | `raven web`; 11 testes |
| Imagem Docker para o Railway | Construída e executada localmente (volume, permissões, health check, fluxo completo) | `Dockerfile`, `docs/deploy-railway.md` |
| Docker Compose, PostgreSQL | Adiados para a Fase 1 | — |

Qualidade: `ruff` e `mypy --strict` sem erros. `pytest`: **65 aprovados**. Os 5 testes `live` foram pulados por falta de credenciais.

## 3. O que a configuração padrão produz hoje

Com as políticas atuais (conservadoras), uma investigação real pontuaria só os critérios editoriais avaliados pelo LLM a partir da descrição do assunto: potencial econômico, portabilidade cultural e viabilidade (**30% do peso**). O dossiê mostraria um **intervalo** de score, não uma nota, e listaria por que cada critério ficou de fora. Esse comportamento é intencional e está coberto por teste (`test_default_policies_keep_restricted_data_out_of_score`).

Caminhos para chegar à cobertura mínima de 60%:

| Ação | Critérios liberados | Cobertura resultante |
|---|---|---|
| Confirmação do DataForSEO (uso comercial + IA) para Trends | Demanda no destino + momento | 60% |
| + confirmação do OutlierKit ou vidIQ | Sucesso na origem | 75% |
| + emenda do YouTube **ou** avaliação humana da lacuna de oferta | Lacuna de oferta | 100% |

A avaliação humana da lacuna de oferta já é suportada pelo motor de score (avaliação humana prevalece). Falta o comando para registrá-la, previsto para o próximo incremento.

## 4. Custos

**Observados:** US$ 0,00. Nenhuma chamada paga foi feita.

**Estimados** (preços de 26/11/2026; câmbio de planejamento R$ 6):

| Item | Volume | Estimativa |
|---|---|---|
| Investigação completa (5 mercados, ~15 chamadas `gpt-6.2-sol`) | ~47 mil tokens de entrada / ~17 mil de saída | ~US$ 0,26 por investigação (a medir) |
| Benchmark completo por fornecedor | 30 assuntos × 6 idiomas = 180 consultas | — |
| ↳ YouTube | 180 `search.list` + ~180 unidades | US$ 0, mas **2 dias** de cota |
| ↳ DataForSEO SERP + Trends | 180 × US$ 0,002 + 180 × US$ 0,011 | ~US$ 2,34 |
| ↳ OutlierKit | 180 × 2 créditos = 360 | exige plano Pro (500 créditos, US$ 99/mês) |
| ↳ vidIQ | 180 × 5 créditos = 900 | exige MAX (6.000) ou Boost (2.000) |

**Proposta em duas etapas, dentro do teto de R$ 1.000:**

- **Etapa A: validar acesso e contrato (~US$ 70 ≈ R$ 420).** Chave do YouTube (grátis), depósito mínimo do DataForSEO (US$ 50; o consumo previsto é ~US$ 3 e o saldo não expira), OpenAI com US$ 20 e vidIQ Free (150 créditos = 30 pesquisas). Com isso rodam os testes `live`, o benchmark reduzido e 5–10 investigações reais. **Atualização (26/11):** o teste gratuito do OutlierKit não inclui API, então ele fica fora da Etapa A; o vidIQ passa a ser a única fonte especializada testável sem custo.
- **Etapa B: benchmark completo de um único fornecedor especializado**, escolhido pela Etapa A: OutlierKit Pro (US$ 99 ≈ R$ 594) **ou** vidIQ MAX mensal (US$ 49 ≈ R$ 294, se o mensal estiver disponível no checkout). Assinar os dois ao mesmo tempo ultrapassa o teto, e o plano exige realocação explícita nesse caso.

## 5. Configuração de fontes recomendada para o MVP (provisória)

Esta recomendação vale **até os dados reais do benchmark** chegarem:

1. **Demanda:** DataForSEO Trends `type: youtube`, mediante confirmação escrita de uso comercial e envio a LLM.
2. **Busca de equivalentes:** YouTube Data API para localizar e exibir, sem pontuar até a emenda. O DataForSEO SERP serve de alternativa para contornar o limite de 100 buscas/dia, mas só depois de avaliar o risco jurídico de dados de SERP coletados por terceiros.
3. **Sinal de origem:** o fornecedor especializado que vencer o benchmark. A documentação não basta para escolher: o OutlierKit tem API REST documentada, mas sem filtros de idioma ou país; o vidIQ tem crédito mais barato, mas parâmetros e uso a partir de backend não documentados.
4. **LLM:** `gpt-6.2-sol` para extração, concorrência e síntese; `gpt-6.1-terra` para classificação; `gpt-6.2-astra` reservado para pesquisa profunda e auditoria.

## 6. Decisões e ações que dependem de você

1. **Credenciais da Etapa A** no `.env` (modelo em `.env.example`).
2. **Pedido de auditoria do YouTube** com caso de uso de analytics (emenda de *derived metrics*). Sem ele, a lacuna de oferta depende de avaliação humana ou de fornecedor licenciado.
3. **Perguntas por escrito aos fornecedores** (checklist, item 12): uso comercial, armazenamento, retenção e envio a LLM. Para o OutlierKit, confirmar também o acesso à API no Hobby e o reembolso. Registrar as respostas em `config/source_policies.yaml > evidence_of_permission`.
4. **Revisão das consultas nativas** em `config/benchmark_subjects.yaml`, principalmente JA, DE e FR, por falante competente antes do teste pago.
5. **Rotulagem humana** do `labels.csv` após o benchmark: relevância 2/1/0 e idioma correto s/n. Sem ela não há métrica de qualidade.

## 7. Como executar a Etapa A

```bash
cp .env.example .env    # preencher as chaves disponíveis
uv sync
uv run raven sources check --live            # autenticação real, custo mínimo
uv run pytest -m live tests/live -v          # contratos contra as APIs reais
uv run raven sources tools vidiq             # catálogo MCP real (sem créditos)
uv run raven benchmark run --limit 10 --languages en,es,pt,ja --providers youtube_data,dataforseo,outlierkit,vidiq --trends
uv run raven investigate --subject-id h01-antikythera --targets BR,MX,DE,JP,FR
uv run raven costs
```

## 8. Próxima entrega recomendada

**Incremento 2 da Fase 0, logo após a Etapa A:**

- ajustar os conectores ao comportamento real observado;
- mapear as ferramentas do vidIQ;
- calibrar os limiares de similaridade com os rótulos;
- adicionar o comando de avaliação humana por critério;
- reescrever este relatório com números reais: cobertura, precisão, exclusivos, falsos positivos e custo por consulta e por dossiê.

A recomendação de avançar ou não para a Fase 1 sai desse relatório.
