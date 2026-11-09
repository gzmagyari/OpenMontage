"""The gateway's UsageReader: where token usage hides in a Messages event stream."""

from __future__ import annotations

from engine.sandbox.gateway import UsageReader


def _feed(reader: UsageReader, event: bytes) -> None:
    reader.feed(b"data:" + event + b"\n")


def test_usage_from_message_start_alone():
    reader = UsageReader(streaming=True)
    _feed(reader, b'{"type":"message_start","message":{"id":"m1",'
                  b'"usage":{"input_tokens":7,"cache_read_input_tokens":3}}}')
    reader.close()
    assert reader.message_id == "m1"
    assert reader.usage == {"input_tokens": 7, "cache_read_input_tokens": 3}


def test_message_delta_usage_completes_the_picture():
    # Anthropic reports output tokens only in the closing message_delta, and some
    # Anthropic-format gateways (LiteLLM) report ALL counts there with zeros in
    # message_start; metering message_start alone priced such responses at $0.
    reader = UsageReader(streaming=True)
    _feed(reader, b'{"type":"message_start","message":{"id":"m1",'
                  b'"usage":{"input_tokens":0,"output_tokens":0}}}')
    _feed(reader, b'{"type":"message_delta","delta":{"stop_reason":"end_turn"},'
                  b'"usage":{"input_tokens":11,"output_tokens":5}}')
    reader.close()
    assert reader.message_id == "m1"
    assert reader.usage == {"input_tokens": 11, "output_tokens": 5}


def test_delta_zeros_do_not_clobber_known_counts():
    reader = UsageReader(streaming=True)
    _feed(reader, b'{"type":"message_start","message":{"id":"m1","usage":{"input_tokens":9}}}')
    _feed(reader, b'{"type":"message_delta","usage":{"input_tokens":0,"output_tokens":4}}')
    reader.close()
    assert reader.usage == {"input_tokens": 9, "output_tokens": 4}
