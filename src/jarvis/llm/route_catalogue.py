"""Data-only catalogue of generic OpenAI-compatible model endpoints."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class EndpointTemplate:
    name: str
    key_env: str
    model_env: str
    base_url: str
    default_model: str = ""


ENDPOINTS = (
    EndpointTemplate("cerebras", "CEREBRAS_API_KEY", "FCC_SMOKE_MODEL_CEREBRAS", "https://api.cerebras.ai/v1"),
    EndpointTemplate("groq", "GROQ_API_KEY", "FCC_SMOKE_MODEL_GROQ", "https://api.groq.com/openai/v1", "openai/gpt-oss-20b"),
    EndpointTemplate("gemini", "GEMINI_API_KEY", "FCC_SMOKE_MODEL_GEMINI", "https://generativelanguage.googleapis.com/v1beta/openai"),
    EndpointTemplate("openrouter", "OPENROUTER_API_KEY", "FCC_SMOKE_MODEL_OPEN_ROUTER", "https://openrouter.ai/api/v1", "minimax/minimax-m3:free"),
    EndpointTemplate("nvidia-nim", "NVIDIA_NIM_API_KEY", "FCC_SMOKE_MODEL_NVIDIA_NIM", "https://integrate.api.nvidia.com/v1"),
    EndpointTemplate("deepseek", "DEEPSEEK_API_KEY", "FCC_SMOKE_MODEL_DEEPSEEK", "https://api.deepseek.com/v1"),
    EndpointTemplate("fireworks", "FIREWORKS_API_KEY", "FCC_SMOKE_MODEL_FIREWORKS", "https://api.fireworks.ai/inference/v1"),
    EndpointTemplate("github-models", "GITHUB_MODELS_TOKEN", "FCC_SMOKE_MODEL_GITHUB_MODELS", "https://models.github.ai/inference"),
    EndpointTemplate("huggingface", "HUGGINGFACE_API_KEY", "FCC_SMOKE_MODEL_HUGGINGFACE", "https://router.huggingface.co/v1", "meta-llama/Llama-3.1-8B-Instruct"),
    EndpointTemplate("kimi", "KIMI_API_KEY", "FCC_SMOKE_MODEL_KIMI", "https://api.moonshot.ai/v1"),
    EndpointTemplate("mistral", "MISTRAL_API_KEY", "FCC_SMOKE_MODEL_MISTRAL", "https://api.mistral.ai/v1", "mistral-small-latest"),
    EndpointTemplate("sambanova", "SAMBANOVA_API_KEY", "FCC_SMOKE_MODEL_SAMBANOVA", "https://api.sambanova.ai/v1"),
    EndpointTemplate("zai", "ZAI_API_KEY", "FCC_SMOKE_MODEL_ZAI", "https://api.z.ai/api/paas/v4"),
)


# Only routes that passed the capability matrix belong in automatic import
# order. Unhealthy providers remain in ENDPOINTS for later explicit probes.
FAST_ORDER = ("mistral", "groq")
CHAT_ORDER = ("openrouter", "mistral")
