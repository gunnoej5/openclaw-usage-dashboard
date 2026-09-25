#!/usr/bin/env python3
"""
Model pricing table — USD per 1,000,000 tokens.

Every number here is transcribed from the vendor's own published pricing page.
Do not guess prices. If a model is missing, leave it missing: the dashboard
surfaces unpriced models as a visible warning, which is strictly better than
inventing a plausible-looking number and quietly under-reporting spend.

SOURCES
-------
Anthropic : https://platform.claude.com/docs/en/about-claude/pricing
            retrieved 2026-09-25
OpenAI    : https://developers.openai.com/api/docs/pricing
            retrieved 2026-09-25

FIELDS
------
input        Base input tokens
output       Output tokens
cacheRead    Cache hits / refreshes ("cached input" on OpenAI)
cacheWrite    5-minute cache writes (Anthropic) / cache writes (OpenAI)
cacheWrite1h  1-hour cache writes (Anthropic only; 0.0 where not offered)

CAVEATS (documented, not silently swallowed)
--------------------------------------------
1. OpenAI publishes separate long-context rates for some models above a
   272K-token context. These entries use the SHORT-context standard rates.
   Long-context requests will therefore under-report.
2. These are Standard-tier rates. Batch (~50% less), Flex, and Fast/Priority
   tiers are priced differently and are not modelled.
3. Anthropic applies a 1.1x multiplier for `inference_geo: "us"` and a 10%
   premium on regional/multi-region cloud endpoints. Not modelled.
4. Anthropic Fable 5.1 / Mythos 5.1 price cache reads at 0.025x base input,
   and Opus 5.5 at 0.05x, rather than the usual 0.1x. That is already baked
   into the absolute cacheRead numbers below.
"""

PRICING_SOURCES = {
    "anthropic": {
        "url": "https://platform.claude.com/docs/en/about-claude/pricing",
        "retrieved": "2026-09-25",
    },
    "openai": {
        "url": "https://developers.openai.com/api/docs/pricing",
        "retrieved": "2026-09-25",
        "tier": "standard, short-context (<272K)",
    },
    "lmstudio": {
        "url": "local inference — no marginal token cost",
        "retrieved": "2026-09-16",
    },
}


def _m(name, provider, model_id, input_, output, cache_read,
       cache_write, cache_write_1h=0.0):
    return {
        "name": name,
        "provider": provider,
        "modelId": model_id,
        "input": input_,
        "output": output,
        "cacheRead": cache_read,
        "cacheWrite": cache_write,
        "cacheWrite1h": cache_write_1h,
    }


# ── Anthropic ────────────────────────────────────────────────────────────────
# platform.claude.com/docs/en/about-claude/pricing  (2026-09-25)
#                              name                provider     modelId                 in     out    cacheR  cacheW  cacheW1h
_ANTHROPIC = {
    "claude-fable-5-1":   _m("Claude Fable 5.1",   "anthropic", "claude-fable-5-1",    10.0,  50.0,   0.25,  12.50,  20.0),
    "claude-mythos-5-1":  _m("Claude Mythos 5.1",  "anthropic", "claude-mythos-5-1",   10.0,  50.0,   0.25,  12.50,  20.0),
    "claude-fable-5":     _m("Claude Fable 5",     "anthropic", "claude-fable-5",      10.0,  50.0,   1.00,  12.50,  20.0),
    "claude-mythos-5":    _m("Claude Mythos 5",    "anthropic", "claude-mythos-5",     10.0,  50.0,   1.00,  12.50,  20.0),
    "claude-opus-5-5":    _m("Claude Opus 5.5",    "anthropic", "claude-opus-5-5",      4.0,  20.0,   0.20,   5.00,   8.0),
    "claude-opus-5":      _m("Claude Opus 5",      "anthropic", "claude-opus-5",        5.0,  25.0,   0.50,   6.25,  10.0),
    "claude-opus-4-8":    _m("Claude Opus 4.8",    "anthropic", "claude-opus-4-8",      5.0,  25.0,   0.50,   6.25,  10.0),
    "claude-opus-4-7":    _m("Claude Opus 4.7",    "anthropic", "claude-opus-4-7",      5.0,  25.0,   0.50,   6.25,  10.0),
    "claude-opus-4-6":    _m("Claude Opus 4.6",    "anthropic", "claude-opus-4-6",      5.0,  25.0,   0.50,   6.25,  10.0),
    "claude-opus-4-5":    _m("Claude Opus 4.5",    "anthropic", "claude-opus-4-5",      5.0,  25.0,   0.50,   6.25,  10.0),
    # Opus 4.1 / 4 are retired but priced 3x higher — keep them exact.
    "claude-opus-4-1":    _m("Claude Opus 4.1",    "anthropic", "claude-opus-4-1",     15.0,  75.0,   1.50,  18.75,  30.0),
    "claude-opus-4":      _m("Claude Opus 4",      "anthropic", "claude-opus-4",       15.0,  75.0,   1.50,  18.75,  30.0),
    "claude-sonnet-5":    _m("Claude Sonnet 5",    "anthropic", "claude-sonnet-5",      2.0,  10.0,   0.20,   2.50,   4.0),
    "claude-sonnet-4-6":  _m("Claude Sonnet 4.6",  "anthropic", "claude-sonnet-4-6",    3.0,  15.0,   0.30,   3.75,   6.0),
    "claude-sonnet-4-5":  _m("Claude Sonnet 4.5",  "anthropic", "claude-sonnet-4-5",    3.0,  15.0,   0.30,   3.75,   6.0),
    "claude-sonnet-4":    _m("Claude Sonnet 4",    "anthropic", "claude-sonnet-4",      3.0,  15.0,   0.30,   3.75,   6.0),
    "claude-haiku-4-5":   _m("Claude Haiku 4.5",   "anthropic", "claude-haiku-4-5",     1.0,   5.0,   0.10,   1.25,   2.0),
    "claude-haiku-3-5":   _m("Claude Haiku 3.5",   "anthropic", "claude-haiku-3-5",     0.80,  4.0,   0.08,   1.00,   1.60),
}
# Dated snapshot ids resolve to their base model's pricing.
_ANTHROPIC["claude-haiku-4-5-20251001"] = dict(
    _ANTHROPIC["claude-haiku-4-5"], modelId="claude-haiku-4-5-20251001")
_ANTHROPIC["claude-opus-4-5-20251101"] = dict(
    _ANTHROPIC["claude-opus-4-5"], modelId="claude-opus-4-5-20251101")
_ANTHROPIC["claude-sonnet-4-5-20250929"] = dict(
    _ANTHROPIC["claude-sonnet-4-5"], modelId="claude-sonnet-4-5-20250929")


# ── OpenAI ───────────────────────────────────────────────────────────────────
# developers.openai.com/api/docs/pricing  (2026-09-25), standard tier,
# short-context column. "-" in the vendor table means not charged -> 0.0.
_OPENAI = {
    "gpt-6-astra":    _m("GPT-6 Astra",    "openai", "gpt-6-astra",    10.0,  50.0,  1.00,   12.50),
    "gpt-6-sol":      _m("GPT-6 Sol",      "openai", "gpt-6-sol",       2.0,  10.0,  0.20,    2.50),
    "gpt-6-luna":     _m("GPT-6 Luna",     "openai", "gpt-6-luna",      0.10,  0.50, 0.01,    0.125),
    "gpt-5.6-sol":    _m("GPT-5.6 Sol",    "openai", "gpt-5.6-sol",     4.0,  20.0,  0.40,    5.00),
    "gpt-5.6-terra":  _m("GPT-5.6 Terra",  "openai", "gpt-5.6-terra",   2.0,  12.0,  0.20,    2.50),
    "gpt-5.6-luna":   _m("GPT-5.6 Luna",   "openai", "gpt-5.6-luna",    0.20,  1.20, 0.02,    0.25),
    "gpt-5.6-cyber":  _m("GPT-5.6 Cyber",  "openai", "gpt-5.6-cyber",  12.50, 75.0,  1.25,   15.625),
    "gpt-5.5-cyber":  _m("GPT-5.5 Cyber",  "openai", "gpt-5.5-cyber",  12.50, 75.0,  1.25,    0.0),
    "gpt-5.5":        _m("GPT-5.5",        "openai", "gpt-5.5",         5.0,  30.0,  0.50,    0.0),
    "gpt-5.5-pro":    _m("GPT-5.5 Pro",    "openai", "gpt-5.5-pro",    30.0, 180.0,  0.0,     0.0),
    "gpt-5.4":        _m("GPT-5.4",        "openai", "gpt-5.4",         2.50, 15.0,  0.25,    0.0),
    "gpt-5.4-mini":   _m("GPT-5.4 mini",   "openai", "gpt-5.4-mini",    0.75,  4.50, 0.075,   0.0),
    "gpt-5.4-nano":   _m("GPT-5.4 nano",   "openai", "gpt-5.4-nano",    0.20,  1.25, 0.02,    0.0),
    "gpt-5.4-pro":    _m("GPT-5.4 Pro",    "openai", "gpt-5.4-pro",    30.0, 180.0,  0.0,     0.0),
    "gpt-5.3-codex":  _m("GPT-5.3 Codex",  "openai", "gpt-5.3-codex",   1.75, 14.0,  0.175,   0.0),
    "gpt-5.2":        _m("GPT-5.2",        "openai", "gpt-5.2",         1.75, 14.0,  0.175,   0.0),
    "gpt-5.2-pro":    _m("GPT-5.2 Pro",    "openai", "gpt-5.2-pro",    21.0, 168.0,  0.0,     0.0),
    "gpt-5.1":        _m("GPT-5.1",        "openai", "gpt-5.1",         1.25, 10.0,  0.125,   0.0),
    "gpt-5":          _m("GPT-5",          "openai", "gpt-5",           1.25, 10.0,  0.125,   0.0),
    "gpt-5-mini":     _m("GPT-5 mini",     "openai", "gpt-5-mini",      0.25,  2.00, 0.025,   0.0),
    "gpt-5-nano":     _m("GPT-5 nano",     "openai", "gpt-5-nano",      0.05,  0.40, 0.005,   0.0),
    "gpt-5-pro":      _m("GPT-5 Pro",      "openai", "gpt-5-pro",      15.0, 120.0,  0.0,     0.0),
    "gpt-4.1":        _m("GPT-4.1",        "openai", "gpt-4.1",         2.00,  8.00, 0.50,    0.0),
    "gpt-4.1-mini":   _m("GPT-4.1 mini",   "openai", "gpt-4.1-mini",    0.40,  1.60, 0.10,    0.0),
    "gpt-4.1-nano":   _m("GPT-4.1 nano",   "openai", "gpt-4.1-nano",    0.10,  0.40, 0.025,   0.0),
    "gpt-4o":         _m("GPT-4o",         "openai", "gpt-4o",          2.50, 10.0,  1.25,    0.0),
    "gpt-4o-2024-05-13": _m("GPT-4o (2024-05-13)", "openai", "gpt-4o-2024-05-13", 5.0, 15.0, 0.0, 0.0),
    "gpt-4o-mini":    _m("GPT-4o mini",    "openai", "gpt-4o-mini",     0.15,  0.60, 0.075,   0.0),
    "o4-mini":        _m("o4-mini",        "openai", "o4-mini",         1.10,  4.40, 0.275,   0.0),
    "o3":             _m("o3",             "openai", "o3",              2.00,  8.00, 0.50,    0.0),
    "o3-mini":        _m("o3-mini",        "openai", "o3-mini",         1.10,  4.40, 0.55,    0.0),
    "o3-pro":         _m("o3-pro",         "openai", "o3-pro",         20.0,  80.0,  0.0,     0.0),
    "o1":             _m("o1",             "openai", "o1",             15.0,  60.0,  7.50,    0.0),
    "o1-pro":         _m("o1-pro",         "openai", "o1-pro",        150.0, 600.0,  0.0,     0.0),
}
# Daybreak aliases currently point at these models (vendor note, 2026-09-25).
_OPENAI["gpt-daybreak-blue-latest"] = dict(
    _OPENAI["gpt-5.6-sol"], modelId="gpt-daybreak-blue-latest")
_OPENAI["gpt-daybreak-red-latest"] = dict(
    _OPENAI["gpt-5.6-cyber"], modelId="gpt-daybreak-red-latest")
# gpt-4o dated snapshots bill at gpt-4o rates.
for _snap in ("gpt-4o-2024-08-06", "gpt-4o-2024-11-20"):
    _OPENAI[_snap] = dict(_OPENAI["gpt-4o"], modelId=_snap)

# Deliberately NOT priced (absent from the vendor table on the retrieval date):
#   openai/gpt-5.6        — only sol/terra/luna/cyber variants are published
# Leaving these out makes them show up as "unpriced", not as $0.00.


# ── Local models ─────────────────────────────────────────────────────────────
# Local inference has no marginal token cost. Tracked at $0 for token volume
# visibility only.
_LOCAL_IDS = [
    ("qwen/qwen3-4b",                              "Qwen3-4B (nano)"),
    ("qwen/qwen3-14b",                             "Qwen3-14B (local)"),
    ("qwen/qwen3-coder-30b",                       "Qwen3-Coder-30B (coder)"),
    ("qwen/qwen3-coder-next",                      "Qwen3-Coder-Next (coder)"),
    ("google/gemma-4-e4b",                         "Gemma-4 E4B (gemma)"),
    # LM Studio appends ":<n>" when several instances of a model are loaded.
    ("google/gemma-4-e4b:2",                       "Gemma-4 E4B (gemma, instance 2)"),
    ("deepseek/deepseek-r1-0528-qwen3-8b",         "DeepSeek-R1 Qwen3-8B (qwen)"),
    ("lmstudio-community/qwen3-coder-30b-a3b-instruct-gguf",
                                                   "Qwen3-Coder-30B-A3B (coder)"),
]
_LMSTUDIO = {
    mid: _m(label, "lmstudio", mid, 0.0, 0.0, 0.0, 0.0)
    for mid, label in _LOCAL_IDS
}


def build_pricing() -> dict:
    """Return {"provider/modelId": entry} for every known model."""
    table: dict = {}
    for provider, models in (
        ("anthropic", _ANTHROPIC),
        ("openai", _OPENAI),
        ("lmstudio", _LMSTUDIO),
    ):
        for model_id, entry in models.items():
            table[f"{provider}/{model_id}"] = entry
    return table


MODEL_PRICING = build_pricing()
