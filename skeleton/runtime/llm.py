"""LLM client: an OpenAI-compatible HTTP client and an offline fake.

The Manager Agent decides what to do when a call fails (Req 4.4), so the real
client never retries. Every failure surfaces as ``LLMError``. Error messages
never contain the API key or the Authorization header (Req 10.2).
"""

from __future__ import annotations

import re
from collections import deque
from typing import Any, Protocol

import httpx

DEFAULT_TIMEOUT_SECONDS = 20.0


class LLMError(Exception):
    """Any LLM failure: timeout, HTTP error, bad JSON, or empty response."""


class LLMClient(Protocol):
    async def complete(self, model: str, messages: list[dict], max_tokens: int = 400) -> str: ...


class OpenAICompatibleClient:
    """Calls ``POST {base_url}/chat/completions`` with Bearer auth."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._url = base_url.strip().rstrip("/") + "/chat/completions"
        self._api_key = api_key
        self._timeout = timeout
        self._transport = transport

    def __repr__(self) -> str:
        return f"OpenAICompatibleClient(url={self._url!r})"

    def _redact(self, text: str) -> str:
        key = self._api_key.strip()
        return text.replace(key, "***") if key else text

    async def complete(self, model: str, messages: list[dict], max_tokens: int = 400) -> str:
        payload = {"model": model, "messages": messages, "max_tokens": max_tokens}
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
        }
        try:
            async with httpx.AsyncClient(
                timeout=self._timeout, transport=self._transport
            ) as client:
                response = await client.post(self._url, json=payload, headers=headers)
        except httpx.TimeoutException:
            raise LLMError("LLM request timed out") from None
        except (httpx.HTTPError, httpx.InvalidURL) as exc:
            # Only the exception class is reported: its text can carry URLs or headers.
            raise LLMError(f"LLM request failed: {type(exc).__name__}") from None

        if not 200 <= response.status_code < 300:
            raise LLMError(f"LLM API returned HTTP {response.status_code}")

        try:
            data = response.json()
        except ValueError:
            raise LLMError("LLM response is not valid JSON") from None
        return self._extract_content(data)

    def _extract_content(self, data: Any) -> str:
        try:
            content = data["choices"][0]["message"]["content"]
        except (KeyError, IndexError, TypeError):
            raise LLMError("LLM response is missing choices[0].message.content") from None
        if not isinstance(content, str) or not content.strip():
            raise LLMError("LLM response is empty")
        return self._redact(content)


class FakeLLMClient:
    """Deterministic offline client for demo mode and tests."""

    def __init__(self, responses: list[str] | None = None) -> None:
        self.responses: deque[str] = deque(responses or [])
        self.calls: list[dict[str, Any]] = []
        self._failures = 0

    def fail_next(self, n: int = 1) -> None:
        """Make the next ``n`` calls raise ``LLMError``."""
        self._failures += max(0, n)

    async def complete(self, model: str, messages: list[dict], max_tokens: int = 400) -> str:
        self.calls.append({"model": model, "messages": messages})
        if self._failures > 0:
            self._failures -= 1
            raise LLMError("fake LLM failure")
        if self.responses:
            return self.responses.popleft()
        return _fake_text(_last_user_message(messages))


def _last_user_message(messages: list[dict]) -> str:
    for message in reversed(messages):
        if message.get("role") == "user":
            content = message.get("content")
            return content if isinstance(content, str) else ""
    return ""


def _fake_text(prompt: str) -> str:
    text = prompt.lower()
    if re.search(r"promo|diskon|marketing|pemasaran", text):
        return (
            "Ide promo: Beli 2 gratis 1 untuk produk yang stoknya menumpuk, "
            "berlaku akhir pekan ini. Pasang info di kasir dan kirim ke pelanggan langganan."
        )
    if re.search(r"ingat|tagih|tagihan|invoice|bayar|jatuh tempo", text):
        return (
            "Halo Kak, kami mengingatkan bahwa tagihan Anda akan jatuh tempo. "
            "Mohon konfirmasi jika sudah dibayar. Terima kasih atas kerja samanya."
        )
    if re.search(r"booking|jadwal|reservasi|follow|tindak lanjut", text):
        return (
            "Halo Kak, terima kasih sudah menghubungi kami. Kami ingin memastikan jadwal "
            "layanan Anda. Silakan balas pesan ini jika ingin mengubah waktu."
        )
    if re.search(r"stok|stock|beli|restock|pesan", text):
        return (
            "Ringkasan: beberapa produk berada di bawah stok minimum. "
            "Disarankan restock hari ini sesuai batas pengeluaran harian."
        )
    return (
        "Ringkasan: kegiatan hari ini berjalan normal. "
        "Tidak ada hal mendesak yang perlu ditangani pemilik."
    )


def build_llm_client(settings: Any) -> LLMClient:
    """Fake client in demo mode (no API key), real client otherwise."""
    if settings.demo_mode:
        return FakeLLMClient()
    return OpenAICompatibleClient(settings.llm_base_url, settings.llm_api_key)
