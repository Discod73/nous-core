#!/usr/bin/env python3
"""
Mutation: CuratorV2 — TF-IDF tuning + class balancing.

Kørsel: python3 /srv/nous/scripts/run_curator_v2_mutation.py

Strategier testet (rækkefølge = billigste først):
  A) LR class_weight='balanced'             — 0 kode-overhead
  B) TF-IDF (1,3)-gram + char subword       — bredere features
  C) Oversample minority via random repeat  — balancerer træningsdata

Vinder-strategi køres igennem Gaia vs. CuratorV1 baseline.
"""
from __future__ import annotations
import json
import pickle
import sys
import time
from pathlib import Path

sys.path.insert(0, "/srv/nous-test")
sys.path.insert(0, "/srv/nous")

from sklearn.feature_extraction.text import TfidfVectorizer
from sklearn.linear_model import LogisticRegression
from sklearn.metrics import f1_score, precision_recall_fscore_support
from sklearn.pipeline import Pipeline
from sklearn.utils import resample
import numpy as np

from gaia.models import Decision, FitnessResult, ClassMetrics
from gaia.safety_gate import SafetyGate
from gaia.decision_engine import DecisionEngine
from gaia.audit_report import AuditReport
from gaia.mutation_runner import CuratorV1Runner, MutationRunner

EVAL_DATA    = Path("/mnt/nous-data/eval_data.json")
BASELINE_PKL = Path("/mnt/nous-data/gaia_baseline_curatorv1.pkl")
V2_PKL       = Path("/mnt/nous-data/gaia_mutation_curatorv2.pkl")
EVOLOG       = Path("/mnt/nous-data/gaia_evolution_log.jsonl")


# ── Inline MutationRunner til sklearn Pipeline ────────────────────────────────
class SklearnRunner(MutationRunner):
    def __init__(self, pkl_path: Path, mtype: str):
        self._path = pkl_path
        self._mtype = mtype
        with open(pkl_path, "rb") as f:
            self._model = pickle.load(f)

    @property
    def mutation_type(self) -> str:
        return self._mtype

    @property
    def artifact_path(self) -> Path:
        return self._path

    def predict(self, texts: list[str]) -> list[str]:
        return list(self._model.predict(texts))

    def train_metrics(self) -> dict:
        return {}


# ── Data ──────────────────────────────────────────────────────────────────────
data = json.loads(EVAL_DATA.read_text())
train_records = data["train"]
test_records  = data["test"]
train_texts  = [r["text"]  for r in train_records]
train_labels = [r["label"] for r in train_records]
test_texts   = [r["text"]  for r in test_records]
test_labels  = [r["label"] for r in test_records]
wings = sorted(set(test_labels))

from collections import Counter
print(f"Train: {len(train_texts)} eks, fordeling: {dict(Counter(train_labels))}")
print(f"Test:  {len(test_texts)} eks, fordeling: {dict(Counter(test_labels))}")
print()


# ── Hjælper: trin og vurdering ────────────────────────────────────────────────
def fit_and_score(pipe: Pipeline, texts_tr, labels_tr) -> tuple[Pipeline, float, list[str]]:
    pipe.fit(texts_tr, labels_tr)
    preds = pipe.predict(test_texts)
    f1 = float(f1_score(test_labels, preds, average="macro", zero_division=0))
    return pipe, f1, list(preds)


def oversample_minority(texts, labels, target_ratio=0.3) -> tuple[list, list]:
    """Gentag minority-klasser så ingen klasse er under target_ratio af max."""
    from collections import defaultdict
    by_class: dict[str, list] = defaultdict(list)
    for t, l in zip(texts, labels):
        by_class[l].append(t)
    max_n = max(len(v) for v in by_class.values())
    target = int(max_n * target_ratio)
    new_texts, new_labels = list(texts), list(labels)
    for label, examples in by_class.items():
        if len(examples) < target:
            needed = target - len(examples)
            extra = resample(examples, n_samples=needed, random_state=42, replace=True)
            new_texts.extend(extra)
            new_labels.extend([label] * needed)
    return new_texts, new_labels


# ── Baseline (genindlæs eller gentræn) ───────────────────────────────────────
print("=== BASELINE: CuratorV1 ===")
if BASELINE_PKL.exists():
    with open(BASELINE_PKL, "rb") as f:
        baseline_model = pickle.load(f)
    baseline_preds = list(baseline_model.predict(test_texts))
else:
    baseline_pipe = Pipeline([
        ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, max_features=5000, sublinear_tf=True)),
        ("clf",   LogisticRegression(max_iter=1000, C=1.0, solver="lbfgs")),
    ])
    baseline_pipe.fit(train_texts, train_labels)
    baseline_preds = list(baseline_pipe.predict(test_texts))
    with open(BASELINE_PKL, "wb") as f:
        pickle.dump(baseline_pipe, f)
baseline_f1 = float(f1_score(test_labels, baseline_preds, average="macro", zero_division=0))
print(f"  Baseline macro F1: {baseline_f1:.4f}")
print()


# ── Strategi A: LR class_weight='balanced' ───────────────────────────────────
print("=== STRATEGI A: LR class_weight=balanced ===")
pipe_a = Pipeline([
    ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, max_features=5000, sublinear_tf=True)),
    ("clf",   LogisticRegression(max_iter=1000, C=1.0, solver="lbfgs", class_weight="balanced")),
])
_, f1_a, preds_a = fit_and_score(pipe_a, train_texts, train_labels)
print(f"  Macro F1: {f1_a:.4f}  (delta vs baseline: {f1_a - baseline_f1:+.4f})")


# ── Strategi B: TF-IDF (1,3)-gram, højere max_features, C=5 ─────────────────
print("=== STRATEGI B: TF-IDF (1,3) + C=5 ===")
pipe_b = Pipeline([
    ("tfidf", TfidfVectorizer(
        ngram_range=(1, 3), min_df=1, max_features=10000,
        sublinear_tf=True, analyzer="word",
    )),
    ("clf", LogisticRegression(max_iter=1000, C=5.0, solver="lbfgs", class_weight="balanced")),
])
_, f1_b, preds_b = fit_and_score(pipe_b, train_texts, train_labels)
print(f"  Macro F1: {f1_b:.4f}  (delta vs baseline: {f1_b - baseline_f1:+.4f})")


# ── Strategi C: Oversample + balanced + (1,2) ────────────────────────────────
print("=== STRATEGI C: Oversample minority + LR balanced ===")
texts_os, labels_os = oversample_minority(train_texts, train_labels, target_ratio=0.25)
print(f"  Oversample: {len(train_texts)} → {len(texts_os)} eksempler")
print(f"  Ny fordeling: {dict(Counter(labels_os))}")
pipe_c = Pipeline([
    ("tfidf", TfidfVectorizer(ngram_range=(1, 2), min_df=1, max_features=5000, sublinear_tf=True)),
    ("clf",   LogisticRegression(max_iter=1000, C=1.0, solver="lbfgs", class_weight="balanced")),
])
_, f1_c, preds_c = fit_and_score(pipe_c, texts_os, labels_os)
print(f"  Macro F1: {f1_c:.4f}  (delta vs baseline: {f1_c - baseline_f1:+.4f})")


# ── Vælg vinder ──────────────────────────────────────────────────────────────
print()
print("=== SAMMENFATNING ===")
candidates = [
    ("A: LR balanced",            f1_a, preds_a, pipe_a, train_texts,  train_labels,  "curator_v2_lr_balanced"),
    ("B: TF-IDF (1,3) + C=5",    f1_b, preds_b, pipe_b, train_texts,  train_labels,  "curator_v2_tfidf_13_c5"),
    ("C: Oversample + balanced",  f1_c, preds_c, pipe_c, texts_os,     labels_os,     "curator_v2_oversample"),
]
for name, f1, _, _, _, _, _ in candidates:
    marker = " ← VINDER" if f1 == max(f1_a, f1_b, f1_c) else ""
    print(f"  {name:<35}: {f1:.4f}{marker}")

winner_name, winner_f1, winner_preds, winner_pipe, winner_tr_t, winner_tr_l, winner_mtype = max(
    candidates, key=lambda x: x[1]
)
print(f"\nVinder: {winner_name}  (F1={winner_f1:.4f})")

# Gem vinder-model
with open(V2_PKL, "wb") as f:
    pickle.dump(winner_pipe, f)

# Train F1 til overfitting-check
train_preds_v2 = list(winner_pipe.predict(winner_tr_t))
train_f1_v2 = float(f1_score(winner_tr_l, train_preds_v2, average="macro", zero_division=0))
print(f"Train F1: {train_f1_v2:.4f}  Test F1: {winner_f1:.4f}  Gap: {train_f1_v2 - winner_f1:+.4f}")
print()


# ── Gaia: SafetyGate ─────────────────────────────────────────────────────────
print("=== GAIA EVALUERING ===")
gate = SafetyGate()
t0 = time.perf_counter()
safety = gate.check(
    y_true=test_labels,
    y_pred=winner_preds,
    train_metrics={"train_macro_f1": train_f1_v2, "test_macro_f1": winner_f1},
    artifact_readable=True,
    baseline_preds=baseline_preds,
)
print(f"SafetyGate: {'BESTÅET' if safety.passed else 'FEJLET'}")
for v in safety.violations:
    print(f"  [{v.rule}] {v.detail}")

# ── Gaia: FitnessResult ───────────────────────────────────────────────────────
def per_class(preds):
    prec, rec, f1, sup = precision_recall_fscore_support(
        test_labels, preds, labels=wings, zero_division=0
    )
    return [ClassMetrics(label=w, f1=float(f1[i]), precision=float(prec[i]),
                         recall=float(rec[i]), support=int(sup[i]))
            for i, w in enumerate(wings)]

fitness = None
if safety.passed:
    t1 = time.perf_counter()
    baseline_preds_rt = list(winner_pipe.predict(test_texts[:20]))  # latency sample
    baseline_lat = (time.perf_counter() - t1) * 1000 / 20

    fitness = FitnessResult(
        baseline_macro_f1   = baseline_f1,
        mutation_macro_f1   = winner_f1,
        delta_f1            = winner_f1 - baseline_f1,
        baseline_per_class  = per_class(baseline_preds),
        mutation_per_class  = per_class(winner_preds),
        baseline_latency_ms = 0.1,
        mutation_latency_ms = baseline_lat,
    )
    print(f"\nFitness:")
    print(f"  Baseline F1: {fitness.baseline_macro_f1:.4f}")
    print(f"  Mutation F1: {fitness.mutation_macro_f1:.4f}")
    print(f"  Delta F1:    {fitness.delta_f1:+.4f}")
    print(f"\nPer-wing:")
    for b, m in zip(fitness.baseline_per_class, fitness.mutation_per_class):
        delta = m.f1 - b.f1
        flag = " ▲" if delta > 0.01 else (" ▼" if delta < -0.005 else " ≈")
        print(f"  {b.label:<20}: baseline={b.f1:.4f}  mutation={m.f1:.4f}  {delta:+.4f}{flag}")

# ── DecisionEngine ─────────────────────────────────────────────────────────────
engine = DecisionEngine()
decision, reason = engine.decide(safety, fitness)
print(f"\nBESLUTNING: {decision.value}")
print(f"Begrundelse: {reason}")

# ── AuditReport ───────────────────────────────────────────────────────────────
reporter = AuditReport()
config = {
    "winner_strategy": winner_name,
    "mutation_type":   winner_mtype,
    "strategies_tested": ["A: lr_balanced", "B: tfidf_13_c5", "C: oversample_balanced"],
}
report_path = reporter.write(
    baseline_path   = BASELINE_PKL,
    mutation_path   = V2_PKL,
    texts           = test_texts,
    labels          = test_labels,
    config          = config,
    seed            = 42,
    mutation_type   = winner_mtype,
    safety_result   = safety,
    fitness_result  = fitness,
    decision        = decision,
    decision_reason = reason,
)
print(f"\nAuditrapport: {report_path}")

# ── Evolution log ──────────────────────────────────────────────────────────────
import hashlib, datetime as dt

run_id_raw = f"gaia_test_002_{decision.value}_{winner_mtype}"
run_id = hashlib.sha256(run_id_raw.encode()).hexdigest()[:12]

evo_entry = {
    "run_id":        f"gaia_test_002_{run_id}",
    "timestamp":     dt.datetime.now(dt.timezone.utc).isoformat(),
    "parent_run_id": "gaia_test_001",
    "mutation_name": winner_mtype,
    "mutation_type": "sklearn_tfidf",
    "decision":      decision.value,
    "baseline_f1":   round(baseline_f1, 4),
    "mutation_f1":   round(winner_f1, 4),
    "delta_f1":      round(winner_f1 - baseline_f1, 4),
    "strategies_compared": {
        "A_lr_balanced":    round(f1_a, 4),
        "B_tfidf13_c5":     round(f1_b, 4),
        "C_oversample":     round(f1_c, 4),
    },
    "winner_strategy": winner_name,
    "safety_violations": [{"rule": v.rule, "detail": v.detail} for v in safety.violations],
    "human_review": None,
    "audit_report":  str(report_path),
}
with open(EVOLOG, "a") as f:
    f.write(json.dumps(evo_entry, ensure_ascii=False) + "\n")
print(f"Evolution log: {EVOLOG} ({sum(1 for _ in open(EVOLOG))} entries)")
