#!/usr/bin/env python3
"""
Trin 3+4+5 — Hent results_v4.json fra Nano og kør Gaia-evaluering.

Kør:  python3 /srv/nous/scripts/run_gaia_eval.py

Forudsætter at trin 1 (export_eval_data.py) og trin 2 (lora_v4 på Nano) er kørt.
Henter <NOUS_NANO_EVAL_DEST> fra Nano via SCP.
Bruger /mnt/nous-data/eval_data.json (beholdt fra trin 1) til CuratorV1 baseline.
"""
from __future__ import annotations
import json
import os
import pickle
import subprocess
import sys
import tempfile
import time
from pathlib import Path

NANO_USER    = os.environ.get("NOUS_NANO_USER", "nous")
NANO_IP      = os.environ.get("NOUS_NANO_HOST", "CHANGE_ME")
SSH_KEY      = Path(os.environ.get("NOUS_SSH_KEY_PATH", str(Path.home() / ".ssh" / "id_ed25519")))
NANO_RESULTS = os.environ.get("NOUS_NANO_EVAL_DEST", "/home/nous/lora_v4/results_v4.json")
LOCAL_RESULTS = Path("/mnt/nous-data/results_v4.json")
EVAL_DATA     = Path("/mnt/nous-data/eval_data.json")
BASELINE_PKL  = Path("/mnt/nous-data/gaia_baseline_curatorv1.pkl")

# Gaia-moduler fra /srv/nous-test/gaia/
sys.path.insert(0, "/srv/nous-test")
sys.path.insert(0, "/srv/nous")
from gaia.safety_gate import SafetyGate
from gaia.decision_engine import DecisionEngine
from gaia.audit_report import AuditReport
from gaia.models import Decision, FitnessResult, ClassMetrics
from gaia.mutation_runner import MutationRunner


# ── PrecomputedRunner — wrapper om Nanos forudregnede predictions ─────────────
class PrecomputedRunner(MutationRunner):
    """
    Indlæser predictions fra results_v4.json i stedet for at køre live inferens.
    Artifakt-stien peger på results_v4.json-filen (bruges til version-hash i audit).
    """

    def __init__(self, results_path: Path):
        self._path = results_path
        data = json.loads(results_path.read_text())
        self._preds      = data["test_predictions"]
        self._metrics    = {
            "train_macro_f1": data["train_macro_f1"],
            "test_macro_f1":  data["test_macro_f1"],
        }
        self._inf_ms     = data.get("infer_ms_per_example", 0.0)
        self._meta       = data

    @property
    def mutation_type(self) -> str:
        return "lora_v4_danish_bert"

    @property
    def artifact_path(self) -> Path:
        return self._path

    def predict(self, texts: list[str]) -> list[str]:
        return self._preds[: len(texts)]

    def train_metrics(self) -> dict:
        return self._metrics

    def measure_latency_ms(self, texts: list[str], n: int = 20) -> float:
        return self._inf_ms


# ── CuratorV1Runner — træner TF-IDF+LR på Pi5 (baseline) ────────────────────
class CuratorV1LocalRunner(MutationRunner):
    """Træner CuratorV1 på Pi5-siden af eval_data (train split)."""

    def __init__(self, pkl_path: Path):
        self._path = pkl_path
        with open(pkl_path, "rb") as f:
            self._model = pickle.load(f)

    @property
    def mutation_type(self) -> str:
        return "curator_v1_tfidf_lr"

    @property
    def artifact_path(self) -> Path:
        return self._path

    def predict(self, texts: list[str]) -> list[str]:
        t0 = time.perf_counter()
        preds = list(self._model.predict(texts))
        self._last_ms = (time.perf_counter() - t0) * 1000 / max(len(texts), 1)
        return preds

    def train_metrics(self) -> dict:
        return {}

    def measure_latency_ms(self, texts: list[str], n: int = 20) -> float:
        sample = texts[:n] if len(texts) >= n else texts
        t0 = time.perf_counter()
        self._model.predict(sample)
        return (time.perf_counter() - t0) * 1000 / max(len(sample), 1)


def train_curator_v1(train_records: list[dict], pkl_path: Path) -> CuratorV1LocalRunner:
    from sklearn.pipeline import Pipeline
    from sklearn.feature_extraction.text import TfidfVectorizer
    from sklearn.linear_model import LogisticRegression

    texts  = [r["text"]  for r in train_records]
    labels = [r["label"] for r in train_records]

    model = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, max_features=5000, sublinear_tf=True)),
        ("clf",   LogisticRegression(max_iter=1000, C=1.0, solver="lbfgs")),
    ])
    model.fit(texts, labels)

    pkl_path.parent.mkdir(parents=True, exist_ok=True)
    with open(pkl_path, "wb") as f:
        pickle.dump(model, f)

    print(f"CuratorV1 trænet på {len(texts)} eksempler → {pkl_path}")
    return CuratorV1LocalRunner(pkl_path)


def main() -> None:
    print("=" * 65)
    print("GAIA EVALUERING — LoRA v4 vs. Curator v1 (baseline)")
    print("=" * 65)

    # ── Trin 3: Hent results_v4.json fra Nano ──────────────────────────────
    if LOCAL_RESULTS.exists():
        print(f"\n[3] results_v4.json allerede lokal: {LOCAL_RESULTS} ({LOCAL_RESULTS.stat().st_size:,} bytes)")
    else:
        print(f"\n[3] SCP results_v4.json fra Nano ({NANO_IP}) ...")
        LOCAL_RESULTS.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(
            ["scp", "-i", str(SSH_KEY),
             f"{NANO_USER}@{NANO_IP}:{NANO_RESULTS}",
             str(LOCAL_RESULTS)],
            check=True,
        )
        print(f"    Modtaget: {LOCAL_RESULTS} ({LOCAL_RESULTS.stat().st_size:,} bytes)")

    # ── Indlæs eval-data (Pi5) ─────────────────────────────────────────────
    if not EVAL_DATA.exists():
        print(f"FEJL: {EVAL_DATA} ikke fundet. Kør export_eval_data.py først.")
        sys.exit(1)

    eval_payload  = json.loads(EVAL_DATA.read_text())
    train_records = eval_payload["train"]
    test_records  = eval_payload["test"]
    test_texts    = [r["text"]  for r in test_records]
    test_labels   = [r["label"] for r in test_records]

    print(f"    Eval-data: {len(train_records)} train / {len(test_records)} test")

    # ── Indlæs v4-resultater ───────────────────────────────────────────────
    v4_data = json.loads(LOCAL_RESULTS.read_text())
    print(f"\nLoRA v4 resultater fra Nano ({v4_data.get('gpu', 'ukendt GPU')}):")
    print(f"  Train F1:  {v4_data['train_macro_f1']:.4f}")
    print(f"  Test F1:   {v4_data['test_macro_f1']:.4f}")
    print(f"  Gap:       {v4_data['train_macro_f1'] - v4_data['test_macro_f1']:+.4f}")
    print(f"  Inferens:  {v4_data.get('infer_ms_per_example', '?')} ms/eks")
    print("\n  Per-wing:")
    for wing, m in sorted(v4_data.get("per_wing", {}).items()):
        print(f"    {wing:<20}: F1={m['f1']:.4f}  (support={m['support']})")

    # ── Trin 4a: Træn CuratorV1 baseline på Pi5 ───────────────────────────
    print(f"\n[4a] Træner CuratorV1 baseline på Pi5 ...")
    baseline_runner = train_curator_v1(train_records, BASELINE_PKL)

    # Baseline predictions på testset
    baseline_preds = baseline_runner.predict(test_texts)
    from sklearn.metrics import f1_score
    baseline_f1 = f1_score(test_labels, baseline_preds, average="macro", zero_division=0)
    print(f"     Baseline test macro F1: {baseline_f1:.4f}")

    # ── Mutation runner (pre-computed fra Nano) ────────────────────────────
    mutation_runner = PrecomputedRunner(LOCAL_RESULTS)
    mutation_preds  = mutation_runner.predict(test_texts)

    # Verificér at predictions passer til test-labels
    if len(mutation_preds) != len(test_labels):
        print(f"FEJL: {len(mutation_preds)} predictions ≠ {len(test_labels)} labels")
        sys.exit(1)

    # ── Trin 4b: SafetyGate ────────────────────────────────────────────────
    print(f"\n[4b] SafetyGate check ...")
    gate = SafetyGate()
    safety_result = gate.check(
        y_true=test_labels,
        y_pred=mutation_preds,
        train_metrics=mutation_runner.train_metrics(),
        artifact_readable=True,
        baseline_preds=baseline_preds,
    )

    if safety_result.passed:
        print("     SafetyGate: BESTÅET ✓")
    else:
        print(f"     SafetyGate: FEJLET ({len(safety_result.violations)} violation(s))")
        for v in safety_result.violations:
            print(f"       [{v.rule}] {v.detail}")

    # ── Trin 4c: FitnessResult (manuelt konstrueret fra kendte tal) ────────
    # FitnessEvaluator ville normalt kalde .predict() på begge runners.
    # Her bygger vi FitnessResult direkte fra forudregnede tal for at undgå
    # at genindlæse LoRA-modellen lokalt på Pi5 (den er kun på Nano).

    from sklearn.metrics import precision_recall_fscore_support
    wings = sorted(set(test_labels))

    def per_class(preds: list[str]) -> list[ClassMetrics]:
        prec, rec, f1, sup = precision_recall_fscore_support(
            test_labels, preds, labels=wings, zero_division=0
        )
        return [
            ClassMetrics(label=w, f1=float(f1[i]), precision=float(prec[i]),
                         recall=float(rec[i]), support=int(sup[i]))
            for i, w in enumerate(wings)
        ]

    mutation_f1 = f1_score(test_labels, mutation_preds, average="macro", zero_division=0)

    fitness_result = None
    if safety_result.passed:
        fitness_result = FitnessResult(
            baseline_macro_f1   = float(baseline_f1),
            mutation_macro_f1   = float(mutation_f1),
            delta_f1            = float(mutation_f1 - baseline_f1),
            baseline_per_class  = per_class(baseline_preds),
            mutation_per_class  = per_class(mutation_preds),
            baseline_latency_ms = baseline_runner.measure_latency_ms(test_texts[:20]),
            mutation_latency_ms = float(v4_data.get("infer_ms_per_example", 0.0)),
        )
        print(f"\n     Baseline:  {fitness_result.baseline_macro_f1:.4f}")
        print(f"     Mutation:  {fitness_result.mutation_macro_f1:.4f}")
        print(f"     Delta F1:  {fitness_result.delta_f1:+.4f}")

    # ── Trin 4d: DecisionEngine ────────────────────────────────────────────
    print(f"\n[4d] DecisionEngine ...")
    engine = DecisionEngine()
    decision, reason = engine.decide(safety_result, fitness_result)
    print(f"     BESLUTNING: {decision.value}")
    print(f"     Begrundelse: {reason}")

    # ── Trin 4e: AuditReport ───────────────────────────────────────────────
    print(f"\n[4e] AuditReport ...")
    reporter = AuditReport()
    report_path = reporter.write(
        baseline_path   = BASELINE_PKL,
        mutation_path   = LOCAL_RESULTS,
        texts           = test_texts,
        labels          = test_labels,
        config          = v4_data.get("lora", {}),
        seed            = v4_data.get("seed", 42),
        mutation_type   = "lora_v4_danish_bert",
        safety_result   = safety_result,
        fitness_result  = fitness_result,
        decision        = decision,
        decision_reason = reason,
    )
    print(f"     Auditrapport gemt: {report_path}")

    # ── Trin 5: Vis auditrapport ───────────────────────────────────────────
    print(f"\n{'='*65}")
    print("AUDITRAPPORT (trin 5)")
    print("=" * 65)
    report = json.loads(report_path.read_text())
    print(json.dumps(report, indent=2, ensure_ascii=False))
    print(f"\nFil: {report_path}")


if __name__ == "__main__":
    main()
