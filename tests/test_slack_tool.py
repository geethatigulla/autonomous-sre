"""Tests for the Slack incoming-webhook helper."""

from __future__ import annotations

import asyncio

import httpx

from orchestrator.tools import slack_tool
from orchestrator.tools.slack_tool import SlackNotifier


class _Resp:
    def __init__(self, status_code: int, text: str = "") -> None:
        self.status_code = status_code
        self.text = text


def _fake_async_client(status_code: int = 200, exc: Exception | None = None, sent: list | None = None):
    class _Client:
        def __init__(self, *a, **kw) -> None:
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a) -> None:
            return None

        async def post(self, url, json):
            if sent is not None:
                sent.append((url, json))
            if exc is not None:
                raise exc
            return _Resp(status_code, "nope" if status_code >= 400 else "ok")

    return _Client


def test_disabled_without_url(monkeypatch) -> None:
    def _boom(*a, **kw):
        raise AssertionError("network must not be touched when disabled")

    monkeypatch.setattr(slack_tool.httpx, "AsyncClient", _boom)
    monkeypatch.setattr(slack_tool.httpx, "post", _boom)

    for url in (None, "", "   "):
        notifier = SlackNotifier(url)
        assert notifier.enabled is False
        result = asyncio.run(notifier.post("hi"))
        assert result.skipped and not result.ok
        assert notifier.post_sync("hi").skipped


def test_post_success(monkeypatch) -> None:
    sent: list = []
    monkeypatch.setattr(slack_tool.httpx, "AsyncClient", _fake_async_client(200, sent=sent))
    result = asyncio.run(SlackNotifier("https://hooks.slack.test/x").post("hello"))
    assert result.ok and result.status_code == 200
    assert sent == [("https://hooks.slack.test/x", {"text": "hello"})]


def test_post_non_2xx_is_reported_not_raised(monkeypatch) -> None:
    monkeypatch.setattr(slack_tool.httpx, "AsyncClient", _fake_async_client(500))
    result = asyncio.run(SlackNotifier("https://hooks.slack.test/x").post("hello"))
    assert not result.ok
    assert result.status_code == 500
    assert result.as_event()["status_code"] == 500


def test_post_network_error_is_reported_not_raised(monkeypatch) -> None:
    monkeypatch.setattr(
        slack_tool.httpx, "AsyncClient", _fake_async_client(exc=httpx.ConnectError("down"))
    )
    result = asyncio.run(SlackNotifier("https://hooks.slack.test/x").post("hello"))
    assert not result.ok
    assert "ConnectError" in result.error


def test_post_sync(monkeypatch) -> None:
    monkeypatch.setattr(slack_tool.httpx, "post", lambda *a, **kw: _Resp(204))
    assert SlackNotifier("https://hooks.slack.test/x").post_sync("hello").ok
