import asyncio
import json
import smtplib
import socket
from email.message import EmailMessage
from urllib.parse import parse_qs

import httpx
import pytest
import respx
from aiosmtpd.controller import Controller

from smtp_api_proxy.config import settings
from smtp_api_proxy.main import ProxyHandler


@pytest.fixture
def smtp_test_server(monkeypatch):
    """Starts a local proxy on an available port for SMTP integration tests."""
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "sendgrid")
    monkeypatch.setattr(settings, "SENDGRID_API_KEY", "test_sg_token")
    monkeypatch.setattr(settings, "MAILGUN_API_KEY", "test_mg_token")
    monkeypatch.setattr(settings, "MAILGUN_DOMAIN", "sandbox.mg")
    monkeypatch.setattr(settings, "MAILGUN_REGION", "us")

    handler = ProxyHandler()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()

    yield controller.hostname, controller.port

    asyncio.run_coroutine_threadsafe(handler.close(), controller.loop).result()
    controller.stop()


@respx.mock
def test_sendgrid_integration_happy_path(smtp_test_server):
    """Verifies SMTP mail is translated to a correctly authenticated SendGrid call."""
    host, port = smtp_test_server
    sendgrid_route = respx.post("https://api.sendgrid.com/v3/mail/send").mock(
        return_value=httpx.Response(202, text="Accepted")
    )

    msg = EmailMessage()
    msg["Subject"] = "Integration Test"
    msg["From"] = "app@local.internal"
    msg["To"] = "user@external.com, second@external.com"
    msg.set_content("Hello from the inner container proxy network!")

    with smtplib.SMTP(host, port) as client:
        refused_recipients = client.send_message(msg)
        assert refused_recipients == {}

    assert sendgrid_route.called
    request = sendgrid_route.calls.last.request
    assert request.headers["Authorization"] == "Bearer test_sg_token"
    payload = json.loads(request.content)
    assert payload["personalizations"][0]["to"] == [
        {"email": "user@external.com"},
        {"email": "second@external.com"},
    ]
    assert payload["from"] == {"email": "app@local.internal"}
    assert payload["subject"] == "Integration Test"
    assert payload["content"] == [{
        "type": "text/plain",
        "value": "Hello from the inner container proxy network!\r\n",
    }]


@pytest.mark.parametrize(
    ("region", "endpoint"),
    [
        ("us", "https://api.mailgun.net"),
        ("eu", "https://api.eu.mailgun.net"),
    ],
)
@respx.mock
def test_mailgun_regional_routing_integration(
    smtp_test_server, region, endpoint, monkeypatch
):
    """Verifies Mailgun requests use the endpoint for the configured region."""
    host, port = smtp_test_server
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", "mailgun")
    monkeypatch.setattr(settings, "MAILGUN_REGION", region)
    mailgun_route = respx.post(f"{endpoint}/v3/sandbox.mg/messages").mock(
        return_value=httpx.Response(200, json={"message": "Queued"})
    )

    msg = EmailMessage()
    msg["Subject"] = "Regional Check"
    msg["From"] = "app@local.internal"
    msg["To"] = "user@external.com"
    msg.set_content("Testing regional endpoint paths.")

    with smtplib.SMTP(host, port) as client:
        refused_recipients = client.send_message(msg)
        assert refused_recipients == {}

    assert mailgun_route.called
    request = mailgun_route.calls.last.request
    assert request.headers["Authorization"] == "Basic YXBpOnRlc3RfbWdfdG9rZW4="
    assert str(request.url) == f"{endpoint}/v3/sandbox.mg/messages"
    assert parse_qs(request.content.decode()) == {
        "from": ["app@local.internal"],
        "to": ["user@external.com"],
        "subject": ["Regional Check"],
        "text": ["Testing regional endpoint paths.\r\n"],
    }


@respx.mock
def test_api_failure_returns_transient_smtp_code(smtp_test_server):
    """Verifies provider errors are reported as SMTP 451 deferrals."""
    host, port = smtp_test_server
    api_route = respx.post("https://api.sendgrid.com/v3/mail/send").mock(
        return_value=httpx.Response(502, text="Bad Gateway")
    )

    msg = EmailMessage()
    msg["Subject"] = "Fault Check"
    msg["From"] = "app@local.internal"
    msg["To"] = "user@external.com"
    msg.set_content("This payload will force an API failure threshold test.")

    with smtplib.SMTP(host, port) as client:
        with pytest.raises(smtplib.SMTPDataError) as exc_info:
            client.send_message(msg)
        assert exc_info.value.smtp_code == 451
    assert api_route.called
