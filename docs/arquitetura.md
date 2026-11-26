# RAVEN — decisões de arquitetura

Registro curto das decisões técnicas. Cada item: contexto, decisão, consequência. Revisável.

## ADR-01 — Monólito modular, CLI primeiro

**Contexto.** A Fase 0 precisa provar fontes e metodologia, não servir usuários. A VPS Orion (2 vCPU/8 GB) é compartilhada.
**Decisão.** Pacote Python único (`src/raven`) com módulos separados: `connectors` (fontes), `llm` (IA), `pipeline` (investigação), `benchmark`, `storage`, `policy`. Interface inicial é a CLI (`raven`). API FastAPI, worker e agendador entram nas Fases 1–2, reaproveitando os mesmos módulos.
**Consequência.** Nenhum processo residente na VPS durante a Fase 0; consumo de memória só durante execuções.

## ADR-02 — Persistência em arquivos na Fase 0

**Contexto.** O plano prevê PostgreSQL + pgvector, mas a modelagem definitiva depende do que as fontes realmente entregam.
**Decisão.** Cada execução grava artefatos auditáveis em `data/runs/<run_id>/` (manifesto, evidências JSONL, chamadas de modelo, falhas, custos, dossiê). Consumo e cotas ficam num ledger SQLite (`data/ledger.sqlite3`); cache de LLM em `data/llm_cache.sqlite3`.
**Consequência.** A Fase 1 começa pela migração para PostgreSQL (tabelas `UsageCosts`, `Evidence`, `ResearchRuns`, `ModelExecutions` etc.) com base em dados reais.

## ADR-08 — Hospedagem no Railway com imagem portável

**Contexto.** Decisão do operador (26/11/2026): hospedar no Railway em vez da VPS Orion, mantendo a migração simples.
**Decisão.** `Dockerfile` padrão, sem recursos exclusivos do Railway; configuração só por variáveis de ambiente; dados em volume montado em `/data`. O entrypoint ajusta a posse do volume e executa o processo sem privilégios. A interface web (FastAPI + Jinja2, HTTP Basic + CSRF) roda tarefas em segundo plano, uma por vez, no mesmo processo; a fila durável no PostgreSQL substitui isso na Fase 2.
**Consequência.** Migrar para outro host é rodar a mesma imagem com o mesmo volume e variáveis. O `railway.json` não é usado, porque o *config as code* foi descontinuado pelo Railway. Docker Compose (com PostgreSQL) entra na Fase 1.

## ADR-03 — Política de uso por fonte e classe de dado, com herança

**Contexto.** As políticas do YouTube proíbem métricas derivadas (III.E.4.h) e agregação entre canais de proprietários distintos (III.E.2.a) sem a emenda de *derived metrics*. Os termos dos fornecedores não confirmam armazenamento, uso comercial ou envio a IA.
**Decisão.** `config/source_policies.yaml` define, para cada fonte e classe de dado (ex.: `youtube_data/metadata`, `youtube_data/statistics`, `dataforseo/trends`), os usos `display`, `llm_input`, `scoring_input`, `derived_metric` e `aggregate` como `allow | deny | pending`. `pending` = negado. Toda evidência recebe os usos permitidos na entrada. **Avaliações derivadas (regra ou LLM) herdam a interseção dos usos de suas entradas**: uma avaliação do LLM sobre metadados do YouTube não entra no score.
**Consequência.** O score fica honesto: com a configuração padrão, só os critérios editoriais (30% do peso) são pontuáveis e o sistema exibe um intervalo, não uma nota. Liberar uma fonte exige editar a política e registrar a evidência da permissão.

## ADR-04 — Score com intervalo; confiança separada

Regras formais em `src/raven/pipeline/scoring.py`:
- sub-scores de níveis 1–5; critério sem evidência permitida é **ausente**, nunca imputado;
- intervalo [ausentes = 0, ausentes = 1]; nota pontual só com cobertura ≥ 60% (configurável);
- direitos "bloqueante" eliminam a oportunidade;
- confiança = 0,40·cobertura + 0,25·diversidade de fontes + 0,20·atualidade + 0,15·consistência; dado simulado ⇒ confiança nula.
Limiares de demanda (`pipeline/demand.py`) e de similaridade (`config/raven.yaml > matching`) são **experimentais** e serão calibrados com os rótulos do benchmark.

## ADR-05 — LLM: Responses API, roteamento por tarefa, saídas estruturadas

- `responses.parse(text_format=<Pydantic>)`, `store=False`, **sem ferramentas** em chamadas que recebem conteúdo externo.
- Rotas por tarefa em `config/raven.yaml > llm.routes` (IDs conferidos na página de preços em 26/11/2026; confirmar na conta).
- Cache por (modelo, tarefa, versão do prompt, versão do schema, entrada); prompts versionados em `llm/prompts.py`.
- Limites de gasto por execução/dia/mês verificados **antes** da chamada; custo real a partir de `usage` (inclui tokens em cache e escrita em cache).
- Defesa contra prompt injection: conteúdo externo serializado em blocos `<external_data>` com id aleatório, limpeza de caracteres invisíveis e delimitadores, instrução de sistema explícita, validação por schema, citações apenas por id de evidência existente, URLs do modelo removidas.

## ADR-06 — Conectores com contrato comum e falhas isoladas

`connectors/base.py` define identidade, versão, estado (`real`, `somente_interface`, `simulado`), capacidades, suporte a filtros de idioma/região, classes de dados, unidade de custo e limitações. Erros tipados (`NotConfigured`, `AuthError`, `QuotaExceeded`, `RateLimited`, `UpstreamError`, `CapabilityNotSupported`, `BadResponse`). Retry com backoff só para erros transitórios. Cotas locais aplicadas antes da chamada (YouTube: 100 `search.list`/dia; DataForSEO: US$/dia; OutlierKit/vidIQ: créditos/dia). O pipeline registra falhas por etapa, mercado e fonte e segue adiante.

## ADR-07 — Simulação explícita

Conectores e LLM simulados existem para testes e demonstração. Itens simulados têm `provenance.simulated=True`, ids `SIM-`, política própria, confiança nula e aviso no topo de dossiês e relatórios. O ledger exclui simulados dos totais de gasto.
