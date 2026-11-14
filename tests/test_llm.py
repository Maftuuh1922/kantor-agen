import json

import httpx
import pytest

from skeleton.core.config import Settings
from skeleton.runtime.llm import (
    FakeLLMClient,
    LLMError,
    OpenAICompatibleClient,
    build_llm_client,
)

KEY = "sk-secret-123456"
MESSAGES = [{"role": "user", "content": "Buat ide promo"}]


def make_client(handler, **kwargs):
    return OpenAICompatibleClient(
        "https://llm.example.com/v1/",
        KEY,
        transport=httpx.MockTransport(handler),
        **kwargs,
    )


def ok(content="halo"):
    return httpx.Response(200, json={"choices": [{"message": {"content": content}}]})


async def test_successful_parse():
    client = make_client(lambda request: ok("Ide promo"))
    assert await client.complete("m-low", MESSAGES) == "Ide promo"


async def test_request_url_headers_and_body():
    seen = []

    def handler(request: httpx.Request):
        seen.append(request)
        return ok()

    await make_client(handler).complete("m-strong", MESSAGES, max_tokens=123)
    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == "https://llm.example.com/v1/chat/completions"
    assert request.headers["Authorization"] == f"Bearer {KEY}"
    assert json.loads(request.content) == {
        "model": "m-strong",
        "messages": MESSAGES,
        "max_tokens": 123,
    }


async def test_timeout_becomes_llm_error():
    def handler(request):
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(LLMError, match="timed out"):
        await make_client(handler).complete("m", MESSAGES)


async def test_connection_error_becomes_llm_error():
    def handler(request):
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(LLMError):
        await make_client(handler).complete("m", MESSAGES)


async def test_http_500_error_has_no_key():
    body = f"upstream error, Authorization: Bearer {KEY}"
    client = make_client(lambda request: httpx.Response(500, text=body))
    with pytest.raises(LLMError) as info:
        await client.complete("m", MESSAGES)
    message = str(info.value)
    assert "500" in message
    assert KEY not in message
    assert "Authorization" not in message
    assert KEY not in repr(client)


async def test_malformed_json():
    client = make_client(lambda request: httpx.Response(200, text="not json"))
    with pytest.raises(LLMError):
        await client.complete("m", MESSAGES)


@pytest.mark.parametrize(
    "payload",
    [
        {},
        {"choices": []},
        {"choices": [{}]},
        {"choices": [{"message": {}}]},
        {"choices": [{"message": {"content": "   "}}]},
        {"choices": [{"message": {"content": None}}]},
        ["not", "a", "dict"],
    ],
)
async def test_missing_or_empty_fields(payload):
    client = make_client(lambda request: httpx.Response(200, json=payload))
    with pytest.raises(LLMError):
        await client.complete("m", MESSAGES)


async def test_fake_records_calls_and_is_deterministic():
    fake = FakeLLMClient()
    first = await fake.complete("m-low", MESSAGES)
    second = await fake.complete("m-strong", MESSAGES)
    assert first == second and first.strip()
    assert fake.calls == [
        {"model": "m-low", "messages": MESSAGES},
        {"model": "m-strong", "messages": MESSAGES},
    ]


async def test_fake_text_follows_last_user_message():
    fake = FakeLLMClient()
    promo = await fake.complete("m", [{"role": "user", "content": "ide promo minggu ini"}])
    reminder = await fake.complete("m", [{"role": "user", "content": "ingatkan tagihan"}])
    assert "promo" in promo.lower()
    assert "tagihan" in reminder.lower()


async def test_fake_responses_queue():
    fake = FakeLLMClient(responses=["satu", "dua"])
    assert await fake.complete("m", MESSAGES) == "satu"
    assert await fake.complete("m", MESSAGES) == "dua"
    assert (await fake.complete("m", MESSAGES)).strip()


async def test_fake_fail_next():
    fake = FakeLLMClient(responses=["ok"])
    fake.fail_next(2)
    for _ in range(2):
        with pytest.raises(LLMError):
            await fake.complete("m", MESSAGES)
    assert await fake.complete("m", MESSAGES) == "ok"
    assert len(fake.calls) == 3


def test_build_llm_client_picks_fake_in_demo_mode():
    assert isinstance(build_llm_client(Settings()), FakeLLMClient)


def test_build_llm_client_picks_real_client_with_key():
    settings = Settings(llm_base_url="https://llm.example.com/v1", llm_api_key=KEY)
    client = build_llm_client(settings)
    assert isinstance(client, OpenAICompatibleClient)
    assert KEY not in repr(client)
