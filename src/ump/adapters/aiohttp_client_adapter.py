# ump/adapters/aiohttp_client_adapter.py
import asyncio
import json
from typing import Any, Dict, Optional

import aiohttp

from ump.core.exceptions import OGCExceptionResponse, OGCProcessException
from ump.core.interfaces.http_client import HttpClientPort
from ump.core.settings import logger


class AioHttpClientAdapter(HttpClientPort):
    def __init__(self):
        self._session: Optional[aiohttp.ClientSession] = None
        # Default client timeout configuration for individual requests.
        # The adapter defines per-field timeouts at init time so callers
        # don't need to construct ClientTimeout objects themselves.
        # These defaults can be tuned if you want longer polling windows.
        self._default_total: float = 10.0
        self._default_sock_read: float = 10.0
        self._default_sock_connect: float = 5.0
        # Pre-built adapter ClientTimeout to use when callers do not provide a timeout
        self._default_client_timeout = aiohttp.ClientTimeout(
            total=self._default_total,
            sock_read=self._default_sock_read,
            sock_connect=self._default_sock_connect,
        )

    async def __aenter__(self):
        """Async context manager entry"""
        self._session = aiohttp.ClientSession()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """Async context manager exit"""
        await self.close()
        return False

    async def get(
        self,
        url: str,
        timeout: float | None = None,
        headers: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        if self._session is None:
            raise RuntimeError(
                "HTTP client not initialized. Use 'async with' context manager."
            )
        # Use provided timeout (total seconds) or fall back to adapter default
        if timeout is None:
            client_timeout = self._default_client_timeout
        else:
            # Keep adapter-level sock_read/sock_connect values but apply provided total
            client_timeout = aiohttp.ClientTimeout(
                total=timeout,
                sock_read=self._default_sock_read,
                sock_connect=self._default_sock_connect,
            )

        return await self._fetch_json(
            url,
            timeout=client_timeout,
            raise_for_status=True,
            headers=headers or {},
        )

    async def _fetch_json(
        self, url: str, raise_for_status: bool = False, **kwargs
    ) -> Dict[str, Any]:
        """
        Fetch JSON from URL with OGC API-specific error handling.

        Translates HTTP/network errors into domain-specific OGCProcessException.
        """
        if self._session is None:
            raise RuntimeError(
                "HTTP client not initialized. Use 'async with' context manager."
            )

        try:
            async with self._session.get(url, **kwargs) as response:
                try:
                    # Attempt to parse JSON from the response
                    response_data = await response.json()
                except (
                    aiohttp.ContentTypeError,
                    json.JSONDecodeError,
                    ValueError,
                ) as json_error:
                    # Response isn't JSON; log a snippet and raise domain error
                    response_text = await response.text()
                    logger.error(
                        "Invalid JSON response from remote service. URL: %s, Content: %s",
                        url,
                        response_text[:500],
                    )
                    raise OGCProcessException(
                        OGCExceptionResponse(
                            type="about:blank",
                            title="Invalid Response Content",
                            status=502,
                            detail=(
                                "The response from the remote service was not valid JSON"
                                f": '{response_text[:100]}'"
                            ),
                            instance=None,
                        )
                    )

                # Optionally raise for HTTP error status codes
                if raise_for_status:
                    response.raise_for_status()

                return response_data

        except OGCProcessException:
            # Bubble up domain-specific errors generated earlier (e.g., invalid JSON)
            raise

        except asyncio.TimeoutError:
            logger.error("Timeout when requesting remote service. URL: %s", url)
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Upstream Timeout",
                    status=504,
                    detail="The request to the remote service timed out.",
                    instance=None,
                )
            )

        except aiohttp.ClientResponseError as client_response_error:
            if client_response_error.status == 401:
                logger.warning(
                    "Authentication failed when requesting remote service. URL: %s, Error: %s",
                    url,
                    str(client_response_error),
                )
                raise OGCProcessException(
                    OGCExceptionResponse(
                        type="about:blank",
                        title="Authentication Failed",
                        status=401,
                        detail="Authentication with the remote service failed.",
                        instance=None,
                    )
                )

            logger.error(
                "HTTP error when requesting remote service. URL: %s, Status: %s, Error: %s",
                url,
                client_response_error.status,
                str(client_response_error),
            )
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Upstream HTTP Error",
                    status=client_response_error.status,
                    detail=f"The remote service returned an HTTP error: {client_response_error.status}",
                    instance=None,
                )
            )

        except aiohttp.ClientError as client_error:
            logger.error(
                "Connection error when requesting remote service. URL: %s, Error: %s",
                url,
                str(client_error),
            )
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Upstream Connection Error",
                    status=502,
                    detail="There was a connection error with the remote service.",
                    instance=None,
                )
            )

        except Exception as unexpected_error:
            logger.error(
                "Unexpected error for remote service. URL: %s, Error: %s",
                url,
                str(unexpected_error),
            )
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Internal Server Error",
                    status=500,
                    detail="An unexpected error occurred while processing your request.",
                    instance=None,
                )
            )

    async def close(self) -> None:
        """Close the session"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None

    async def get_content(
        self,
        url: str,
        timeout: float | None = None,
        headers: Dict[str, str] | None = None,
    ) -> tuple[bytes, str]:
        """Fetch URL, returning (body_bytes, content_type).

        Never attempts JSON parsing. The Content-Type from the remote response
        header is returned verbatim and must be forwarded to the client.
        """
        if self._session is None:
            raise RuntimeError(
                "HTTP client not initialized. Use 'async with' context manager."
            )
        client_timeout = (
            self._default_client_timeout
            if timeout is None
            else aiohttp.ClientTimeout(
                total=timeout,
                sock_read=self._default_sock_read,
                sock_connect=self._default_sock_connect,
            )
        )
        try:
            async with self._session.get(
                url, timeout=client_timeout, headers=headers or {}
            ) as resp:
                resp.raise_for_status()
                body = await resp.read()
                content_type = resp.content_type or "application/octet-stream"
                return body, content_type
        except OGCProcessException:
            raise
        except asyncio.TimeoutError:
            logger.error("Timeout fetching content from %s", url)
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Upstream Timeout",
                    status=504,
                    detail="The request to the remote service timed out.",
                    instance=None,
                )
            )
        except aiohttp.ClientResponseError as exc:
            logger.error("HTTP error fetching content from %s: %s", url, exc.status)
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Upstream HTTP Error",
                    status=exc.status,
                    detail=f"The remote service returned HTTP {exc.status}.",
                    instance=None,
                )
            )
        except aiohttp.ClientError as exc:
            logger.error("Connection error fetching content from %s: %s", url, exc)
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Upstream Connection Error",
                    status=502,
                    detail="There was a connection error with the remote service.",
                    instance=None,
                )
            )

    async def post(
        self,
        url: str,
        json: Dict[str, Any] | None,
        timeout: float | None = None,
        headers: Dict[str, str] | None = None,
    ) -> Dict[str, Any]:
        if self._session is None:
            raise RuntimeError(
                "HTTP client not initialized. Use 'async with' context manager."
            )

        # Use provided timeout (total seconds) or adapter default ClientTimeout
        if timeout is None:
            client_timeout = self._default_client_timeout
        else:
            client_timeout = aiohttp.ClientTimeout(
                total=timeout,
                sock_read=self._default_sock_read,
                sock_connect=self._default_sock_connect,
            )

        try:
            async with self._session.post(
                url, json=json, timeout=client_timeout, headers=headers
            ) as response:
                # Attempt to parse JSON, but return status and headers as well
                try:
                    body = await response.json()
                except aiohttp.ContentTypeError:
                    body = await response.text()

                # Raise for status to let higher-level mapping handle errors if requested
                # We won't call raise_for_status here to allow the caller to inspect status
                return {
                    "status": response.status,
                    "headers": dict(response.headers),
                    "body": body,
                }

        except asyncio.TimeoutError:
            logger.error("Timeout when POSTing to remote service. URL: %s", url)
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Upstream Timeout",
                    status=504,
                    detail="The request to the remote service timed out.",
                    instance=None,
                )
            )
        except aiohttp.ClientResponseError as client_response_err:
            logger.error(
                "HTTP error when POSTing to remote service. URL: %s, Status: %s, Error: %s",
                url,
                client_response_err.status,
                str(client_response_err),
            )
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Upstream HTTP Error",
                    status=client_response_err.status,
                    detail=f"The remote service returned an HTTP error: {client_response_err.status}",
                    instance=None,
                )
            )
        except aiohttp.ClientError as client_err:
            logger.error(
                "Connection error when POSTing to remote service. URL: %s, Error: %s",
                url,
                str(client_err),
            )
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Upstream Connection Error",
                    status=502,
                    detail="There was a connection error with the remote service.",
                    instance=None,
                )
            )
        except Exception as unexpected_post_error:
            logger.error(
                "Unexpected error when POSTing to remote service. URL: %s, Error: %s",
                url,
                str(unexpected_post_error),
            )
            raise OGCProcessException(
                OGCExceptionResponse(
                    type="about:blank",
                    title="Internal Server Error",
                    status=500,
                    detail="An unexpected error occurred while processing your request.",
                    instance=None,
                )
            )
