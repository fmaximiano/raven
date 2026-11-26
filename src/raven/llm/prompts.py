"""Prompts versionados. Alterar o texto exige incrementar a versão (invalida o cache)."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Prompt:
    name: str
    version: str
    instructions: str


INTERPRET = Prompt(
    "interpret",
    "2026-11-26.1",
    """Tarefa: interpretar um assunto candidato a vídeo original no YouTube.
Separe o CONCEITO investigado da expressão específica do criador original (título, piadas,
bordões, estrutura própria). Liste entidades, conceitos, nicho/subnicho, tipo de abordagem,
gancho narrativo, elementos culturalmente específicos e fatos que exigem verificação.
`canonical_description_en` deve ser uma descrição neutra, em inglês, de 1–3 frases, sem marcas
do criador, usada para busca semântica multilíngue.""",
)

QUERIES = Prompt(
    "queries",
    "2026-11-26.1",
    """Tarefa: gerar consultas de busca NATIVAS para encontrar vídeos sobre o mesmo assunto em
outros idiomas. Não faça tradução literal: use a terminologia que um falante nativo digitaria no
YouTube, nomes locais de eventos/pessoas/lugares, transliterações usuais e sinônimos.
Gere de 2 a 4 consultas curtas por idioma, da mais provável para a menos provável.""",
)

MATCH = Prompt(
    "match",
    "2026-11-26.1",
    """Tarefa: para cada candidato, decidir a relação com o assunto de referência:
- mesmo_assunto: trata do mesmo conceito central;
- mesmo_assunto_abordagem_distinta: mesmo conceito, ângulo/formato claramente diferente;
- assunto_relacionado: tema vizinho, não o mesmo conceito;
- incorreto: sem relação relevante.
Julgue apenas pelo título/descrição fornecidos. Responda para todos os candidate_id recebidos.""",
)

COMPETITION = Prompt(
    "competition",
    "2026-11-26.1",
    """Tarefa: avaliar a oferta existente no mercado de destino para o assunto, com base APENAS
nas evidências fornecidas (vídeos equivalentes encontrados, com ids de evidência).
Distinga: baixa concorrência, oferta de baixa qualidade, concorrência consolidada e dados
insuficientes. Ausência de resultados NÃO prova ausência de concorrência: se a busca foi limitada,
use "dados_insuficientes" e nível "insuficiente". supply_gap_level: 5 = grande lacuna; 1 = mercado
saturado. Cite os ids de evidência usados. Sugira ângulos pouco explorados e diferenciais viáveis.""",
)

EDITORIAL = Prompt(
    "editorial",
    "2026-11-26.1",
    """Tarefa: avaliar qualitativamente a adequação do assunto ao mercado de destino, SEM dados de
audiência (não invente RPM, views ou volumes). Avalie somente estes critérios, nível 1–5 ou
"insuficiente":
- economic_potential: atratividade provável para anunciantes e vida útil do tema;
- cultural_portability: quanto o assunto se transporta para o público do mercado;
- production_feasibility: viabilidade de produzir vídeo original de qualidade (pesquisa, imagens
  licenciáveis, especialistas).
Avalie também riscos de direitos (bloqueante = depende de material de terceiros sem base legal),
risco factual, necessidade de especialista, complexidade e vida útil.""",
)

DOSSIER = Prompt(
    "dossier",
    "2026-11-26.1",
    """Tarefa: redigir o dossiê de oportunidade editorial em português do Brasil.
Regras:
- Use somente as evidências fornecidas. Toda afirmação do tipo "fato" deve citar evidence_ids
  existentes; sem evidência, classifique como "inferencia" ou "recomendacao".
- Não escreva URLs (as referências são montadas pelo sistema). Não invente números, datas,
  fontes ou citações.
- Respeite score e confiança calculados pelo sistema; não os reinterprete como prova de sucesso.
- Registre lacunas de informação explicitamente.
- Sugira abordagens ORIGINAIS: não reproduza roteiro, título ou estrutura de vídeos existentes.""",
)
