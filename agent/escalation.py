import os
MIN_CONFIDENCE_TO_PROCEED = float(os.getenv("LOCALIZATION_MIN_CONFIDENCE","0.35"))
TIERS=["economy","standard","premium"]
TIER_MODEL_ENV={"economy":"COPILOT_MODEL_ECONOMY","standard":"COPILOT_MODEL_STANDARD","premium":"COPILOT_MODEL_PREMIUM"}
TIER_MODEL_DEFAULT={"economy":"gpt-4.1-mini","standard":"gpt-4.1","premium":"claude-sonnet-4.5"}
def model_for_tier(tier): return os.getenv(TIER_MODEL_ENV.get(tier,""), TIER_MODEL_DEFAULT.get(tier,TIER_MODEL_DEFAULT["standard"]))
def classify_complexity(loc, ticket_text):
    all_cands=loc.get("candidates",[]); confidence=loc.get("confidence",0.0)
    # Size the change by STRONG candidates only (>= half the top score).
    # Weak matches that merely share the object name must not make a
    # one-file fix look multi-file and push it to the most expensive model.
    top=max((c.get("score",0) for c in all_cands), default=0)
    # Only filter when scores are actually present (top > 0). If every
    # candidate lacks a real score, there is nothing to compare against -
    # keep the full list rather than silently emptying it.
    cands=[c for c in all_cands if c.get("score",0) >= 0.5*top] if top else list(all_cands)
    n_files=len(cands); ticket_len=len(ticket_text or "")
    n_components=len({c["path"].split("/")[-2] if "/" in c["path"] else c["path"] for c in cands[:5]})
    if confidence>=0.65 and n_files<=2 and ticket_len<600: return "simple"
    if confidence>=0.4 and n_files<=5 and n_components<=3: return "moderate"
    return "complex"
def initial_tier(complexity, confidence):
    if confidence < MIN_CONFIDENCE_TO_PROCEED: return None
    return {"simple":"economy","moderate":"standard","complex":"premium"}[complexity]
def next_tier(t):
    i=TIERS.index(t); return TIERS[i+1] if i+1<len(TIERS) else None
def plan_attempt(attempt_no, complexity, confidence, prior_tier=None, prior_failed=False, credits_used_on_ticket=0.0, ceiling=60.0):
    if credits_used_on_ticket>=ceiling:
        return {"action":"hand_to_person","reason":f"per-ticket ceiling reached ({credits_used_on_ticket:.1f}/{ceiling:.0f} credits)"}
    if attempt_no==1:
        tier=initial_tier(complexity, confidence)
        if tier is None: return {"action":"ask_for_info","reason":f"localisation confidence {confidence:.2f} below threshold {MIN_CONFIDENCE_TO_PROCEED}"}
        return {"action":"run","tier":tier,"model":model_for_tier(tier),"retry_with_feedback":False}
    if not prior_failed: return {"action":"hand_to_person","reason":"previous attempt succeeded; nothing to escalate"}
    if attempt_no==2: return {"action":"run","tier":prior_tier,"model":model_for_tier(prior_tier),"retry_with_feedback":True}
    nxt=next_tier(prior_tier)
    if nxt is None: return {"action":"hand_to_person","reason":"already at premium tier and it failed"}
    return {"action":"run","tier":nxt,"model":model_for_tier(nxt),"retry_with_feedback":True}
