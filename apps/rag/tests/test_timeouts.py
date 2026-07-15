import asyncio

import pytest

from app.timeouts import ExternalCallTimeoutError, with_hard_timeout


async def test_returns_result_within_deadline():
    async def fast() -> str:
        return "done"

    assert await with_hard_timeout(fast(), seconds=1.0, operation="fast.op") == "done"


async def test_raises_named_timeout_when_deadline_exceeded():
    async def slow() -> None:
        await asyncio.sleep(5)

    with pytest.raises(ExternalCallTimeoutError) as excinfo:
        await with_hard_timeout(slow(), seconds=0.05, operation="openai.embeddings")
    assert excinfo.value.operation == "openai.embeddings"
    assert "openai.embeddings" in str(excinfo.value)
