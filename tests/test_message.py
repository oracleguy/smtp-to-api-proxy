from email import policy
from email.message import EmailMessage

import pytest
from pydantic import ValidationError

from smtp_api_proxy.config import ProxySettings
from smtp_api_proxy.message import EmailTranslationError, parse_email


def parse(content: bytes):
    return parse_email(
        content,
        "bounce@example.com",
        ["to@example.com"],
        ["X-Correlation-ID", "X-Request-ID"],
    )


def test_envelope_sender_is_separate_and_from_falls_back():
    parsed = parse(b"To: to@example.com\r\n\r\nbody")
    assert parsed.envelope_from == "bounce@example.com"
    assert parsed.sender.email == "bounce@example.com"


def test_headerless_plain_text():
    parsed = parse(b"\r\nbody")
    assert parsed.text == "body"
    assert parsed.sender.email == "bounce@example.com"
    assert parsed.subject == "(No Subject)"


def test_folded_encoded_headers():
    parsed = parse(
        b"From: =?utf-8?q?Caf=C3=A9_App?= <sender@example.com>\r\n"
        b"To: to@example.com\r\n"
        b"Subject: =?utf-8?q?Caf=C3=A9?=\r\n receipt\r\n"
        b"References: <first@example.com>\r\n <second@example.com>\r\n"
        b"x-request-id: request-123\r\n"
        b"\r\nbody"
    )
    assert parsed.sender.name == "Café App"
    assert parsed.subject == "Café receipt"
    assert parsed.headers == {
        "References": "<first@example.com> <second@example.com>",
        "x-request-id": "request-123",
    }


def test_empty_address_groups_do_not_add_recipients():
    parsed = parse(
        b"To: undisclosed-recipients:;\r\nCc: empty:;, to@example.com\r\n\r\nbody"
    )
    assert not parsed.to
    assert [box.email for box in parsed.cc] == ["to@example.com"]
    assert not parsed.bcc


def test_recipients_are_matched_case_insensitively_but_keep_envelope_spelling():
    parsed = parse(b"To: User <TO@EXAMPLE.COM>\r\nCc: to@example.com\r\n\r\nbody")
    assert parsed.to[0].email == "to@example.com"
    assert parsed.to[0].name == "User"
    assert not parsed.cc


def test_related_start_selects_html_not_a_text_resource():
    msg = EmailMessage()
    msg["To"] = "to@example.com"
    msg.make_related()
    resource = EmailMessage()
    resource.set_content("Resource contents")
    resource["Content-ID"] = "<resource>"
    html = EmailMessage()
    html.set_content('<a href="cid:resource">Download</a>', subtype="html")
    html["Content-ID"] = "<body>"
    msg.attach(resource)
    msg.attach(html)
    msg.set_param("start", "<body>")
    parsed = parse(msg.as_bytes(policy=policy.SMTP))
    assert parsed.text is None
    assert parsed.html == '<a href="cid:resource">Download</a>\r\n'
    assert parsed.attachments[0].content == b"Resource contents\r\n"
    assert parsed.attachments[0].inline
    assert parsed.attachments[0].content_id == "resource"


@pytest.mark.parametrize(
    "raw",
    [
        b"Content-Type: text/plain; charset=unknown-encoding\r\n\r\nbody",
        b"Content-Type: text/plain; charset=utf-8\r\n\r\n\xff",
        b"Content-Type: multipart/mixed; boundary=missing\r\n\r\nbody",
        (
            b"Content-Type: multipart/signed; boundary=x\r\n\r\n"
            b"--x\r\nContent-Type: text/plain\r\n\r\nbody\r\n--x--"
        ),
        b"Content-Type: application/pkcs7-mime\r\n\r\nencrypted",
        b"From: first@example.com, second@example.com\r\n\r\nbody",
        b"To: invalid-address\r\n\r\nbody",
        b"Content-Transfer-Encoding: base64\r\n\r\n!!!!",
        b"Content-Transfer-Encoding: unknown\r\n\r\nbody",
        b"References: <one@example.com>\r\nReferences: <two@example.com>\r\n\r\nbody",
    ],
)
def test_invalid_or_unsupported_messages_are_rejected(raw):
    with pytest.raises(EmailTranslationError):
        parse(raw)


def test_multiple_plain_bodies_are_not_silently_dropped():
    msg = EmailMessage()
    msg.make_mixed()
    for body in ("First", "Second"):
        part = EmailMessage()
        part.set_content(body)
        msg.attach(part)
    with pytest.raises(EmailTranslationError, match="Multiple bodies"):
        parse(msg.as_bytes())


@pytest.mark.parametrize("name", ["To", "X-SG-ID", "X-Mailgun-Variables", "X-Bad:\n"])
def test_header_allowlist_rejects_reserved_or_invalid_names(name):
    with pytest.raises(ValidationError):
        ProxySettings(
            EMAIL_PROVIDER="sendgrid",
            SENDGRID_API_KEY="test",
            FORWARDED_X_HEADERS=[name],
        )


def test_header_allowlist_accepts_application_names():
    config = ProxySettings(
        EMAIL_PROVIDER="sendgrid",
        SENDGRID_API_KEY="test",
        FORWARDED_X_HEADERS=["X-App-ID"],
    )
    assert config.FORWARDED_X_HEADERS == ["X-App-ID"]
