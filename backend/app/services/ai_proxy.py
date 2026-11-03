"""Model-only routing shared by SDK and lightweight HTTP callers."""
from typing import Any


def model_proxy_url(settings: Any) -> str:
    """Model transport only; never use this override for literature sources."""
    return getattr(settings, "ai_model_proxy_url", getattr(
        settings, "ai_proxy_url", getattr(settings, "online_knowledge_proxy_url", "")
    ))
