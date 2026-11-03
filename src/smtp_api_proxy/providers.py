import httpx

from smtp_api_proxy.config import settings


class EmailProviderError(Exception):
    """Raised when an outbound web API request fails."""


async def send_via_sendgrid(
    client: httpx.AsyncClient,
    mail_from: str,
    rcpt_tos: list[str],
    subject: str,
    body: str,
) -> None:
    """Forwards incoming mail payload to the SendGrid Web API."""
    url = "https://api.sendgrid.com/v3/mail/send"
    headers = {
        "Authorization": f"Bearer {settings.SENDGRID_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "personalizations": [{"to": [{"email": rcpt} for rcpt in rcpt_tos]}],
        "from": {"email": mail_from},
        "subject": subject,
        "content": [{"type": "text/plain", "value": body}],
    }

    response = await client.post(url, json=payload, headers=headers)
    if response.is_error:
        raise EmailProviderError(
            f"SendGrid API Error ({response.status_code}): {response.text}"
        )


async def send_via_mailgun(
    client: httpx.AsyncClient,
    mail_from: str,
    rcpt_tos: list[str],
    subject: str,
    body: str,
) -> None:
    """Forwards incoming mail payload to the correct regional Mailgun Web API."""
    base_url = (
        "https://api.eu.mailgun.net"
        if settings.MAILGUN_REGION == "eu"
        else "https://api.mailgun.net"
    )
    url = f"{base_url}/v3/{settings.MAILGUN_DOMAIN}/messages"
    auth = ("api", settings.MAILGUN_API_KEY)
    data = {
        "from": mail_from,
        "to": rcpt_tos,
        "subject": subject,
        "text": body,
    }

    response = await client.post(url, auth=auth, data=data)
    if response.is_error:
        raise EmailProviderError(
            f"Mailgun API Error ({response.status_code}): {response.text}"
        )
