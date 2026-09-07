"""Data-only catalogue of generic OpenAI-compatible model endpoints."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EndpointTemplate:
    name: str
    key_env: str
    model_env: str
    base_url: str


ENDPOINTS = (
    EndpointTemplate("gemini", "GEMINI_API_KEY", "FCC_SMOKE_MODEL_GEMINI", "https://generativelanguage.googleapis.com/v1beta/openai"),
    EndpointTemplate("openrouter", "OPENROUTER_API_KEY", "FCC_SMOKE_MODEL_OPEN_ROUTER", "https://openrouter.ai/api/v1"),
    EndpointTemplate("deepseek", "DEEPSEEK_API_KEY", "FCC_SMOKE_MODEL_DEEPSEEK", "https://api.deepseek.com/v1"),
    EndpointTemplate("fireworks", "FIREWORKS_API_KEY", "FCC_SMOKE_MODEL_FIREWORKS", "https://api.fireworks.ai/inference/v1"),
    EndpointTemplate("github-models", "GITHUB_MODELS_TOKEN", "FCC_SMOKE_MODEL_GITHUB_MODELS", "https://models.github.ai/inference"),
    EndpointTemplate("huggingface", "HUGGINGFACE_API_KEY", "FCC_SMOKE_MODEL_HUGGINGFACE", "https://router.huggingface.co/v1"),
    EndpointTemplate("kimi", "KIMI_API_KEY", "FCC_SMOKE_MODEL_KIMI", "https://api.moonshot.ai/v1"),
    EndpointTemplate("mistral", "MISTRAL_API_KEY", "FCC_SMOKE_MODEL_MISTRAL", "https://api.mistral.ai/v1"),
    EndpointTemplate("sambanova", "SAMBANOVA_API_KEY", "FCC_SMOKE_MODEL_SAMBANOVA", "https://api.sambanova.ai/v1"),
    EndpointTemplate("zai", "ZAI_API_KEY", "FCC_SMOKE_MODEL_ZAI", "https://api.z.ai/api/paas/v4"),
)


FAST_ORDER = ("gemini", "openrouter")
CHAT_ORDER = ("gemini", "openrouter")
