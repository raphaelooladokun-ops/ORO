"""LLM narrative layer -- sits on top of the deterministic flags in rules/flags.py.

Grounds the model strictly in the structured figures already computed by the
rule engine. The prompt hands over the fired flags verbatim (as JSON) and
instructs the model to reference only those numbers -- it is not given the
raw ledger, so it cannot invent figures that aren't already in the flags.
"""
import json

import config


def build_prompt(position_summary: dict, flags: list[dict]) -> tuple[str, str]:
    system = (
        "You are a terse, direct personal-finance advisor. You will be given a JSON "
        "summary of the user's current net-worth position and a JSON list of "
        "rule-based flags already computed from their ledger. Write their weekly "
        "'3 highest-leverage moves' briefing.\n\n"
        "Ground every claim strictly in the numbers provided -- never invent a "
        "figure, account name, or percentage that isn't in the input JSON. If "
        "there are fewer than 3 fired flags, cover only the flags given; do not "
        "pad with generic advice. Reference specific flags by their figures. "
        "Keep it under 200 words. No preamble."
    )
    user = (
        "Position summary:\n"
        f"{json.dumps(position_summary, indent=2, default=str)}\n\n"
        "Fired flags:\n"
        f"{json.dumps(flags, indent=2, default=str)}\n\n"
        "Write the weekly briefing."
    )
    return system, user


def generate_narrative(position_summary: dict, flags: list[dict]) -> str:
    if not config.ANTHROPIC_API_KEY:
        return (
            "Set the ANTHROPIC_API_KEY environment variable to enable the weekly "
            "narrative. The rule-based flags above are still fully computed and "
            "trustworthy without it."
        )
    if not flags:
        return "No rule flags fired this week -- nothing high-leverage to call out."

    try:
        import anthropic

        client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
        system, user = build_prompt(position_summary, flags)
        response = client.messages.create(
            model=config.ANTHROPIC_MODEL,
            max_tokens=1024,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        text = next((b.text for b in response.content if b.type == "text"), "")
        return text or "The model returned no text output."
    except Exception as exc:  # never let a narrative failure break the dashboard
        return f"Narrative generation failed ({exc}). The rule-based flags above are unaffected."
