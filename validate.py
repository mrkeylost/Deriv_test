# validate.py
import json, os, sys

OUTPUT_DIR = "outputs"
ERRORS = []
PASSES = []

def ok(msg):
    PASSES.append(msg)
    print(f"  ✓ {msg}")

def fail(msg):
    ERRORS.append(msg)
    print(f"  ✗ {msg}")

def load_json(filename):
    path = os.path.join(OUTPUT_DIR, filename)
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        fail(f"{filename} not found")
        return None
    except json.JSONDecodeError:
        fail(f"{filename} is not valid JSON")
        return None

print("\n" + "="*60)
print("VALIDATION REPORT")
print("="*60)

# ── 1. Required artifacts exist ───────────────────────────────────────────
print("\n[1] Checking required artifacts...")
required_files = [
    "normalized_tickets.json",
    "triage_predictions.json",
    "review_overrides.json",
    "final_queue.json",
    "queue_summary.md",
    "escalations.json",
    "llm_calls.jsonl"
]
for fname in required_files:
    path = os.path.join(OUTPUT_DIR, fname)
    if os.path.exists(path):
        ok(f"{fname} exists")
    else:
        fail(f"{fname} MISSING")

# ── 2. Load config and artifacts ─────────────────────────────────────────
print("\n[2] Loading files...")
with open("triage_config.json") as f:
    config = json.load(f)
ok("triage_config.json loaded")

with open("tickets.json") as f:
    raw_tickets = json.load(f)
ok("tickets.json loaded")

normalized   = load_json("normalized_tickets.json")
predictions  = load_json("triage_predictions.json")
overrides    = load_json("review_overrides.json")
final_queue  = load_json("final_queue.json")
escalations  = load_json("escalations.json")

allowed_cats  = config["allowed_categories"]
allowed_pris  = config["allowed_priorities"]
routing_rules = config["routing_rules"]
max_words     = config["reply_style"]["max_words"]

# ── 3. Normalization checks ───────────────────────────────────────────────
print("\n[3] Checking normalization...")
if normalized:
    raw_ids  = {t["ticket_id"] for t in raw_tickets}
    norm_ids = {t["ticket_id"] for t in normalized}
    if raw_ids == norm_ids:
        ok("All raw ticket IDs present in normalized_tickets.json")
    else:
        fail(f"ID mismatch — raw: {raw_ids}, normalized: {norm_ids}")

    for t in normalized:
        if "text_for_model" not in t:
            fail(f"{t['ticket_id']} missing text_for_model")
        elif "char_count" not in t:
            fail(f"{t['ticket_id']} missing char_count")
        elif t["char_count"] != len(t["text_for_model"]):
            fail(f"{t['ticket_id']} char_count mismatch")
        else:
            ok(f"{t['ticket_id']} normalization fields valid")

# ── 4. Every ticket has exactly one prediction ────────────────────────────
print("\n[4] Checking predictions coverage...")
if predictions and normalized:
    norm_ids = {t["ticket_id"] for t in normalized}
    pred_ids = [p["ticket_id"] for p in predictions]
    pred_id_set = set(pred_ids)

    if len(pred_ids) != len(pred_id_set):
        fail("Duplicate ticket IDs found in predictions")
    else:
        ok("No duplicate predictions")

    if norm_ids == pred_id_set:
        ok("Every ticket has exactly one prediction")
    else:
        missing = norm_ids - pred_id_set
        extra   = pred_id_set - norm_ids
        if missing: fail(f"Missing predictions for: {missing}")
        if extra:   fail(f"Extra predictions for unknown tickets: {extra}")

# ── 5. Category, priority, and routing values ─────────────────────────────
print("\n[5] Checking category/priority/routing constraints...")
if predictions:
    for p in predictions:
        tid = p["ticket_id"]
        if p["category"] not in allowed_cats:
            fail(f"{tid} invalid category: {p['category']}")
        else:
            ok(f"{tid} category '{p['category']}' is valid")

        if p["priority"] not in allowed_pris:
            fail(f"{tid} invalid priority: {p['priority']}")
        else:
            ok(f"{tid} priority '{p['priority']}' is valid")

        expected_route = routing_rules.get(p["category"])
        if p["route_to"] != expected_route:
            fail(f"{tid} route_to '{p['route_to']}' should be '{expected_route}'")
        else:
            ok(f"{tid} route_to '{p['route_to']}' is correct")

# ── 6. Reply word count ───────────────────────────────────────────────────
print("\n[6] Checking reply word limits...")
if predictions:
    for p in predictions:
        word_count = len(p["suggested_reply"].split())
        if word_count > max_words:
            fail(f"{p['ticket_id']} reply is {word_count} words (max {max_words})")
        else:
            ok(f"{p['ticket_id']} reply word count {word_count}/{max_words} ✓")

# ── 7. Overrides applied in final queue ──────────────────────────────────
print("\n[7] Checking overrides applied in final queue...")
if overrides and final_queue:
    for o in overrides:
        tid = o["ticket_id"]
        fq  = next((f for f in final_queue if f["ticket_id"] == tid), None)
        if fq is None:
            fail(f"{tid} override exists but ticket not in final queue")
            continue
        if fq["final_category"] != o["new_category"]:
            fail(f"{tid} category override not applied in final queue")
        elif fq["final_priority"] != o["new_priority"]:
            fail(f"{tid} priority override not applied in final queue")
        elif not fq["was_overridden"]:
            fail(f"{tid} was_overridden should be True")
        else:
            ok(f"{tid} override correctly applied in final queue")
elif not overrides:
    ok("No overrides to validate")

# ── 8. Final summary ──────────────────────────────────────────────────────
print("\n" + "="*60)
print(f"RESULT: {len(PASSES)} passed, {len(ERRORS)} failed")
print("="*60 + "\n")

if ERRORS:
    print("Failed checks:")
    for e in ERRORS:
        print(f"  ✗ {e}")
    sys.exit(1)
else:
    print("All checks passed. Pipeline output is valid.")
    sys.exit(0)