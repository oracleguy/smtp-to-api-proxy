import asyncio
import logging
from email import message_from_bytes

import httpx
from aiosmtpd.controller import Controller

from smtp_api_proxy.config import settings
from smtp_api_proxy.providers import (
    EmailProviderError,
    send_via_mailgun,
    send_via_sendgrid,
)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("smtp_api_proxy")

class ProxyHandler:
    def __init__(self):
        # Configure outbound proxies if provided in settings
        mounts = {"all://": httpx.AsyncBaseTransport(proxy=settings.HTTP_PROXY)} if settings.HTTP_PROXY else None
        self.client = httpx.AsyncClient(mounts=mounts)

    async def handle_DATA(self, server, session, envelope):
        mail_from = envelope.mail_from
        rcpt_tos = envelope.rcpt_tos
        
        # Parse the raw SMTP payload
        msg = message_from_bytes(envelope.content)
        subject = msg.get("subject", "(No Subject)")
        
        # Extract plain text content body
        body = ""
        if msg.is_multipart():
            for part in msg.walk():
                if part.get_content_type() == "text/plain":
                    body = part.get_payload(decode=True).decode(errors="ignore")
                    break
        else:
            body = msg.get_payload(decode=True).decode(errors="ignore")

        logger.info(f"Received mail from {mail_from} via SMTP. Forwarding via {settings.EMAIL_PROVIDER}...")

        try:
            if settings.EMAIL_PROVIDER == "sendgrid":
                await send_via_sendgrid(self.client, mail_from, rcpt_tos, subject, body)
            elif settings.EMAIL_PROVIDER == "mailgun":
                await send_via_mailgun(self.client, mail_from, rcpt_tos, subject, body)
            
            logger.info("Email forwarded successfully.")
            return "250 Message accepted for delivery"
            
        except EmailProviderError as e:
            logger.error(f"Failed to forward message: {e}")
            return "451 Requested action aborted: local error in processing"
            
        # Unexpected handler failures must defer delivery rather than accept the mail.
        except Exception as e:  # noqa: BLE001
            logger.error(f"Unexpected proxy exception: {e}")
            return "451 Internal server translation error"

    async def close(self):
        await self.client.aclose()

def main():
    handler = ProxyHandler()
    controller = Controller(handler, hostname=settings.SMTP_HOST, port=settings.SMTP_PORT)
    
    logger.info(f"Starting SMTP Proxy on {settings.SMTP_HOST}:{settings.SMTP_PORT}")
    controller.start()
    
    try:
        # Keep the main loop alive natively
        asyncio.get_event_loop().run_forever()
    except KeyboardInterrupt:
        logger.info("Stopping SMTP Proxy server...")
    finally:
        controller.stop()
        asyncio.run(handler.close())

if __name__ == "__main__":
    main()
