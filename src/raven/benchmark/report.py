"""Métricas e relatório do benchmark de fontes.

Operacionais (sempre): chamadas, bloqueios, erros, resultados por chamada, custo, latência,
filtros ignorados.
Qualidade (somente com rótulos humanos em labels.csv):
- cobertura: fração das combinações assunto×idioma com ≥1 resultado relevante (relevance=2);
- precisão: relevantes ÷ retornados rotulados; relacionados contam à parte;
- falsos positivos: irrelevantes (0) ÷ retornados rotulados;
- exclusivos: relevantes encontrados só por aquele fornecedor na combinação;
- idioma correto: language_ok=s ÷ rotulados;
- custo por resultado relevante.
"""

from __future__ import annotations

import csv
import json
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any

from raven.benchmark.protocol import BenchmarkCall, TrendsCall


def _pct(n: float, d: float) -> str:
    return f"{100 * n / d:.0f}%" if d else "—"


def _p(values: list[float], q: float) -> float | None:
    if not values:
        return None
    s = sorted(values)
    return s[min(len(s) - 1, int(q * (len(s) - 1) + 0.5))]


def load_calls(run_dir: Path) -> list[BenchmarkCall]:
    p = run_dir / "results.jsonl"
    return (
        [BenchmarkCall.model_validate_json(x) for x in p.read_text(encoding="utf-8").splitlines()]
        if p.exists()
        else []
    )


def load_labels(run_dir: Path) -> dict[tuple[str, str, str], dict[str, str]]:
    p = run_dir / "labels.csv"
    if not p.exists():
        return {}
    with p.open(encoding="utf-8") as fh:
        return {
            (r["subject_id"], r["language"], r["video_id"]): r
            for r in csv.DictReader(fh)
            if r.get("relevance", "").strip() in {"0", "1", "2"}
        }


def compute_metrics(
    calls: list[BenchmarkCall], labels: dict[tuple[str, str, str], dict[str, str]]
) -> dict[str, Any]:
    providers = sorted({c.provider for c in calls})
    combos = sorted({(c.subject_id, c.language) for c in calls})
    relevant_by_combo: dict[tuple[str, str], dict[str, set[str]]] = defaultdict(lambda: defaultdict(set))
    out: dict[str, Any] = {"providers": {}, "combos": len(combos), "labeled_items": len(labels)}
    for c in calls:
        if not c.ok:
            continue
        for vid in c.video_ids:
            lab = labels.get((c.subject_id, c.language, vid))
            if lab and lab["relevance"] == "2":
                relevant_by_combo[(c.subject_id, c.language)][c.provider].add(vid)

    for p in providers:
        pc = [c for c in calls if c.provider == p]
        ok = [c for c in pc if c.ok]
        lat = [c.latency_ms for c in ok]
        usd = [c.cost_usd for c in ok if c.cost_usd is not None]
        labeled = rel = related = irr = lang_ok = lang_labeled = 0
        for c in ok:
            for vid in c.video_ids:
                lab = labels.get((c.subject_id, c.language, vid))
                if not lab:
                    continue
                labeled += 1
                rel += lab["relevance"] == "2"
                related += lab["relevance"] == "1"
                irr += lab["relevance"] == "0"
                if lab.get("language_ok", "").strip().lower() in {"s", "n", "y"}:
                    lang_labeled += 1
                    lang_ok += lab["language_ok"].strip().lower() in {"s", "y"}
        p_combos = {(c.subject_id, c.language) for c in ok}
        covered = sum(1 for k in p_combos if relevant_by_combo[k].get(p))
        exclusive = 0
        for by_p in relevant_by_combo.values():
            others = set().union(*(v for q, v in by_p.items() if q != p)) if len(by_p) > 1 else set()
            exclusive += len(by_p.get(p, set()) - others)
        ignored: dict[str, int] = defaultdict(int)
        for c in ok:
            for f in c.ignored_filters:
                ignored[f] += 1
        out["providers"][p] = {
            "calls": len(pc),
            "ok": len(ok),
            "blocked": sum(c.blocked for c in pc),
            "errors": sum(1 for c in pc if not c.ok and not c.blocked),
            "error_samples": sorted({(c.error or "")[:140] for c in pc if not c.ok})[:3],
            "results_per_call": round(statistics.mean([c.n_results for c in ok]), 1) if ok else None,
            "zero_result_calls": sum(1 for c in ok if c.n_results == 0),
            "cost_units": round(sum(c.cost_units for c in ok), 4),
            "cost_unit": next((c.cost_unit for c in ok if c.cost_unit), ""),
            "cost_usd": round(sum(usd), 4) if usd else None,
            "latency_p50_ms": _p(lat, 0.5),
            "latency_p95_ms": _p(lat, 0.95),
            "ignored_filters": dict(ignored),
            "simulated": any(c.simulated for c in pc),
            "labeled": labeled,
            "relevant": rel,
            "related": related,
            "irrelevant": irr,
            "coverage_combos": covered,
            "combos_ok": len(p_combos),
            "exclusive_relevant": exclusive,
            "language_ok": lang_ok,
            "language_labeled": lang_labeled,
            "usd_per_relevant": round(sum(usd) / rel, 4) if usd and rel else None,
        }
    return out


def render_report(run_dir: Path) -> str:
    manifest = json.loads((run_dir / "manifest.json").read_text(encoding="utf-8"))
    calls = load_calls(run_dir)
    labels = load_labels(run_dir)
    m = compute_metrics(calls, labels)
    lines: list[str] = []
    if manifest.get("simulated") or any(v["simulated"] for v in m["providers"].values()):
        lines.append(
            "> ⚠️ **BENCHMARK COM DADOS SIMULADOS.** Os números abaixo testam o mecanismo, não as fontes. Não usar para decisão.\n\n"
        )
    lines.append(f"# Benchmark de fontes — `{manifest['run_id']}`\n\n")
    lines.append(
        f"Amostra `{manifest['sample_hash']}` · {len(manifest['subjects'])} assuntos · idiomas {', '.join(manifest['languages'])} · "
        f"{manifest['queries_per_language']} consulta(s)/idioma · {manifest['results_per_query']} resultados/consulta · "
        f"{m['combos']} combinações assunto×idioma.\n\n"
    )
    lines.append(
        "## Operação\n\n| Fornecedor | Estado | Chamadas | OK | Bloqueadas | Erros | Result./chamada | Sem resultado | Custo | US$ | p50 ms | p95 ms | Filtros ignorados |\n|---|---|---|---|---|---|---|---|---|---|---|---|---|\n"
    )
    for p, v in m["providers"].items():
        lines.append(
            f"| {p} | {manifest.get('provider_status', {}).get(p, '?')} | {v['calls']} | {v['ok']} | {v['blocked']} | {v['errors']} | "
            f"{v['results_per_call'] if v['results_per_call'] is not None else '—'} | {v['zero_result_calls']} | "
            f"{v['cost_units']} {v['cost_unit']} | {v['cost_usd'] if v['cost_usd'] is not None else '—'} | "
            f"{v['latency_p50_ms'] and round(v['latency_p50_ms'])} | {v['latency_p95_ms'] and round(v['latency_p95_ms'])} | "
            f"{', '.join(f'{k}×{n}' for k, n in v['ignored_filters'].items()) or '—'} |\n"
        )
    errs = {p: v["error_samples"] for p, v in m["providers"].items() if v["error_samples"]}
    if errs:
        lines.append("\n**Exemplos de erro/bloqueio:**\n")
        for p, samples in errs.items():
            lines.extend(f"- {p}: {s}\n" for s in samples)
    lines.append("\n## Qualidade (rótulos humanos)\n\n")
    if not labels:
        lines.append(
            "_Pendente de rotulagem._ Preencha `relevance` (2/1/0) e `language_ok` (s/n) em `labels.csv` e rode "
            "`raven benchmark report` novamente. Sem rótulos não há métrica de relevância, cobertura ou exclusividade.\n"
        )
    else:
        lines.append(
            f"{m['labeled_items']} itens rotulados.\n\n| Fornecedor | Cobertura | Precisão | Relacionados | Falsos positivos | Exclusivos relevantes | Idioma correto | US$/relevante |\n|---|---|---|---|---|---|---|---|\n"
        )
        for p, v in m["providers"].items():
            lines.append(
                f"| {p} | {v['coverage_combos']}/{v['combos_ok']} ({_pct(v['coverage_combos'], v['combos_ok'])}) | "
                f"{_pct(v['relevant'], v['labeled'])} | {_pct(v['related'], v['labeled'])} | {_pct(v['irrelevant'], v['labeled'])} | "
                f"{v['exclusive_relevant']} | {_pct(v['language_ok'], v['language_labeled'])} | {v['usd_per_relevant'] or '—'} |\n"
            )
        lines.append(
            "\nAmostra pequena: resultados exploratórios, não estimativas com precisão estatística.\n"
        )
    tpath = run_dir / "trends.jsonl"
    if tpath.exists():
        tcalls = [TrendsCall.model_validate_json(x) for x in tpath.read_text(encoding="utf-8").splitlines()]
        ok = [t for t in tcalls if t.ok]
        with_data = [t for t in ok if t.valid_series > 0]
        lines.append(
            f"\n## Cobertura de demanda (Trends YouTube)\n\n{len(tcalls)} consultas assunto×mercado; {len(ok)} sem erro; "
            f"{len(with_data)} com ao menos uma série utilizável ({_pct(len(with_data), len(ok))}).\n\n| Mercado | Consultas | Com dados |\n|---|---|---|\n"
        )
        by_m: dict[str, list[TrendsCall]] = defaultdict(list)
        for t in ok:
            by_m[t.market].append(t)
        for mk, ts in sorted(by_m.items()):
            lines.append(f"| {mk} | {len(ts)} | {sum(1 for t in ts if t.valid_series)} |\n")
    costs = run_dir / "costs.json"
    if costs.exists():
        lines.append(
            "\n## Consumo registrado no ledger\n\n| Fonte | Operação | Chamadas | Unidades | US$ | Erros |\n|---|---|---|---|---|---|\n"
        )
        for c in json.loads(costs.read_text(encoding="utf-8")):
            lines.append(
                f"| {c['source_id']} | {c['operation']} | {c['calls']} | {c['units']} {c['unit']} | {float(c['usd'] or 0):.4f} | {c['errors']} |\n"
            )
    return "".join(lines)
