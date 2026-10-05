"""Model resolution via Copilot CLI's built-in auto-routing (Issue C10 fix).

WHY THIS EXISTS:
Explicit --model names (gpt-5.4, claude-sonnet-4.6, etc.) kept failing with
"is not available", even for names taken straight from the live `/model`
picker - almost certainly because org/plan policy restricts which exact
model names the --model flag accepts non-interactively, independent of
what the interactive picker displays. On top of that, GitHub periodically
renames/retires model names with no non-interactive way to list current
ones (open upstream request: github/copilot-cli#700). Any hardcoded name,
or list of names, is fragile for both reasons.

THE FIX:
`copilot --help` documents a built-in alternative: `--model auto` lets
Copilot CLI pick a currently-valid model itself, and `--auto-tier
<preference>` (efficiency | balance | intelligence) steers that choice by
cost/capability trade-off. This maps 1:1 onto escalation.py's existing
economy/standard/premium tiers, so "pick a model based on defect
complexity" (the actual goal) is delegated to Copilot's own routing
instead of a name list we'd have to keep updating by hand.

No fallback loop, no cache file, no candidate list to maintain. If a
specific model is pinned explicitly via AGENT_MODEL_NAME or
COPILOT_CLI_MODEL in .env, that is used as an exact --model value instead
(auto-tier is not applicable in that case).
"""

AUTO_TIER_FOR_ESCALATION_TIER = {
    "economy": "efficiency",
    "standard": "balance",
    "premium": "intelligence",
}


def auto_tier_for(escalation_tier):
    """Map an escalation.py tier (economy/standard/premium) to the
    --auto-tier preference Copilot CLI expects. Falls back to 'balance'
    for an unrecognised tier rather than failing the run."""
    return AUTO_TIER_FOR_ESCALATION_TIER.get(escalation_tier, "balance")


def model_cli_args(escalation_tier, explicit_model=None):
    """Build the --model/--auto-tier arguments to append to the copilot
    command. Returns a list suitable for `cmd +=`."""
    if explicit_model:
        return ["--model", explicit_model]
    if escalation_tier:
        return ["--model", "auto", "--auto-tier", auto_tier_for(escalation_tier)]
    return ["--model", "auto"]


def describe(escalation_tier, explicit_model=None):
    """Human-readable line for the console, matching model_cli_args()."""
    if explicit_model:
        return f"{explicit_model} (explicit .env override)"
    if escalation_tier:
        return f"auto (--auto-tier {auto_tier_for(escalation_tier)}, from {escalation_tier} tier)"
    return "auto (no tier routed, CLI default auto-tier)"
