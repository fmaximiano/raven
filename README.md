# RAVEN — Research and Analysis of Viral and Emerging Niches

Plataforma privada de inteligência editorial (Content Radar) para identificar, investigar e priorizar oportunidades de vídeos **originais** no YouTube, comparando demanda, concorrência e maturidade de assuntos entre mercados linguísticos.

Especificação estratégica: [`docs/Content_Radar_Plano_v2.md`](docs/Content_Radar_Plano_v2.md).

## Estado atual: Fase 0 (viabilidade)

- ✅ Mecanismo completo implementado e testado offline: conectores, políticas de uso, LLM, busca multilíngue, score/confiança, dossiê e benchmark.
- ⏳ **Nenhuma fonte foi testada com credenciais reais ainda.** O critério de aceite da Fase 0 depende disso.
- 📄 Relatório de viabilidade: [`docs/fase0/relatorio-viabilidade.md`](docs/fase0/relatorio-viabilidade.md)
- 📄 Verificação documental das fontes: [`docs/fase0/verificacao-fontes.md`](docs/fase0/verificacao-fontes.md)
- 📄 Decisões de arquitetura: [`docs/arquitetura.md`](docs/arquitetura.md)

## Interface web e deploy

`uv run raven web` sobe a interface em `http://localhost:8000` (exige `RAVEN_WEB_PASSWORD`). Ela lista execuções, mostra dossiês e relatórios, dispara investigações e benchmarks, recebe a rotulagem do benchmark e exibe fontes e custos.

Deploy: imagem Docker portável (`Dockerfile`). Passo a passo para o Railway e para a migração a uma VPS em [`docs/deploy-railway.md`](docs/deploy-railway.md).

## Instalação

Requer Python 3.12+ e [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env   # preencha as credenciais disponíveis (todas opcionais)
```

## Uso

```bash
uv run raven --help
uv run raven sources list                 # fontes, capacidades, políticas e limitações
uv run raven sources check [--live]       # credenciais; --live faz a chamada mais barata de cada fonte
uv run raven sources tools vidiq          # catálogo MCP do vidIQ (sem custo de créditos)

# Prova de ponta a ponta (sem custo, tudo simulado e marcado como tal)
uv run raven investigate --subject-id h01-antikythera --simulate

# Investigação real (exige OpenAI + ao menos uma fonte de busca)
uv run raven investigate --subject-id s07-tardigrades --targets BR,MX,DE,JP,FR
uv run raven investigate --label "Por que o Concorde foi aposentado" --origin-language fr --targets BR,US,JP

# Benchmark de fontes (mesmas consultas em todos os fornecedores)
uv run raven benchmark run --limit 10 --languages en,es,pt,ja --trends
#   → preencha relevance (2/1/0) e language_ok (s/n) em data/runs/<run>/labels.csv
uv run raven benchmark report <run_id>

uv run raven costs                        # gasto real registrado (exclui simulados)
uv run raven data purge [--dry-run]       # expira execuções conforme a retenção de cada fonte
```

Cada execução grava artefatos auditáveis em `data/runs/<run_id>/`: manifesto, evidências com procedência, chamadas de modelo, falhas, custos e dossiê (`dossier.md`).

## Configuração

- `config/raven.yaml`: idiomas, mercados, nichos, pesos do score, rotas de modelos, preços, limites de gasto, cotas por fonte e limiares de similaridade.
- `config/source_policies.yaml`: o que cada fonte permite fazer com cada classe de dado (`allow | deny | pending`; `pending` = negado). Altere somente com evidência registrada.
- `config/benchmark_subjects.yaml`: amostra de 30 assuntos com consultas nativas em 6 idiomas (rascunho a revisar por falantes).

## Estrutura

```
src/raven/
  connectors/   contrato comum + YouTube, DataForSEO, OutlierKit, vidIQ (MCP), simulado
  llm/          Responses API, roteamento, cache, orçamento, schemas, prompts, anti-injection
  pipeline/     investigação, evidências, correspondência, demanda, score, dossiê
  benchmark/    protocolo, rotulagem e relatório
  storage/      ledger de custos/cotas e artefatos de execução
  web/          interface web (FastAPI + Jinja2) e tarefas em segundo plano
  services.py   operações compartilhadas pela CLI e pela web
  policy.py     políticas por fonte com herança de restrições
  cli.py
tests/          unit · contract (formato documentado das APIs) · e2e · web · live (-m live)
```

## Desenvolvimento

```bash
uv run pytest                 # testes offline
uv run pytest -m live         # contra APIs reais (exige credenciais; tem custo)
uv run ruff check src tests && uv run ruff format --check src tests && uv run mypy src
```

## Princípios

- Conteúdo estrangeiro é **sinal de demanda**, não material a copiar.
- Nenhum número, URL, data ou fonte inventados: o LLM cita evidências por id, as URLs vêm dos conectores e o score não imputa valores ausentes.
- Score (atratividade) e confiança (qualidade das evidências) são separados.
- Dados restritos não entram no score, nem indiretamente via LLM.
- Simulado é sempre marcado como simulado.
