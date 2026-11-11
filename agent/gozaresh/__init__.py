"""Daily market report (/gozaresh). Self-contained: shares nothing with posts, sources or the LLM writer
except the Telegram application object and the owner list."""
from .handlers import register

__all__ = ["register"]
