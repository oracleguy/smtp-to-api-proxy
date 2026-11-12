import asyncio
import base64
import json
import smtplib
import socket
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser

import httpx
import pytest
from aiosmtpd.controller import Controller

from smtp_api_proxy.config import settings
from smtp_api_proxy.main import ProxyHandler


@pytest.fixture(params=["sendgrid", "mailgun"])
def provider(request):
    return request.param


@pytest.fixture
def smtp_test_server(monkeypatch, provider):
    monkeypatch.setattr(settings, "EMAIL_PROVIDER", provider)
    monkeypatch.setattr(settings, "SENDGRID_API_KEY", "test_sg_token")
    monkeypatch.setattr(settings, "MAILGUN_API_KEY", "test_mg_token")
    monkeypatch.setattr(settings, "MAILGUN_DOMAIN", "sandbox.mg")
    monkeypatch.setattr(settings, "MAILGUN_REGION", "us")
    monkeypatch.setattr(settings, "HTTP_PROXY", None)
    monkeypatch.setattr(
        settings, "FORWARDED_X_HEADERS", ["X-Correlation-ID", "X-Request-ID"]
    )

    handler = ProxyHandler()
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    try:
        controller.start()
        yield controller.hostname, controller.port
    finally:
        try:
            if controller.server is not None:
                asyncio.run_coroutine_threadsafe(
                    handler.close(), controller.loop
                ).result(timeout=5)
            else:
                asyncio.run(handler.close())
        finally:
            controller.stop()


@pytest.fixture
def api(respx_mock, provider):
    endpoint = (
        "https://api.sendgrid.com/v3/mail/send"
        if provider == "sendgrid"
        else "https://api.mailgun.net/v3/sandbox.mg/messages.mime"
    )
    return respx_mock.post(endpoint).respond(202 if provider == "sendgrid" else 200)


def message() -> EmailMessage:
    msg = EmailMessage()
    msg["From"] = "app@local.internal"
    msg["To"] = "user@external.com"
    msg["Subject"] = "Integration Test"
    msg.set_content("Receipt body")
    return msg


def send(server, msg, **kwargs):
    with smtplib.SMTP(*server, timeout=5) as client:
        assert client.send_message(msg, **kwargs) == {}


def mailgun_submission(request: httpx.Request) -> tuple[list[str], EmailMessage]:
    """Decode the upload independently of the app's MIME builder."""
    upload = BytesParser(_class=EmailMessage, policy=policy.default).parsebytes(
        b"Content-Type: "
        + request.headers["Content-Type"].encode()
        + b"\r\n\r\n"
        + request.content
    )
    assert isinstance(upload, EmailMessage)
    recipients = []
    messages = []
    for part in upload.iter_parts():
        field = part.get_param("name", header="Content-Disposition")
        if field == "to":
            recipients.append(part.get_content())
        elif field == "message":
            nested = part.get_payload(0)
            assert isinstance(nested, EmailMessage)
            messages.append(nested)
    assert len(messages) == 1
    return recipients, messages[0]


def test_plain_text(smtp_test_server, api, provider):
    msg = message()
    msg.replace_header("To", "user@external.com, second@external.com")
    send(smtp_test_server, msg)
    assert api.call_count == 1
    request = api.calls.last.request
    if provider == "sendgrid":
        api_key = settings.SENDGRID_API_KEY
        assert api_key is not None
        assert request.headers["Authorization"] == "Bearer " + api_key
        payload = json.loads(request.content)
        assert payload["personalizations"] == [
            {"to": [{"email": "user@external.com"}, {"email": "second@external.com"}]}
        ]
        assert payload["from"] == {"email": "app@local.internal"}
        assert payload["subject"] == "Integration Test"
        assert payload["content"] == [
            {"type": "text/plain", "value": "Receipt body\r\n"}
        ]
    else:
        assert request.headers["Authorization"] == "Basic YXBpOnRlc3RfbWdfdG9rZW4="
        recipients, submitted = mailgun_submission(request)
        assert recipients == ["user@external.com", "second@external.com"]
        assert str(submitted["Subject"]) == "Integration Test"
        assert submitted.get_content() == "Receipt body\r\n"


def test_recipient_privacy_and_sender(smtp_test_server, api, provider):
    msg = message()
    msg.replace_header("From", "Café App <visible@example.com>")
    msg.replace_header("To", "User <user@external.com>, excluded@example.com")
    msg["Cc"] = "Copy <copy@example.com>, user@external.com"
    msg["Bcc"] = "hidden@example.com"
    msg["Reply-To"] = "Support <support@example.com>, help@example.com"
    send(
        smtp_test_server,
        msg,
        from_addr="bounce@example.com",
        to_addrs=[
            "user@external.com",
            "copy@example.com",
            "hidden@example.com",
            "envelope-only@example.com",
            "user@external.com",
        ],
    )
    request = api.calls.last.request
    if provider == "sendgrid":
        payload = json.loads(request.content)
        assert payload["from"] == {"email": "visible@example.com", "name": "Café App"}
        assert payload["personalizations"] == [
            {
                "to": [{"email": "user@external.com", "name": "User"}],
                "cc": [{"email": "copy@example.com", "name": "Copy"}],
                "bcc": [
                    {"email": "hidden@example.com"},
                    {"email": "envelope-only@example.com"},
                ],
            }
        ]
        assert payload["reply_to_list"] == [
            {"email": "support@example.com", "name": "Support"},
            {"email": "help@example.com"},
        ]
        assert "excluded@example.com" not in request.content.decode()
        assert "bounce@example.com" not in request.content.decode()
    else:
        recipients, submitted = mailgun_submission(request)
        assert recipients == [
            "user@external.com",
            "copy@example.com",
            "hidden@example.com",
            "envelope-only@example.com",
        ]
        assert str(submitted["From"]) == "Café App <visible@example.com>"
        assert str(submitted["To"]) == "User <user@external.com>"
        assert str(submitted["Cc"]) == "Copy <copy@example.com>"
        assert str(submitted["Reply-To"]) == (
            "Support <support@example.com>, help@example.com"
        )
        assert submitted["Bcc"] is None
        for address in (
            "hidden@example.com",
            "envelope-only@example.com",
            "excluded@example.com",
            "bounce@example.com",
        ):
            assert address not in submitted.as_string()


@pytest.mark.parametrize("html_only", [False, True])
def test_html_and_decoding(smtp_test_server, api, provider, html_only):
    msg = message()
    msg.replace_header("Subject", "Café receipt")
    if html_only:
        msg.set_content("<h1>Café receipt</h1>", subtype="html", charset="iso-8859-1")
    else:
        msg.set_content("Café receipt", charset="iso-8859-1")
        msg.add_alternative("<h1>Café receipt</h1>", subtype="html")
    send(smtp_test_server, msg)
    request = api.calls.last.request
    if provider == "sendgrid":
        payload = json.loads(request.content)
        assert payload["subject"] == "Café receipt"
        expected = [{"type": "text/html", "value": "<h1>Café receipt</h1>\r\n"}]
        if not html_only:
            expected.insert(0, {"type": "text/plain", "value": "Café receipt\r\n"})
        assert payload["content"] == expected
    else:
        _, submitted = mailgun_submission(request)
        assert str(submitted["Subject"]) == "Café receipt"
        html = submitted.get_body(("html",))
        assert html is not None
        assert html.get_content() == "<h1>Café receipt</h1>\r\n"
        plain = submitted.get_body(("plain",))
        if html_only:
            assert plain is None
        else:
            assert plain is not None
            assert plain.get_content() == "Café receipt\r\n"


def test_attachments_and_inline_images(smtp_test_server, api, provider):
    msg = message()
    msg.add_alternative('<html><img src="cid:logo-id"></html>', subtype="html")
    html = msg.get_body(("html",))
    assert html is not None
    html.add_related(
        b"\x89PNG\r\nlogo",
        maintype="image",
        subtype="png",
        filename="logo.png",
        cid="<logo-id>",
        disposition="inline",
    )
    msg.add_attachment(
        b"%PDF-invoice\x00\xff",
        maintype="application",
        subtype="pdf",
        filename="facturé.pdf",
    )
    send(smtp_test_server, msg)
    request = api.calls.last.request
    if provider == "sendgrid":
        payload = json.loads(request.content)
        attachments = payload["attachments"]
        assert attachments == [
            {
                "content": base64.b64encode(b"\x89PNG\r\nlogo").decode(),
                "type": "image/png",
                "filename": "logo.png",
                "disposition": "inline",
                "content_id": "logo-id",
            },
            {
                "content": base64.b64encode(b"%PDF-invoice\x00\xff").decode(),
                "type": "application/pdf",
                "filename": "facturé.pdf",
                "disposition": "attachment",
            },
        ]
        assert 'src="cid:logo-id"' in payload["content"][1]["value"]
    else:
        _, submitted = mailgun_submission(request)
        parts = {
            part.get_filename(): part
            for part in submitted.walk()
            if part.get_filename()
        }
        assert set(parts) == {"logo.png", "facturé.pdf"}
        assert parts["logo.png"].get_payload(decode=True) == b"\x89PNG\r\nlogo"
        assert parts["logo.png"]["Content-ID"] == "<logo-id>"
        assert parts["logo.png"].get_content_disposition() == "inline"
        assert parts["facturé.pdf"].get_payload(decode=True) == b"%PDF-invoice\x00\xff"
        assert parts["facturé.pdf"].get_content_disposition() == "attachment"
        html = submitted.get_body(("html",))
        assert html is not None
        assert 'src="cid:logo-id"' in html.get_content()


def test_text_attachment_is_not_body(smtp_test_server, api, provider):
    msg = message()
    msg.clear_content()
    msg.make_mixed()
    attachment = EmailMessage()
    attachment.set_content("Notes, not the body")
    attachment["Content-Disposition"] = 'attachment; filename="notes.txt"'
    body = EmailMessage()
    body.set_content("Actual body")
    msg.attach(attachment)
    msg.attach(body)
    send(smtp_test_server, msg)
    request = api.calls.last.request
    if provider == "sendgrid":
        payload = json.loads(request.content)
        assert payload["content"] == [
            {"type": "text/plain", "value": "Actual body\r\n"}
        ]
        assert base64.b64decode(payload["attachments"][0]["content"]) == (
            b"Notes, not the body\r\n"
        )
    else:
        _, submitted = mailgun_submission(request)
        plain = submitted.get_body(("plain",))
        assert plain is not None
        assert plain.get_content() == "Actual body\r\n"
        assert next(submitted.iter_attachments()).get_content() == (
            "Notes, not the body\r\n"
        )


def test_selective_headers(smtp_test_server, api, provider, monkeypatch):
    monkeypatch.setattr(settings, "FORWARDED_X_HEADERS", ["X-App-ID"])
    msg = message()
    msg["Message-ID"] = "<receipt@example.com>"
    msg["In-Reply-To"] = "<previous@example.com>"
    msg["References"] = "<first@example.com> <previous@example.com>"
    msg["X-App-ID"] = "billing"
    msg["X-Request-ID"] = "not-allowlisted"
    msg["X-SG-ID"] = "do-not-copy"
    msg["Received"] = "do-not-copy"
    msg["DKIM-Signature"] = "do-not-copy"
    msg["Authentication-Results"] = "do-not-copy"
    msg["Return-Path"] = "<bounce@example.com>"
    send(smtp_test_server, msg)
    request = api.calls.last.request
    expected = {
        "Message-ID": "<receipt@example.com>",
        "In-Reply-To": "<previous@example.com>",
        "References": "<first@example.com> <previous@example.com>",
        "X-App-ID": "billing",
    }
    if provider == "sendgrid":
        assert json.loads(request.content)["headers"] == expected
    else:
        _, submitted = mailgun_submission(request)
        for name, value in expected.items():
            assert str(submitted[name]) == value
        for name in (
            "X-Request-ID",
            "X-SG-ID",
            "Received",
            "DKIM-Signature",
            "Authentication-Results",
            "Return-Path",
        ):
            assert submitted[name] is None


@pytest.mark.parametrize("recipient_header", ["Cc", "Bcc", None])
def test_no_to_semantics(smtp_test_server, api, provider, recipient_header):
    msg = message()
    del msg["To"]
    if recipient_header:
        msg[recipient_header] = "only@example.com"
    if provider == "sendgrid":
        with pytest.raises(smtplib.SMTPDataError) as error:
            send(smtp_test_server, msg, to_addrs=["only@example.com"])
        assert error.value.smtp_code == 554
        assert isinstance(error.value.smtp_error, bytes)
        assert b"SendGrid requires" in error.value.smtp_error
        assert not api.called
    else:
        send(smtp_test_server, msg, to_addrs=["only@example.com"])
        recipients, submitted = mailgun_submission(api.calls.last.request)
        assert recipients == ["only@example.com"]
        assert submitted["To"] is None
        assert submitted["Bcc"] is None
        assert (submitted["Cc"] is not None) == (recipient_header == "Cc")


@pytest.mark.parametrize(
    "mime_headers",
    [
        b"Content-Type: text/plain; charset=utf-8\r\n",
        b"Content-Type: application/pkcs7-mime\r\n",
    ],
)
def test_unsupported_content_is_rejected_before_http(
    smtp_test_server, api, mime_headers
):
    raw = (
        b"From: app@local.internal\r\nTo: user@external.com\r\n"
        + mime_headers
        + b"\r\n\xff"
    )
    with (
        smtplib.SMTP(*smtp_test_server, timeout=5) as client,
        pytest.raises(smtplib.SMTPDataError) as error,
    ):
        client.sendmail("app@local.internal", ["user@external.com"], raw)
    assert error.value.smtp_code == 554
    assert not api.called


def test_explicit_bcc_header_is_not_copied(smtp_test_server, api, provider):
    msg = message()
    msg["Bcc"] = "hidden@example.com, not-in-envelope@example.com"
    with smtplib.SMTP(*smtp_test_server, timeout=5) as client:
        assert (
            client.sendmail(
                "app@local.internal",
                ["user@external.com", "hidden@example.com"],
                msg.as_bytes(policy=policy.SMTP),
            )
            == {}
        )
    if provider == "sendgrid":
        payload = json.loads(api.calls.last.request.content)
        assert payload["personalizations"][0]["bcc"] == [
            {"email": "hidden@example.com"}
        ]
        assert (
            "not-in-envelope@example.com" not in api.calls.last.request.content.decode()
        )
    else:
        _, submitted = mailgun_submission(api.calls.last.request)
        assert submitted["Bcc"] is None
        assert "hidden@example.com" not in submitted.as_string()


def test_empty_attachment_provider_limitation(smtp_test_server, api, provider):
    msg = message()
    msg.add_attachment(
        b"", maintype="application", subtype="octet-stream", filename="empty.bin"
    )
    if provider == "sendgrid":
        with pytest.raises(smtplib.SMTPDataError) as error:
            send(smtp_test_server, msg)
        assert error.value.smtp_code == 554
        assert not api.called
    else:
        send(smtp_test_server, msg)
        _, submitted = mailgun_submission(api.calls.last.request)
        assert next(submitted.iter_attachments()).get_payload(decode=True) == b""


def test_empty_subject_provider_limitation(smtp_test_server, api, provider):
    msg = message()
    msg.replace_header("Subject", "")
    if provider == "sendgrid":
        with pytest.raises(smtplib.SMTPDataError) as error:
            send(smtp_test_server, msg)
        assert error.value.smtp_code == 554
        assert not api.called
    else:
        send(smtp_test_server, msg)
        _, submitted = mailgun_submission(api.calls.last.request)
        assert str(submitted["Subject"]) == ""


@pytest.mark.parametrize("failure", [400, 429, 502, "timeout"])
def test_api_failure_returns_transient_smtp_code(smtp_test_server, api, failure):
    if failure == "timeout":
        api.side_effect = httpx.ReadTimeout("provider timed out")
    else:
        api.respond(failure, text="Provider error")
    with pytest.raises(smtplib.SMTPDataError) as error:
        send(smtp_test_server, message())
    assert error.value.smtp_code == 451
    assert api.call_count == 1


@pytest.mark.parametrize("region", ["us", "eu"])
@pytest.mark.parametrize("provider", ["mailgun"], indirect=True)
def test_mailgun_region(smtp_test_server, provider, region, monkeypatch, respx_mock):
    monkeypatch.setattr(settings, "MAILGUN_REGION", region)
    host = "api.eu.mailgun.net" if region == "eu" else "api.mailgun.net"
    route = respx_mock.post(f"https://{host}/v3/sandbox.mg/messages.mime").respond(200)
    send(smtp_test_server, message())
    assert route.call_count == 1
