"""
Infer competence scores for a model from its value string (name patterns).
Used during sync to populate the matrix for newly-discovered models.
All scores are benchmark-informed priors — callers can override via upsert_model.
"""
from __future__ import annotations


def infer_competence(model_value: str) -> dict[str, float]:
    """Return {coding, docs, reasoning, general} scores from model name patterns."""
    n = model_value.lower()

    if any(x in n for x in ("qwen2.5-coder", "qwen3-coder", "deepseek-coder",
                             "starcoder", "codestral", "codellama", "codegemma")):
        return {"coding": 0.88, "docs": 0.65, "reasoning": 0.75, "general": 0.70}

    if any(x in n for x in ("o1", "o3", "r1", "deepseek-r")):
        return {"coding": 0.85, "docs": 0.80, "reasoning": 0.93, "general": 0.88}

    if "qwen3" in n:
        return {"coding": 0.82, "docs": 0.78, "reasoning": 0.84, "general": 0.82}

    if "qwen2.5" in n or "qwen2" in n:
        return {"coding": 0.78, "docs": 0.74, "reasoning": 0.80, "general": 0.78}

    if "claude-opus" in n or "claude-4" in n:
        return {"coding": 0.90, "docs": 0.94, "reasoning": 0.92, "general": 0.93}
    if "claude-sonnet" in n:
        return {"coding": 0.87, "docs": 0.88, "reasoning": 0.88, "general": 0.88}
    if "claude-haiku" in n:
        return {"coding": 0.78, "docs": 0.75, "reasoning": 0.78, "general": 0.78}

    if "gpt-4o" in n:
        return {"coding": 0.87, "docs": 0.84, "reasoning": 0.88, "general": 0.86}
    if "gpt-4" in n:
        return {"coding": 0.85, "docs": 0.82, "reasoning": 0.87, "general": 0.85}
    if "gpt-3.5" in n:
        return {"coding": 0.72, "docs": 0.70, "reasoning": 0.72, "general": 0.72}

    if "gemini-2" in n and "flash" not in n:
        return {"coding": 0.84, "docs": 0.84, "reasoning": 0.86, "general": 0.85}
    if "gemini" in n and "flash" in n:
        return {"coding": 0.78, "docs": 0.76, "reasoning": 0.78, "general": 0.78}

    if "llama3.3" in n or "llama-3.3" in n:
        return {"coding": 0.78, "docs": 0.78, "reasoning": 0.80, "general": 0.80}
    if "llama3" in n or "llama-3" in n:
        return {"coding": 0.74, "docs": 0.74, "reasoning": 0.76, "general": 0.76}
    if "llama" in n:
        return {"coding": 0.68, "docs": 0.68, "reasoning": 0.70, "general": 0.70}

    if "mixtral" in n or "mistral-large" in n:
        return {"coding": 0.80, "docs": 0.76, "reasoning": 0.80, "general": 0.78}
    if "mistral" in n:
        return {"coding": 0.74, "docs": 0.72, "reasoning": 0.74, "general": 0.74}

    if "nemotron" in n:
        return {"coding": 0.78, "docs": 0.75, "reasoning": 0.82, "general": 0.80}

    return {"coding": 0.70, "docs": 0.68, "reasoning": 0.70, "general": 0.70}
