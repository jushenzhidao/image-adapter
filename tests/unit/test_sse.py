"""Unit tests for SSE streaming bridge (AC-07)."""

import pytest

from adapter.utils.sse import stream_chat_as_sse


@pytest.mark.asyncio
async def test_stream_chat_as_sse():
    """AC-07: Non-streaming response is split into SSE chunks + [DONE]."""
    completion = {
        "id": "chatcmpl-test",
        "created": 1234567890,
        "model": "test-model",
        "choices": [
            {"message": {"role": "assistant", "content": "Hello world"}}
        ],
    }
    
    chunks = []
    async for chunk in stream_chat_as_sse(completion):
        chunks.append(chunk)
    
    # Should have: role chunk, content chunks, finish chunk, [DONE]
    assert len(chunks) >= 4
    assert 'data: {' in chunks[0]
    assert 'assistant' in chunks[0]
    assert "data: [DONE]" in chunks[-1]
    
    # Content should be split
    content_chunks = [c for c in chunks if '"content":' in c and '"role"' not in c]
    assert len(content_chunks) > 0
