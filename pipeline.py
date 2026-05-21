# pipeline.py
import json, os, hashlib, datetime
from groq import Groq
from dotenv import load_dotenv
import os

load_dotenv()

# ── Config ──────────────────────────────────────────────────────────────
API_KEY = os.getenv("GROQ_API_KEY")
OUTPUT_DIR = "outputs"
os.makedirs(OUTPUT_DIR, exist_ok=True)

client = Groq(api_key=API_KEY)
MODEL = "llama-3.3-70b-versatile"

STAGE = "INIT"

def log(stage, msg):
    print(f"[{stage}] {msg}")

def save(filename, data):
    path = os.path.join(OUTPUT_DIR, filename)
    with open(path, "w") as f:
        json.dump(data, f, indent=2)
    return path

# ── Stage: INPUTS_LOADED ─────────────────────────────────────────────────
STAGE = "INPUTS_LOADED"
with open("tickets.json") as f:
    tickets = json.load(f)

with open("triage_config.json") as f:
    config = json.load(f)

log(STAGE, f"Loaded {len(tickets)} tickets and config")

# ── Stage: TICKETS_NORMALIZED ────────────────────────────────────────────
STAGE = "TICKETS_NORMALIZED"

normalized = []
for t in tickets:
    subject = t["subject"].strip()
    message = t["message"].strip()
    text_for_model = f"Subject: {subject}\nMessage: {message}"
    normalized.append({
        "ticket_id":     t["ticket_id"],
        "subject":       subject,
        "message":       message,
        "channel":       t.get("channel", ""),
        "created_at":    t.get("created_at", ""),
        "text_for_model": text_for_model,
        "char_count":    len(text_for_model)
    })

save("normalized_tickets.json", normalized)
log(STAGE, f"Normalized {len(normalized)} tickets → outputs/normalized_tickets.json")

# ── Stage: TRIAGE_PREDICTED ──────────────────────────────────────────────
STAGE = "TRIAGE_PREDICTED"

tickets_text = "\n\n".join(
    f"[{t['ticket_id']}]\n{t['text_for_model']}" for t in normalized
)

prompt = f"""
You are a support triage assistant. Classify each ticket below.

ALLOWED CATEGORIES: {config["allowed_categories"]}
ALLOWED PRIORITIES: {config["allowed_priorities"]}
ROUTING RULES: {json.dumps(config["routing_rules"])}
REPLY STYLE: {json.dumps(config["reply_style"])}

For each ticket return a JSON array. Each item must have:
- ticket_id (string)
- category (must be one of allowed_categories)
- priority (must be one of allowed_priorities)
- reason (short string, why you chose this category/priority)
- suggested_reply (follow reply_style tone, max {config["reply_style"]["max_words"]} words)
- route_to (must match routing_rules for chosen category)
- confidence (float 0.0 to 1.0)

Return ONLY a valid JSON array. No markdown, no explanation.

TICKETS:
{tickets_text}
"""

# Log the LLM call
prompt_hash = hashlib.md5(prompt.encode()).hexdigest()
llm_log = {
    "stage": STAGE,
    "timestamp": datetime.datetime.now(datetime.timezone.utc).isoformat(),
    "provider": "groq",
    "model": MODEL,
    "prompt_hash": prompt_hash,
    "input_artifacts": ["normalized_tickets.json", "triage_config.json"],
    "output_artifact": "triage_predictions.json"
}
llm_log_path = os.path.join(OUTPUT_DIR, "llm_calls.jsonl")
with open(llm_log_path, "a") as f:
    f.write(json.dumps(llm_log) + "\n")

# Call Gemini
log(STAGE, "Calling Groq API...")
response = client.chat.completions.create(
    model=MODEL,
    messages=[{"role": "user", "content": prompt}]
)
raw = response.choices[0].message.content.strip()

# Parse with fallback per ticket
try:
    predictions = json.loads(raw)
except json.JSONDecodeError:
    log(STAGE, "WARNING: JSON parse failed, attempting cleanup...")
    raw_clean = raw.replace("```json", "").replace("```", "").strip()
    try:
        predictions = json.loads(raw_clean)
    except:
        log(STAGE, "ERROR: Could not parse LLM response. Falling back all tickets to 'other'.")
        predictions = []

# Validate + repair each prediction
allowed_cats  = config["allowed_categories"]
allowed_pris  = config["allowed_priorities"]
routing_rules = config["routing_rules"]
valid_predictions = []

predicted_ids = {p["ticket_id"] for p in predictions}

for t in normalized:
    # Find matching prediction
    pred = next((p for p in predictions if p["ticket_id"] == t["ticket_id"]), None)

    if pred is None:
        log(STAGE, f"WARNING: No prediction for {t['ticket_id']}, using fallback")
        pred = {
            "ticket_id": t["ticket_id"],
            "category": "other",
            "priority": "normal",
            "reason": "Fallback: no prediction returned by model",
            "suggested_reply": "Thank you for reaching out. Our team will review your request shortly.",
            "route_to": routing_rules["other"],
            "confidence": 0.0
        }

    # Enforce allowed values
    if pred.get("category") not in allowed_cats:
        pred["category"] = "other"
    if pred.get("priority") not in allowed_pris:
        pred["priority"] = "normal"

    # Always recompute route_to from config (never trust the model)
    pred["route_to"] = routing_rules[pred["category"]]

    valid_predictions.append(pred)

save("triage_predictions.json", valid_predictions)
log(STAGE, f"Saved {len(valid_predictions)} predictions → outputs/triage_predictions.json")

# ── Stage: HUMAN_REVIEW_COMPLETE ─────────────────────────────────────────
STAGE = "HUMAN_REVIEW_COMPLETE"

print("\n" + "="*60)
print("TRIAGE PREDICTIONS — HUMAN REVIEW")
print("="*60)
for p in valid_predictions:
    print(f"\n  Ticket : {p['ticket_id']}")
    print(f"  Category : {p['category']}")
    print(f"  Priority : {p['priority']}")
    print(f"  Confidence : {p['confidence']}")
    print(f"  Reason : {p['reason']}")
print("\n" + "="*60)
print("Enter overrides as: ticket_id,category,priority")
print("Example: T-1001,billing_issue,urgent")
print("Press Enter on an empty line when done.")
print("="*60 + "\n")

overrides = []
while True:
    line = input("Override: ").strip()
    if not line:
        break
    parts = line.split(",")
    if len(parts) != 3:
        print("  Invalid format. Use: ticket_id,category,priority")
        continue
    tid, new_cat, new_pri = [p.strip() for p in parts]

    if new_cat not in allowed_cats:
        print(f"  Invalid category. Allowed: {allowed_cats}")
        continue
    if new_pri not in allowed_pris:
        print(f"  Invalid priority. Allowed: {allowed_pris}")
        continue

    match = next((p for p in valid_predictions if p["ticket_id"] == tid), None)
    if not match:
        print(f"  Ticket {tid} not found.")
        continue

    overrides.append({
        "ticket_id":    tid,
        "old_category": match["category"],
        "new_category": new_cat,
        "old_priority": match["priority"],
        "new_priority": new_pri
    })

    # Apply override immediately to valid_predictions
    match["category"] = new_cat
    match["priority"] = new_pri
    match["route_to"] = routing_rules[new_cat]
    print(f"  ✓ Override applied for {tid}")

save("review_overrides.json", overrides)
log(STAGE, f"{len(overrides)} override(s) saved → outputs/review_overrides.json")

# ── Stage: FINAL_QUEUE_GENERATED ─────────────────────────────────────────
STAGE = "FINAL_QUEUE_GENERATED"

overridden_ids = {o["ticket_id"] for o in overrides}
final_queue = []
for p in valid_predictions:
    final_queue.append({
        "ticket_id":      p["ticket_id"],
        "final_category": p["category"],
        "final_priority": p["priority"],
        "final_route_to": p["route_to"],
        "suggested_reply": p["suggested_reply"],
        "was_overridden":  p["ticket_id"] in overridden_ids
    })

save("final_queue.json", final_queue)
log(STAGE, f"Saved final queue → outputs/final_queue.json")

# ── Queue Summary ─────────────────────────────────────────────────────────
from collections import Counter

cat_counts  = Counter(f["final_category"] for f in final_queue)
pri_counts  = Counter(f["final_priority"] for f in final_queue)
route_counts = Counter(f["final_route_to"] for f in final_queue)
overridden   = [f for f in final_queue if f["was_overridden"]]

summary_lines = [
    "# Queue Summary\n",
    f"**Total tickets:** {len(final_queue)}\n",
    "## By Category",
    *[f"- {k}: {v}" for k, v in cat_counts.items()],
    "\n## By Priority",
    *[f"- {k}: {v}" for k, v in pri_counts.items()],
    "\n## By Destination Queue",
    *[f"- {k}: {v}" for k, v in route_counts.items()],
    "\n## Overridden Tickets",
]
if overridden:
    for f in overridden:
        o = next(x for x in overrides if x["ticket_id"] == f["ticket_id"])
        summary_lines.append(
            f"- {f['ticket_id']}: {o['old_category']}/{o['old_priority']}"
            f" → {f['final_category']}/{f['final_priority']}"
        )
else:
    summary_lines.append("- None")

summary_path = os.path.join(OUTPUT_DIR, "queue_summary.md")
with open(summary_path, "w") as f:
    f.write("\n".join(summary_lines))
log(STAGE, "Saved queue summary → outputs/queue_summary.md")

# ── Escalations ───────────────────────────────────────────────────────────
escalations = []
for p in valid_predictions:
    reasons = []
    if p["category"] == "other":
        reasons.append("category is 'other'")
    if float(p.get("confidence", 1.0)) < 0.60:
        reasons.append(f"low confidence ({p.get('confidence')})")
    if reasons:
        escalations.append({
            "ticket_id": p["ticket_id"],
            "category":  p["category"],
            "priority":  p["priority"],
            "escalation_reasons": reasons
        })

save("escalations.json", escalations)
log(STAGE, f"{len(escalations)} ticket(s) flagged → outputs/escalations.json")

# ── Done ──────────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("PIPELINE COMPLETE — all artifacts saved to outputs/")
print("="*60)