"""Camada de IA - Anthropic Claude (default) ou OpenAI."""
from .providers import generate_completion, AIError
from .script_generator import generate_script_variants, regenerate_from_winner

__all__ = [
    "generate_completion",
    "AIError",
    "generate_script_variants",
    "regenerate_from_winner",
]
