#!/usr/bin/env python3
"""
LoRA v4 — GPU-træning på Nano med PEFT (Maltehb/danish-bert-botxo).

Køres PÅ Nano via SSH fra Pi5 (run_gaia_eval.py) eller manuelt:
    /home/nous/lora_v3_env/bin/python /home/nous/lora_v4/lora_v4.py

Forbedringer over v3:
  - AutoModelForSequenceClassification + PEFT get_peft_model (TaskType.SEQ_CLS)
  - modules_to_save=["classifier"] — head trænes altid
  - target_modules: query, value, key, dense (bredere coverage)
  - r=16, alpha=32 (op fra v3)
  - class_weight i loss (håndterer ubalance)
  - cosine scheduler med warm-up
  - Gemmer i LoRAv4Runner-kompatibelt format
"""
from __future__ import annotations
import json
import os
import random
import sys
import time
from collections import Counter
from pathlib import Path

import numpy as np
import torch
from collections import Counter
from sklearn.metrics import f1_score, precision_recall_fscore_support

import sys as _sys
_sys.stdout = open(_sys.stdout.fileno(), mode='w', buffering=1)  # linje-bufferet

assert torch.cuda.is_available(), "FEJL: GPU ikke tilgængelig"
GPU_NAME = torch.cuda.get_device_name(0)
CUDA_VER = torch.version.cuda
print(f"GPU: {GPU_NAME}, CUDA {CUDA_VER}", flush=True)
print(flush=True)

# ── Konfiguration ─────────────────────────────────────────────────────────────
DATA_FILE    = "/home/nous/lora_v4/eval_data.json"
MODEL_CACHE  = "/home/nous/gaia_tests/model_cache"
OUT_DIR      = "/home/nous/lora_v4/adapter"
RESULTS_FILE = "/home/nous/lora_v4/results_v4.json"
LOG_FILE     = "/home/nous/lora_v4/train_v4.log"

SEED         = 42
MAX_LEN      = 256
BATCH_SIZE   = 16
EPOCHS       = 80
LR           = 2e-4
WARMUP_RATIO = 0.10
WEIGHT_DECAY = 0.01
LORA_R       = 16
LORA_ALPHA   = 32
LORA_DROPOUT = 0.05
MAX_CLASS_WEIGHT = 4.0   # cap: forhindrer ekstrem ubalance fra at destabilisere

random.seed(SEED)
np.random.seed(SEED)
torch.manual_seed(SEED)

# ── Indlæs data ───────────────────────────────────────────────────────────────
with open(DATA_FILE) as f:
    data = json.load(f)

train_raw = data["train"]
test_raw  = data["test"]

train_texts  = [r["text"]  for r in train_raw]
train_labels = [r["label"] for r in train_raw]
test_texts   = [r["text"]  for r in test_raw]
test_labels  = [r["label"] for r in test_raw]

WING_NAMES = sorted(set(train_labels + test_labels))
LABEL2ID   = {l: i for i, l in enumerate(WING_NAMES)}
ID2LABEL   = {i: l for l, i in LABEL2ID.items()}

print("Klasser:", WING_NAMES, flush=True)
print("Train fordeling:", dict(Counter(train_labels)), flush=True)
print("Test fordeling: ", dict(Counter(test_labels)), flush=True)
print(f"Train: {len(train_texts)}  Test: {len(test_texts)}", flush=True)
print(flush=True)

# ── Klasse-vægte (ubalance) ───────────────────────────────────────────────────
from sklearn.utils.class_weight import compute_class_weight

class_weights = compute_class_weight(
    class_weight="balanced",
    classes=np.array(WING_NAMES),
    y=train_labels,
)
# Cap: forhindrer tiny klasser (fbf: 8 eks.) i at dominere gradienten
class_weights = np.clip(class_weights, 1.0 / MAX_CLASS_WEIGHT, MAX_CLASS_WEIGHT)
print("Klasse-vægte (cappet):", {w: round(float(class_weights[i]), 2) for i, w in enumerate(WING_NAMES)}, flush=True)
weight_tensor = torch.tensor(class_weights, dtype=torch.float).cuda()

# ── Tokenizer + model ─────────────────────────────────────────────────────────
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from peft import get_peft_model, LoraConfig, TaskType

tokenizer = AutoTokenizer.from_pretrained(MODEL_CACHE)
base_model = AutoModelForSequenceClassification.from_pretrained(
    MODEL_CACHE,
    num_labels=len(WING_NAMES),
    id2label=ID2LABEL,
    label2id=LABEL2ID,
    ignore_mismatched_sizes=True,
)

lora_config = LoraConfig(
    task_type=TaskType.SEQ_CLS,
    r=LORA_R,
    lora_alpha=LORA_ALPHA,
    lora_dropout=LORA_DROPOUT,
    target_modules=["query", "key", "value", "dense"],
    modules_to_save=["classifier"],
    bias="none",
)
model = get_peft_model(base_model, lora_config)
model = model.cuda()

trainable = sum(p.numel() for p in model.parameters() if p.requires_grad)
total     = sum(p.numel() for p in model.parameters())
print(f"Parametre: {total:,} total, {trainable:,} trainable ({100*trainable/total:.1f}%)", flush=True)
print(flush=True)

# ── Dataset ───────────────────────────────────────────────────────────────────
from torch.utils.data import Dataset, DataLoader

class WingDataset(Dataset):
    def __init__(self, texts, labels, tokenizer, max_len):
        self.texts    = texts
        self.label_ids = [LABEL2ID[l] for l in labels]
        self.tokenizer = tokenizer
        self.max_len   = max_len

    def __len__(self):
        return len(self.texts)

    def __getitem__(self, idx):
        enc = self.tokenizer(
            self.texts[idx],
            max_length=self.max_len,
            truncation=True,
            padding="max_length",
            return_tensors="pt",
        )
        return {
            "input_ids":      enc["input_ids"].squeeze(0),
            "attention_mask": enc["attention_mask"].squeeze(0),
            "label":          torch.tensor(self.label_ids[idx], dtype=torch.long),
        }

train_ds = WingDataset(train_texts, train_labels, tokenizer, MAX_LEN)
test_ds  = WingDataset(test_texts,  test_labels,  tokenizer, MAX_LEN)
train_dl = DataLoader(train_ds, batch_size=BATCH_SIZE, shuffle=True,  num_workers=0)
test_dl  = DataLoader(test_ds,  batch_size=BATCH_SIZE, shuffle=False, num_workers=0)

# ── Optimizer + scheduler ─────────────────────────────────────────────────────
from transformers import get_cosine_schedule_with_warmup

optimizer = torch.optim.AdamW(
    [p for p in model.parameters() if p.requires_grad],
    lr=LR,
    weight_decay=WEIGHT_DECAY,
)
total_steps  = len(train_dl) * EPOCHS
warmup_steps = int(total_steps * WARMUP_RATIO)
scheduler    = get_cosine_schedule_with_warmup(optimizer, warmup_steps, total_steps)
criterion    = torch.nn.CrossEntropyLoss(weight=weight_tensor)

# ── Træning ───────────────────────────────────────────────────────────────────
log_lines: list[str] = []

def log(msg: str) -> None:
    print(msg, flush=True)
    log_lines.append(msg)

log(f"Starter LoRA v4 træning: {EPOCHS} epochs, LR={LR}, r={LORA_R}, α={LORA_ALPHA}")
t_start = time.perf_counter()
best_test_f1 = 0.0

for epoch in range(1, EPOCHS + 1):
    model.train()
    epoch_loss = 0.0
    for batch in train_dl:
        input_ids      = batch["input_ids"].cuda()
        attention_mask = batch["attention_mask"].cuda()
        labels_batch   = batch["label"].cuda()

        optimizer.zero_grad()
        out  = model(input_ids=input_ids, attention_mask=attention_mask)
        loss = criterion(out.logits, labels_batch)
        loss.backward()
        torch.nn.utils.clip_grad_norm_(model.parameters(), 1.0)
        optimizer.step()
        scheduler.step()
        epoch_loss += loss.item()

    if epoch % 10 == 0 or epoch == 1:
        # Hurtig eval på test
        model.eval()
        preds_ids, true_ids = [], []
        with torch.no_grad():
            for batch in test_dl:
                out = model(
                    input_ids=batch["input_ids"].cuda(),
                    attention_mask=batch["attention_mask"].cuda(),
                )
                preds_ids.extend(out.logits.argmax(dim=-1).cpu().tolist())
                true_ids.extend(batch["label"].tolist())

        pred_lbls = [ID2LABEL[i] for i in preds_ids]
        true_lbls = [ID2LABEL[i] for i in true_ids]
        test_f1   = f1_score(true_lbls, pred_lbls, average="macro", zero_division=0)
        best_test_f1 = max(best_test_f1, test_f1)

        pred_dist = dict(Counter(pred_lbls))
        log(f"  Epoch {epoch:3d}/{EPOCHS}  loss={epoch_loss/len(train_dl):.4f}  test_macro_f1={test_f1:.4f}  preds={pred_dist}")

train_time = time.perf_counter() - t_start
log(f"\nTræning færdig: {train_time:.1f}s  best_test_f1={best_test_f1:.4f}")

# ── Endelig evaluering ────────────────────────────────────────────────────────
model.eval()
final_preds_ids, final_true_ids = [], []
t_inf = time.perf_counter()
with torch.no_grad():
    for batch in test_dl:
        out = model(
            input_ids=batch["input_ids"].cuda(),
            attention_mask=batch["attention_mask"].cuda(),
        )
        final_preds_ids.extend(out.logits.argmax(dim=-1).cpu().tolist())
        final_true_ids.extend(batch["label"].tolist())

inf_ms = (time.perf_counter() - t_inf) * 1000 / len(test_texts)

final_pred_lbls = [ID2LABEL[i] for i in final_preds_ids]
final_true_lbls = [ID2LABEL[i] for i in final_true_ids]

test_macro_f1 = float(f1_score(final_true_lbls, final_pred_lbls, average="macro", zero_division=0))

# Train F1 (til overfitting-check i SafetyGate)
model.eval()
train_preds_ids = []
with torch.no_grad():
    for batch in DataLoader(train_ds, batch_size=BATCH_SIZE * 2):
        out = model(
            input_ids=batch["input_ids"].cuda(),
            attention_mask=batch["attention_mask"].cuda(),
        )
        train_preds_ids.extend(out.logits.argmax(dim=-1).cpu().tolist())

train_pred_lbls = [ID2LABEL[i] for i in train_preds_ids]
train_macro_f1  = float(f1_score(train_labels, train_pred_lbls, average="macro", zero_division=0))

# Per-wing metrics
prec, rec, f1, sup = precision_recall_fscore_support(
    final_true_lbls, final_pred_lbls, labels=WING_NAMES, zero_division=0
)
per_wing = {
    wing: {
        "f1":        round(float(f1[i]), 4),
        "precision": round(float(prec[i]), 4),
        "recall":    round(float(rec[i]), 4),
        "support":   int(sup[i]),
    }
    for i, wing in enumerate(WING_NAMES)
}

log("\nEndelig evaluering (testset):")
log(f"  Train macro F1: {train_macro_f1:.4f}")
log(f"  Test  macro F1: {test_macro_f1:.4f}")
log(f"  Overfitting gap: {train_macro_f1 - test_macro_f1:+.4f}")
log("\nPer-wing F1:")
for wing in WING_NAMES:
    log(f"  {wing:<20}: {per_wing[wing]['f1']:.4f}  (support={per_wing[wing]['support']})")

# ── Gem adapter (LoRAv4Runner-kompatibelt format) ─────────────────────────────
Path(OUT_DIR).mkdir(parents=True, exist_ok=True)
model.save_pretrained(OUT_DIR)
tokenizer.save_pretrained(OUT_DIR)

# label_map.json: {"0": "wing_name", ...}
label_map = {str(i): label for i, label in ID2LABEL.items()}
Path(OUT_DIR, "label_map.json").write_text(json.dumps(label_map, indent=2, ensure_ascii=False))

# train_metrics.json: bruges af SafetyGate's overfitting-check
train_metrics = {
    "train_macro_f1": round(train_macro_f1, 4),
    "test_macro_f1":  round(test_macro_f1, 4),
}
Path(OUT_DIR, "train_metrics.json").write_text(json.dumps(train_metrics, indent=2))

log(f"\nAdapter gemt: {OUT_DIR}")

# ── Gem results_v4.json (sendes til Pi5) ──────────────────────────────────────
results = {
    "seed":              SEED,
    "test_predictions":  final_pred_lbls,
    "ground_truth":      final_true_lbls,
    "train_macro_f1":    round(train_macro_f1, 4),
    "test_macro_f1":     round(test_macro_f1, 4),
    "best_test_f1":      round(best_test_f1, 4),
    "infer_ms_per_example": round(inf_ms, 3),
    "train_time_s":      round(train_time, 1),
    "per_wing":          per_wing,
    "n_train":           len(train_texts),
    "n_test":            len(test_texts),
    "wings":             WING_NAMES,
    "lora": {
        "r":               LORA_R,
        "alpha":           LORA_ALPHA,
        "dropout":         LORA_DROPOUT,
        "target_modules":  ["query", "key", "value", "dense"],
        "modules_to_save": ["classifier"],
        "epochs":          EPOCHS,
        "lr":              LR,
    },
    "gpu":    GPU_NAME,
    "cuda":   CUDA_VER,
    "model":  "Maltehb/danish-bert-botxo + LoRA v4",
    "adapter_dir": OUT_DIR,
}
Path(RESULTS_FILE).write_text(json.dumps(results, indent=2, ensure_ascii=False))
log(f"Resultater gemt: {RESULTS_FILE}")

# Log-fil
Path(LOG_FILE).write_text("\n".join(log_lines))
log(f"Log gemt: {LOG_FILE}")
