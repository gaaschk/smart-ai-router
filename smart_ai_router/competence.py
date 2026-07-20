"""
Infer competence scores for a model from its value string (name patterns).
Used during sync to populate the matrix for newly-discovered models.
All scores are benchmark-informed priors — callers can override via upsert_model.
"""
from __future__ import annotations


def infer_competence(model_value: str) -> dict[str, float]:
    """Return {coding, docs, reasoning, general} scores from model name patterns."""
    # Normalize version separators so "claude-opus-4.8" and "claude-opus-4-8"
    # both match the same pattern (OpenRouter uses dots, our patterns use hyphens).
    n = model_value.lower().replace(".", "-")

    # ── Specialised coders (high coding, lower general) ──────────────────────
    if any(x in n for x in ("qwen2.5-coder", "qwen3-coder", "deepseek-coder",
                             "starcoder", "codestral", "codellama", "codegemma",
                             "kimi-k2.7-code")):
        return {"coding": 0.88, "docs": 0.65, "reasoning": 0.75, "general": 0.70}

    # ── Reasoning / thinking models ──────────────────────────────────────────
    if any(x in n for x in ("o3", "o4", "deepseek-r2", "deepseek-r3",
                             "qwen3-235b", "qwen3.7")):
        return {"coding": 0.90, "docs": 0.84, "reasoning": 0.95, "general": 0.91}

    if any(x in n for x in ("o1", "r1", "deepseek-r", "qwen3-max-thinking",
                             "kimi-k2-thinking", "kimi-k3")):
        return {"coding": 0.87, "docs": 0.82, "reasoning": 0.93, "general": 0.88}

    # ── Claude ────────────────────────────────────────────────────────────────
    if any(x in n for x in ("claude-fable", "fable-5")):
        return {"coding": 0.95, "docs": 0.96, "reasoning": 0.96, "general": 0.96}
    if any(x in n for x in ("claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6")):
        return {"coding": 0.92, "docs": 0.94, "reasoning": 0.94, "general": 0.94}
    if any(x in n for x in ("claude-opus", "claude-4")):
        return {"coding": 0.90, "docs": 0.94, "reasoning": 0.92, "general": 0.93}
    if "claude-sonnet-4-6" in n:
        return {"coding": 0.88, "docs": 0.89, "reasoning": 0.89, "general": 0.89}
    if "claude-sonnet" in n:
        return {"coding": 0.87, "docs": 0.88, "reasoning": 0.88, "general": 0.88}
    if "claude-haiku-4-5" in n or "claude-haiku-5" in n:
        return {"coding": 0.80, "docs": 0.78, "reasoning": 0.80, "general": 0.82}
    if "claude-haiku" in n:  # older haiku (3, 3.5)
        return {"coding": 0.72, "docs": 0.70, "reasoning": 0.72, "general": 0.74}

    # ── GPT-5 family ──────────────────────────────────────────────────────────
    if any(x in n for x in ("gpt-5.3", "gpt-5.2", "gpt-5.1", "gpt-5-turbo")):
        return {"coding": 0.91, "docs": 0.89, "reasoning": 0.92, "general": 0.91}
    if "gpt-5" in n:
        return {"coding": 0.90, "docs": 0.88, "reasoning": 0.91, "general": 0.90}

    # ── GPT-4 family ──────────────────────────────────────────────────────────
    if "gpt-4o" in n:
        return {"coding": 0.87, "docs": 0.84, "reasoning": 0.88, "general": 0.86}
    if "gpt-4" in n:
        return {"coding": 0.85, "docs": 0.82, "reasoning": 0.87, "general": 0.85}
    if "gpt-3.5" in n:
        return {"coding": 0.72, "docs": 0.70, "reasoning": 0.72, "general": 0.72}

    # ── Gemini ────────────────────────────────────────────────────────────────
    if "gemini-3-pro" in n or "gemini-3-ultra" in n:
        return {"coding": 0.90, "docs": 0.89, "reasoning": 0.92, "general": 0.91}
    if "gemini-3" in n and "flash" not in n:
        return {"coding": 0.86, "docs": 0.85, "reasoning": 0.88, "general": 0.87}
    if "gemini-3" in n and "flash" in n:
        return {"coding": 0.80, "docs": 0.79, "reasoning": 0.81, "general": 0.80}
    if "gemini-2" in n and "flash" not in n:
        return {"coding": 0.84, "docs": 0.84, "reasoning": 0.86, "general": 0.85}
    if "gemini" in n and "flash" in n:
        return {"coding": 0.78, "docs": 0.76, "reasoning": 0.78, "general": 0.78}

    # ── Deepseek ──────────────────────────────────────────────────────────────
    if any(x in n for x in ("deepseek-v3", "deepseek-v4")):
        return {"coding": 0.88, "docs": 0.83, "reasoning": 0.88, "general": 0.86}
    if "deepseek" in n:
        return {"coding": 0.85, "docs": 0.78, "reasoning": 0.84, "general": 0.80}

    # ── Kimi / Moonshot ───────────────────────────────────────────────────────
    if any(x in n for x in ("kimi-k2", "kimi-k3")):
        return {"coding": 0.87, "docs": 0.82, "reasoning": 0.88, "general": 0.85}

    # ── Qwen ──────────────────────────────────────────────────────────────────
    if "qwen3.7" in n or "qwen3-max" in n:
        return {"coding": 0.85, "docs": 0.81, "reasoning": 0.88, "general": 0.84}
    if "qwen3" in n:
        return {"coding": 0.82, "docs": 0.78, "reasoning": 0.84, "general": 0.82}
    if "qwen2.5" in n or "qwen2" in n:
        return {"coding": 0.78, "docs": 0.74, "reasoning": 0.80, "general": 0.78}

    # ── Llama ─────────────────────────────────────────────────────────────────
    if "llama3.3" in n or "llama-3.3" in n or "llama3:70b" in n or "llama-3.1:70b" in n:
        return {"coding": 0.78, "docs": 0.78, "reasoning": 0.80, "general": 0.80}
    if "llama3" in n or "llama-3" in n:
        return {"coding": 0.74, "docs": 0.74, "reasoning": 0.76, "general": 0.76}
    if "llama" in n:
        return {"coding": 0.68, "docs": 0.68, "reasoning": 0.70, "general": 0.70}

    # ── Mistral ───────────────────────────────────────────────────────────────
    if "mixtral" in n or "mistral-large" in n:
        return {"coding": 0.80, "docs": 0.76, "reasoning": 0.80, "general": 0.78}
    if "mistral" in n:
        return {"coding": 0.74, "docs": 0.72, "reasoning": 0.74, "general": 0.74}

    # ── Nvidia ────────────────────────────────────────────────────────────────
    if "nemotron" in n:
        return {"coding": 0.78, "docs": 0.75, "reasoning": 0.82, "general": 0.80}

    # ── Default ───────────────────────────────────────────────────────────────
    return {"coding": 0.70, "docs": 0.68, "reasoning": 0.70, "general": 0.70}
