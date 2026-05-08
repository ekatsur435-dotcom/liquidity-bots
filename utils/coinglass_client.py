"""
Coinglass client proxy — utils/coinglass_client.py
Перенаправляет импорт из api/coinglass_client.py
"""
from api.coinglass_client import CoinglassClient  # noqa: F401
__all__ = ["CoinglassClient"]
