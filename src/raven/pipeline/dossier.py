"""Validação do rascunho do LLM e renderização determinística do dossiê.

Garantias:
- citações a ids inexistentes são removidas e registradas como aviso;
- "fato" sem evidência válida é rebaixado a "inferencia";
- URLs escritas pelo modelo são removidas (referências vêm apenas das evidências coletadas);
- score, confiança e referências são montados pelo sistema, não pelo modelo.
"""

from __future__ import annotations

import re
from typing import TYPE_CHECKING, Any

from raven.domain import Evidence, Use
from raven.llm.schemas import DossierDraft, Statement
from raven.pipeline.scoring import CRITERIA_LABELS

if TYPE_CHECKING:
    from raven.pipeline.investigate import InvestigationResult, MarketResult

_URL = re.compile(r"https?://\S+|www\.\S+", re.I)


def _strip_urls(text: str, warnings: list[str], where: str) -> str:
    if _URL.search(text):
        warnings.append(f"URL escrita pelo modelo removida em {where}")
        return _URL.sub("[link removido]", text)
    return text


def _fix_statements(
    items: list[Statement], known: set[str], warnings: list[str], where: str
) -> list[Statement]:
    out = []
    for st in items:
        valid = [i for i in st.evidence_ids if i in known]
        invalid = [i for i in st.evidence_ids if i not in known]
        if invalid:
            warnings.append(f"{where}: ids de evidência inexistentes removidos {invalid}")
        kind = st.kind
        if kind == "fato" and not valid:
            warnings.append(f"{where}: 'fato' sem evidência válida rebaixado a inferência")
            kind = "inferencia"
        out.append(Statement(text=_strip_urls(st.text, warnings, where), kind=kind, evidence_ids=valid))
    return out


def sanitize_draft(draft: DossierDraft, known_ids: set[str]) -> tuple[DossierDraft, list[str]]:
    warnings: list[str] = []
    data: dict[str, Any] = draft.model_dump()
    for field in ("why_now", "demand_evidence", "competition_evidence", "potential_audience"):
        data[field] = [
            s.model_dump() for s in _fix_statements(getattr(draft, field), known_ids, warnings, field)
        ]
    for field in ("provisional_title", "summary", "resources_needed", "final_opinion"):
        data[field] = _strip_urls(data[field], warnings, field)
    for field in (
        "information_gaps",
        "original_approaches",
        "title_and_hook_ideas",
        "editorial_risks",
        "legal_risks",
        "cultural_risks",
    ):
        data[field] = [_strip_urls(t, warnings, field) for t in data[field]]
    return DossierDraft.model_validate(data), warnings


# ---- Markdown ------------------------------------------------------------------------------


def _stmts(items: list[Statement]) -> str:
    if not items:
        return "_Sem itens._\n"
    tag = {"fato": "Fato", "inferencia": "Inferência", "recomendacao": "Recomendação"}
    return "".join(
        f"- **{tag[s.kind]}:** {s.text}"
        + (f" [{', '.join(s.evidence_ids)}]" if s.evidence_ids else "")
        + "\n"
        for s in items
    )


def _bullets(items: list[str]) -> str:
    return "".join(f"- {t}\n" for t in items) if items else "_Sem itens._\n"


def _market_section(m: MarketResult) -> str:
    sc, cf = m.score, m.confidence
    lines = [f"### {m.market} ({m.language})\n"]
    if sc:
        if sc.status.value == "bloqueada":
            lines.append(f"**Status:** BLOQUEADA — {sc.blocked_reason}\n\n")
        elif sc.point is not None:
            lines.append(
                f"**Score experimental:** {sc.point}/100 (intervalo {sc.minimum}–{sc.maximum}; cobertura {sc.coverage:.0%})\n\n"
            )
        else:
            lines.append(
                f"**Score:** não exibido — cobertura {sc.coverage:.0%} abaixo do mínimo. "
                f"Intervalo possível: {sc.minimum}–{sc.maximum}/100.\n\n"
            )
    if cf:
        lines.append(f"**Confiança:** {cf.level} ({cf.value})\n\n")
        lines.extend(f"- {r}\n" for r in cf.reasons)
    lines.append("\n| Critério | Nível | Método | Entra no score? | Observação |\n|---|---|---|---|---|\n")
    for a in m.assessments:
        lines.append(
            f"| {CRITERIA_LABELS.get(a.criterion, a.criterion)} | {a.level if a.level is not None else '—'} | "
            f"{a.method.value} | {'sim' if a.subscore is not None else 'não'} | "
            f"{(a.exclusion_reason or a.rationale).replace('|', '/')[:160]} |\n"
        )
    found = [c for c in m.candidates if c.evidence_id]
    lines.append(
        f"\nBusca: {len(m.queries)} consulta(s) nativas ({'; '.join(m.queries) or '—'}) em "
        f"{', '.join(m.searched_sources) or 'nenhuma fonte'}; {len(m.candidates)} candidatos, "
        f"{len(found)} equivalentes aceitos, "
        f"{sum(1 for c in m.candidates if c.band.value == 'nao_avaliado')} não avaliáveis por política.\n"
    )
    if m.competition:
        c = m.competition
        lines.append(f"\n**Oferta:** {c.supply_situation} — {c.rationale}\n")
        if c.underexplored_angles:
            lines.append("\nÂngulos pouco explorados: " + "; ".join(c.underexplored_angles) + "\n")
    if m.editorial:
        e = m.editorial
        lines.append(
            f"\n**Riscos:** direitos {e.rights_risk} ({e.rights_notes}); factual {e.factual_risk}; "
            f"especialista {'sim' if e.needs_specialist else 'não'}; complexidade {e.estimated_research_complexity}; "
            f"vida útil {e.topic_lifespan}.\n"
        )
    return "".join(lines) + "\n"


def _references(evidence: list[Evidence]) -> str:
    rows = ["| Id | Tipo | Fonte | Coletado em | Afirmação | Link |\n|---|---|---|---|---|---|\n"]
    for e in sorted(evidence, key=lambda x: x.id):
        if not e.allows(Use.DISPLAY) and not e.provenance.simulated:
            continue
        rows.append(
            f"| {e.id} | {e.kind.value} | {e.provenance.source_id}/{e.provenance.data_class}"
            f"{' (SIMULADO)' if e.provenance.simulated else ''} | {e.provenance.collected_at:%Y-%m-%d} | "
            f"{e.claim.replace('|', '/')[:180]} | {e.url or '—'} |\n"
        )
    return "".join(rows)


def render_markdown(result: InvestigationResult, costs: list[dict[str, Any]]) -> str:
    d = result.draft
    out: list[str] = []
    if result.simulated:
        out.append(
            "> ⚠️ **EXECUÇÃO SIMULADA.** Contém dados ou respostas de modelo sintéticos. "
            "Nada neste dossiê é evidência real.\n\n"
        )
    title = d.provisional_title if d else result.subject.label
    out.append(
        f"# Dossiê — {title}\n\nExecução `{result.run_id}` · assunto `{result.subject.id}` · nicho {result.subject.niche}\n\n"
    )
    if d:
        out.append(f"## 1. Resumo\n{d.summary}\n\n")
    if result.interpretation:
        i = result.interpretation
        out.append(
            f"## 2. Assunto\n**Conceito:** {i.core_subject}\n\n**Gancho:** {i.narrative_hook}\n\n"
            f"**Fatos a verificar:**\n\n{_bullets(i.facts_to_verify)}\n"
            f"**Elementos do criador original a não replicar:**\n\n{_bullets(i.creator_specific_expression)}\n"
        )
    ranked = sorted(
        result.markets,
        key=lambda m: (
            m.score.point if m.score and m.score.point is not None else -1,
            m.score.maximum if m.score else 0,
        ),
        reverse=True,
    )
    out.append("## 3. Mercados de destino\n")
    out.extend(_market_section(m) for m in ranked)
    if d:
        out.append(f"## 4. Por que agora\n{_stmts(d.why_now)}\n")
        out.append(f"## 5. Evidências de demanda\n{_stmts(d.demand_evidence)}\n")
        out.append(f"## 6. Concorrência\n{_stmts(d.competition_evidence)}\n")
        out.append(f"## 7. Público potencial\n{_stmts(d.potential_audience)}\n")
        out.append(f"## 8. Abordagens originais\n{_bullets(d.original_approaches)}\n")
        out.append(f"## 9. Títulos e ganchos possíveis\n{_bullets(d.title_and_hook_ideas)}\n")
        out.append(f"## 10. Recursos necessários\n{d.resources_needed}\n\n")
        out.append(
            f"## 11. Riscos\n\n**Editoriais:**\n\n{_bullets(d.editorial_risks)}\n**Legais:**\n\n{_bullets(d.legal_risks)}\n"
            f"**Culturais:**\n\n{_bullets(d.cultural_risks)}\n"
        )
        out.append(f"## 12. Lacunas de informação\n{_bullets(d.information_gaps)}\n")
        out.append(f"## 13. Parecer\n**Recomendação:** {d.final_recommendation}\n\n{d.final_opinion}\n\n")
    if result.issues:
        out.append("## Falhas e bloqueios registrados\n")
        out.extend(
            f"- [{i.stage}{'/' + i.market if i.market else ''}{'/' + i.source if i.source else ''}] {i.error}\n"
            for i in result.issues
        )
        out.append("\n")
    if result.draft_warnings:
        out.append("## Avisos de validação do rascunho\n" + _bullets(result.draft_warnings) + "\n")
    out.append("## Referências\n" + _references(result.evidence) + "\n")
    out.append(
        "## Custos observados\n| Fonte | Operação | Chamadas | Unidades | US$ | Erros |\n|---|---|---|---|---|---|\n"
    )
    for c in costs:
        out.append(
            f"| {c['source_id']} | {c['operation']} | {c['calls']} | {c['units']} {c['unit']} | {float(c['usd'] or 0):.4f} | {c['errors']} |\n"
        )
    llm_usd = sum(m.usd for m in result.model_calls)
    out.append(
        f"\nChamadas de modelo: {len(result.model_calls)} (US$ {llm_usd:.4f}; em cache: {sum(m.cached for m in result.model_calls)}).\n"
    )
    return "".join(out)
