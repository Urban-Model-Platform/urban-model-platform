# ump/core/interfaces/http_client.py
from abc import ABC, abstractmethod
from typing import Any, Dict

class HttpClientPort(ABC):
    @abstractmethod
    async def __aenter__(self) -> "HttpClientPort":
        """Async context manager entry method"""
        pass

    @abstractmethod
    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit method"""
        pass

    @abstractmethod
    async def get(self, url: str, timeout: float | None = None, headers: Dict[str, str] | None = None) -> Dict[str, Any]:
        """Make a GET request and return JSON response.

        The timeout is optional; adapters may use an internal default ClientTimeout
        when timeout is None.
        """
        pass
    
    @abstractmethod
    async def close(self) -> None:
        """Close the HTTP client session"""
        pass

    @abstractmethod
    async def post(self, url: str, json: Dict[str, Any] | None, timeout: float | None = None, headers: Dict[str, str] | None = None) -> Dict[str, Any]:
        """Make a POST request. Returns a dict with keys: 'status' (int),
        'headers' (dict) and 'body' (parsed JSON or raw text).

        The timeout is optional; adapters may use an internal default ClientTimeout
        when timeout is None.
        """
        pass
