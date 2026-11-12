import base64

import httpx

from smtp_api_proxy.config import settings
from smtp_api_proxy.message import EmailTranslationError, ParsedEmail


class EmailProviderError(Exception):
    """Raised when an outbound web API request fails."""


async def send_via_sendgrid(client: httpx.AsyncClient, message: ParsedEmail) -> None:
    """Translate supported MIME content to the SendGrid Web API."""
    if not message.to:
        raise EmailTranslationError(
            "SendGrid requires an envelope recipient present in the To header"
        )
    if not message.subject:
        raise EmailTranslationError("SendGrid requires a nonempty Subject")
    api_key = settings.SENDGRID_API_KEY
    if not api_key:
        raise EmailProviderError("SendGrid API key is missing")
    personalization: dict[str, object] = {
        "to": [box.sendgrid() for box in message.to],
    }
    for name, boxes in (("cc", message.cc), ("bcc", message.bcc)):
        if boxes:
            personalization[name] = [box.sendgrid() for box in boxes]
    content = [
        {"type": content_type, "value": body or "\r\n"}
        for content_type, body in (
            ("text/plain", message.text),
            ("text/html", message.html),
        )
        if body is not None
    ]
    payload: dict[str, object] = {
        "personalizations": [personalization],
        "from": message.sender.sendgrid(),
        "subject": message.subject,
        "content": content,
    }
    if message.reply_to:
        payload["reply_to_list"] = [box.sendgrid() for box in message.reply_to]
    if message.headers:
        payload["headers"] = message.headers
    if message.attachments:
        attachments: list[dict[str, str]] = []
        for attachment in message.attachments:
            if not attachment.content:
                raise EmailTranslationError(
                    "SendGrid cannot represent empty attachments"
                )
            item = {
                "content": base64.b64encode(attachment.content).decode("ascii"),
                "type": attachment.content_type,
                "filename": attachment.filename,
                "disposition": "inline" if attachment.inline else "attachment",
            }
            if attachment.content_id:
                item["content_id"] = attachment.content_id
            attachments.append(item)
        payload["attachments"] = attachments

    response = await client.post(
        "https://api.sendgrid.com/v3/mail/send",
        json=payload,
        headers={"Authorization": "Bearer " + api_key},
    )
    if not response.is_success:
        raise EmailProviderError(
            f"SendGrid API Error ({response.status_code}): {response.text}"
        )


async def send_via_mailgun(client: httpx.AsyncClient, message: ParsedEmail) -> None:
    """Use MIME submission to keep visible headers separate from delivery recipients."""
    api_key = settings.MAILGUN_API_KEY
    if not api_key or not settings.MAILGUN_DOMAIN:
        raise EmailProviderError("Mailgun API key or domain is missing")
    base_url = (
        "https://api.eu.mailgun.net"
        if settings.MAILGUN_REGION == "eu"
        else "https://api.mailgun.net"
    )
    response = await client.post(
        f"{base_url}/v3/{settings.MAILGUN_DOMAIN}/messages.mime",
        auth=("api", api_key),
        data={"to": message.recipients},
        files={"message": ("message.eml", message.as_mime(), "message/rfc822")},
    )
    if not response.is_success:
        raise EmailProviderError(
            f"Mailgun API Error ({response.status_code}): {response.text}"
        )
