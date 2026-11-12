import json
import re
import time
from pathlib import Path

from asgiref.sync import async_to_sync
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from rag import embedding_eval as ev
from tenants.models import Tenant

RESULTS_DIR = Path(settings.BASE_DIR) / "var" / "embedding_evals"
OUTCOME_ORDER = {"found": 3, "refused": 2, "wrong": 1, "leaked": 0, "no_right_passage": -1}


class Command(BaseCommand):
    help = (
        "Test how well an embedding model finds the right passages (nothing is saved to the database). "
        "Results are written to var/embedding_evals/ so runs can be compared."
    )

    def add_arguments(self, parser):
        parser.add_argument("--model", action="append", default=[],
                            help="Embedding model to test, e.g. bedrock/cohere.embed-multilingual-v3 "
                                 "(default: the enabled one). Repeat to compare models in one run.")
        parser.add_argument("--set", action="append", default=[], metavar="FIELD=VALUE",
                            help="Try a value without saving it, e.g. --set min_score=0.33 or "
                                 "--set query_params='{\"input_type\": \"search_query\"}'. Applies to every --model.")
        parser.add_argument("--cases", help="Test cases JSON (default rag/eval_cases/embeddings.json)")
        parser.add_argument("--tenant", action="append", default=[], help="Only these workspace slugs")
        parser.add_argument("--compare", metavar="FILE|latest",
                            help="Compare with an earlier result file, or 'latest' for the newest saved one")
        parser.add_argument("--no-save", action="store_true", help="Don't write a result file")
        parser.add_argument("--details", action="store_true", help="List every question, not only problems")

    def handle(self, *args, **opts):
        try:
            cases = ev.load_cases(opts["cases"])
            if opts["tenant"]:
                cases["workspaces"] = {k: v for k, v in cases.get("workspaces", {}).items() if k in opts["tenant"]}
            overrides = dict(self._parse_set(item) for item in opts["set"])
            baseline = self._load_baseline(opts["compare"]) if opts["compare"] else None
            tenants = {t.slug: t for t in Tenant.objects.filter(slug__in=list(cases.get("workspaces", {})))}
            configs = [ev.candidate_config(m, overrides) for m in (opts["model"] or [None])]
        except ev.EvalError as exc:
            raise CommandError(str(exc)) from exc

        reports = async_to_sync(self._run_all)(cases, configs, tenants, overrides, opts)

        if baseline:
            reports.insert(0, baseline)
        if len(reports) > 1:
            self._compare(reports)

    async def _run_all(self, cases, configs, tenants, overrides, opts):
        """All models in one event loop, then close LiteLLM's HTTP clients cleanly."""
        import litellm

        reports = []
        try:
            for config in configs:
                self.stdout.write(self.style.MIGRATE_HEADING(f"\n=== {config.embed_model}"))
                try:
                    report = await ev.run(cases, config, tenants, log=lambda m: self.stdout.write(f"  {m}"))
                except Exception as exc:  # LLM errors etc.
                    raise CommandError(f"{config.embed_model}: {exc}") from exc
                report["overrides"] = overrides
                self._print(report, opts["details"])
                if not opts["no_save"]:
                    path = self._save(report)
                    shown = path.relative_to(settings.BASE_DIR) if path.is_relative_to(settings.BASE_DIR) else path
                    self.stdout.write(f"  saved {shown}")
                reports.append(report)
        finally:
            try:
                await litellm.close_litellm_async_clients()
            except Exception:
                pass
        return reports

    # --- input ------------------------------------------------------------------

    def _parse_set(self, item):
        name, sep, raw = item.partition("=")
        if not sep:
            raise ev.EvalError(f"--set needs FIELD=VALUE, got {item!r}")
        try:
            value = json.loads(raw)
        except ValueError:
            value = raw  # plain strings such as prefixes
        if raw.strip().lower() in ("none", "null", ""):
            value = None
        return name.strip(), value

    def _load_baseline(self, which):
        if which == "latest":
            files = sorted(RESULTS_DIR.glob("*.json"))
            if not files:
                raise ev.EvalError(f"No saved results in {RESULTS_DIR}")
            path = files[-1]
        else:
            path = Path(which)
        try:
            report = json.loads(path.read_text())
        except (OSError, ValueError) as exc:
            raise ev.EvalError(f"Cannot read {path}: {exc}") from exc
        report["_file"] = path.name
        return report

    def _save(self, report):
        RESULTS_DIR.mkdir(parents=True, exist_ok=True)
        slug = re.sub(r"[^A-Za-z0-9.]+", "-", report["model"]).strip("-")
        path = RESULTS_DIR / f"{time.strftime('%Y%m%d-%H%M%S')}-{slug}.json"
        n = 1
        while path.exists():
            n += 1
            path = path.with_name(f"{path.stem}-{n}.json")
        path.write_text(json.dumps(report, indent=2, ensure_ascii=False))
        return path

    # --- output -----------------------------------------------------------------

    def _print(self, r, details):
        st, s = r["settings"], r["summary"]
        w = self.stdout.write
        w(f"  cut-offs: document {st['doc_cutoff']}, strong {st['strong_cutoff']:.3g}, "
          f"FAQ {st['faq_cutoff']}, FAQ context {st['faq_context_cutoff']}")
        w(f"  options:  query {st['query_params'] or '{}'}, document {st['document_params'] or '{}'}")
        if r.get("overrides"):
            w(self.style.WARNING(f"  unsaved overrides: {r['overrides']}"))
        if r["skipped"]:
            w(self.style.WARNING(f"  skipped (no such workspace): {', '.join(r['skipped'])}"))
        w("")
        for q in r["questions"]:
            good = (q["kind"] == "answerable" and q["outcome"] == "found") or (q["kind"] == "off_topic" and q["outcome"] == "refused")
            if details or not good:
                tag = "ok " if good else ("?? " if q["outcome"] == "no_right_passage" else "BAD")
                extra = (f"right passage {q['right_score']} (rank {q['right_rank']})" if q["kind"] == "answerable"
                         and q.get("right_score") is not None else f"best {q['best_score']}")
                line = f"  {tag} {q['outcome']:8} {q['workspace']:18} {extra:30} {q['question']}"
                w(line if good else self.style.ERROR(line))
        for p in r["faq_pairs"]:
            if details or not p["ok"]:
                note = "answered by FAQ" if p["answered_by_faq"] else (
                    "similar, blocked by wording rule" if p["similar_enough"] else "not similar enough")
                line = f"  {'ok ' if p['ok'] else 'BAD'} FAQ {p['expect']:9} {p['score']:.3f}  {note:32} {p['asked']!r} ~ {p['faq']!r}"
                w(line if p["ok"] else self.style.ERROR(line))
        w("")
        w(f"  Real questions answered from the right passage: {s['found']}/{s['answerable']}"
          f"  (wrong passage {s['wrong']}, refused {s['refused']})")
        w(f"  Off-topic questions refused:                    {s['off_topic_refused']}/{s['off_topic']}")
        w(f"  FAQ pairs handled correctly:                    {s['faq_ok']}/{s['faq_pairs']}")
        w(f"  Right passage ranked in top 3:                  {s['right_in_top3']}/{s['answerable']}")
        if s["margin"] is not None:
            verdict = "separates cleanly" if s["margin"] > 0 else "overlaps: no single cut-off separates them"
            w(f"  Lowest right-passage score {s['lowest_right_score']:.3f} vs highest off-topic {s['highest_off_topic_score']:.3f}"
              f" -> margin {s['margin']:+.3f} ({verdict})")
        if s["bad_cases"]:
            w(self.style.WARNING(f"  {s['bad_cases']} test case(s) match no passage at all: fix their 'expect'/'document'"))
        w(self.style.SUCCESS(f"  SCORE {s['passed']}/{s['total']} ({s['score']:.0%})") if s["score"] is not None else "  no cases ran")

    def _compare(self, reports):
        w = self.stdout.write
        names = [chr(ord("A") + i) for i in range(len(reports))]
        w(self.style.MIGRATE_HEADING("\n=== Comparison (A is the baseline)"))
        for n, r in zip(names, reports):
            source = f"saved {r['_file']}" if r.get("_file") else "this run"
            extra = f", unsaved overrides {r['overrides']}" if r.get("overrides") else ""
            w(f"  {n}: {r['model']} ({source}{extra})")
        w("")
        rows = [("Score", lambda s: f"{s['passed']}/{s['total']} ({s['score']:.0%})" if s["score"] is not None else "-"),
                ("Right passage found", lambda s: f"{s['found']}/{s['answerable']}"),
                ("Off-topic refused", lambda s: f"{s['off_topic_refused']}/{s['off_topic']}"),
                ("FAQ pairs correct", lambda s: f"{s['faq_ok']}/{s['faq_pairs']}"),
                ("Right passage in top 3", lambda s: f"{s['right_in_top3']}/{s['answerable']}"),
                ("Separation margin", lambda s: f"{s['margin']:+.3f}" if s["margin"] is not None else "-")]
        width = 14
        w("  " + " " * 24 + "".join(f"{n[:width]:>{width + 2}}" for n in names))
        for label, fmt in rows:
            w(f"  {label:24}" + "".join(f"{fmt(r['summary']):>{width + 2}}" for r in reports))
        base = {(q["workspace"], q["question"]): q for q in reports[0]["questions"]}
        for r, name in zip(reports[1:], names[1:]):
            changes = []
            for q in r["questions"]:
                old = base.get((q["workspace"], q["question"]))
                if old and old["outcome"] != q["outcome"]:
                    better = OUTCOME_ORDER[q["outcome"]] > OUTCOME_ORDER[old["outcome"]] if q["kind"] == "answerable" \
                        else q["outcome"] == "refused"
                    changes.append((better, f"{old['outcome']} -> {q['outcome']}", q))
            if changes:
                w(f"\n  Changes in {name}:")
                for better, what, q in sorted(changes, key=lambda c: not c[0]):
                    line = f"    {'better' if better else 'WORSE '} {what:22} {q['workspace']:18} {q['question']}"
                    w(self.style.SUCCESS(line) if better else self.style.ERROR(line))
