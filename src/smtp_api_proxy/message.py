from dataclasses import dataclass
from email import policy
from email.headerregistry import AddressHeader, BaseHeader
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import formataddr


class EmailTranslationError(ValueError):
    """A message cannot be translated without losing supported mail semantics."""


@dataclass(frozen=True)
class Mailbox:
    email: str
    name: str = ""

    def formatted(self) -> str:
        return formataddr((self.name, self.email))

    def sendgrid(self) -> dict[str, str]:
        return {"email": self.email, **({"name": self.name} if self.name else {})}


@dataclass(frozen=True)
class Attachment:
    content: bytes
    content_type: str
    filename: str
    inline: bool
    content_id: str | None


@dataclass(frozen=True)
class ParsedEmail:
    envelope_from: str
    sender: Mailbox
    to: list[Mailbox]
    cc: list[Mailbox]
    bcc: list[Mailbox]
    reply_to: list[Mailbox]
    subject: str
    text: str | None
    html: str | None
    attachments: list[Attachment]
    headers: dict[str, str]

    @property
    def recipients(self) -> list[str]:
        return [box.email for box in self.to + self.cc + self.bcc]

    def as_mime(self) -> bytes:
        """Rebuild only supported content; BCC and transport headers are never copied."""
        msg = EmailMessage(policy=policy.SMTP)
        msg["From"] = self.sender.formatted()
        for name, boxes in (
            ("To", self.to),
            ("Cc", self.cc),
            ("Reply-To", self.reply_to),
        ):
            if boxes:
                msg[name] = ", ".join(box.formatted() for box in boxes)
        msg["Subject"] = self.subject
        for name, value in self.headers.items():
            msg[name] = value
        if self.text is not None:
            msg.set_content(self.text)
            if self.html is not None:
                msg.add_alternative(self.html, subtype="html")
        else:
            msg.set_content(self.html, subtype="html")

        html_part = msg.get_body(preferencelist=("html",))
        for attachment in self.attachments:
            maintype, subtype = attachment.content_type.split("/", 1)
            cid = f"<{attachment.content_id}>" if attachment.content_id else None
            if attachment.inline and html_part is not None:
                html_part.add_related(
                    attachment.content,
                    maintype=maintype,
                    subtype=subtype,
                    filename=attachment.filename,
                    cid=cid,
                    disposition="inline",
                )
            else:
                msg.add_attachment(
                    attachment.content,
                    maintype=maintype,
                    subtype=subtype,
                    filename=attachment.filename,
                    cid=cid,
                    disposition="inline" if attachment.inline else "attachment",
                )
        return msg.as_bytes()


def _addresses(msg: EmailMessage, header: str) -> list[Mailbox]:
    values = msg.get_all(header, [])
    boxes: list[Mailbox] = []
    for value in values:
        if (
            not isinstance(value, BaseHeader)
            or value.defects
            or not isinstance(value, AddressHeader)
        ):
            raise EmailTranslationError(f"Invalid {header} address")
        for address in value.addresses:
            if not address.username or not address.domain:
                raise EmailTranslationError(f"Invalid {header} address")
            boxes.append(Mailbox(address.addr_spec, address.display_name))
    return boxes


def _header_value(value: object) -> str:
    # RFC folding is whitespace, not a newline in provider JSON/custom headers.
    return " ".join(str(value).splitlines())


def parse_email(
    content: bytes,
    envelope_from: str,
    envelope_recipients: list[str],
    forwarded_x_headers: list[str],
) -> ParsedEmail:
    try:
        return _parse_email(
            content, envelope_from, envelope_recipients, forwarded_x_headers
        )
    except (LookupError, UnicodeError, ValueError) as exc:
        if isinstance(exc, EmailTranslationError):
            raise
        raise EmailTranslationError("Invalid message encoding or MIME content") from exc


def _parse_email(
    content: bytes,
    envelope_from: str,
    envelope_recipients: list[str],
    forwarded_x_headers: list[str],
) -> ParsedEmail:
    msg = BytesParser(_class=EmailMessage, policy=policy.default).parsebytes(content)
    if not isinstance(msg, EmailMessage):
        raise EmailTranslationError("Invalid MIME message")
    senders = _addresses(msg, "From")
    if len(senders) > 1:
        raise EmailTranslationError("Multiple From addresses are unsupported")
    sender = senders[0] if senders else Mailbox(envelope_from)
    if not sender.email or "@" not in sender.email:
        raise EmailTranslationError("A valid From address is required")

    to_headers = {box.email.casefold(): box for box in _addresses(msg, "To")}
    cc_headers = {box.email.casefold(): box for box in _addresses(msg, "Cc")}
    to: list[Mailbox] = []
    cc: list[Mailbox] = []
    bcc: list[Mailbox] = []
    seen: set[str] = set()
    for address in envelope_recipients:
        key = address.casefold()
        if key in seen:
            continue
        seen.add(key)
        if key in to_headers:
            to.append(Mailbox(address, to_headers[key].name))
        elif key in cc_headers:
            cc.append(Mailbox(address, cc_headers[key].name))
        else:
            bcc.append(Mailbox(address))
    if not seen:
        raise EmailTranslationError("At least one envelope recipient is required")

    plain_part = msg.get_body(preferencelist=("plain",))
    html_part = msg.get_body(preferencelist=("html",))
    text = plain_part.get_content(errors="strict") if plain_part is not None else None
    html = html_part.get_content(errors="strict") if html_part is not None else None
    if text is not None and not isinstance(text, str):
        raise EmailTranslationError("Invalid plain text body")
    if html is not None and not isinstance(html, str):
        raise EmailTranslationError("Invalid HTML body")
    if text is None and html is None:
        raise EmailTranslationError("A plain text or HTML body is required")

    attachments: list[Attachment] = []
    for part in msg.walk():
        content_type = part.get_content_type()
        encoding = str(part.get("Content-Transfer-Encoding", "7bit")).lower()
        if encoding not in ("7bit", "8bit", "binary", "quoted-printable", "base64"):
            raise EmailTranslationError("Unsupported content transfer encoding")
        if part.is_multipart():
            if (
                content_type
                not in ("multipart/mixed", "multipart/alternative", "multipart/related")
                or part.get_content_disposition() == "attachment"
            ):
                raise EmailTranslationError(
                    f"Unsupported MIME structure: {content_type}"
                )
        elif part is not plain_part and part is not html_part:
            filename = part.get_filename()
            content_id = part.get("Content-ID")
            if (
                content_type in ("text/plain", "text/html")
                and not filename
                and not content_id
                and part.get_content_disposition() != "attachment"
            ):
                raise EmailTranslationError(
                    "Multiple bodies of the same type unsupported"
                )
            payload = part.get_payload(decode=True)
            if not isinstance(payload, bytes):
                raise EmailTranslationError("Invalid attachment payload")
            attachments.append(
                Attachment(
                    payload,
                    content_type,
                    filename or f"attachment-{len(attachments) + 1}",
                    part.get_content_disposition() == "inline"
                    or (
                        content_id is not None
                        and part.get_content_disposition() != "attachment"
                    ),
                    str(content_id).strip("<>") if content_id is not None else None,
                )
            )
        if part.defects:
            raise EmailTranslationError("Malformed MIME content")

    allowed = {"message-id", "in-reply-to", "references"} | {
        name.casefold() for name in forwarded_x_headers
    }
    headers: dict[str, str] = {}
    for name in msg:
        if name.casefold() in allowed:
            if len(msg.get_all(name, [])) != 1:
                raise EmailTranslationError(f"Duplicate forwarded header: {name}")
            headers[name] = _header_value(msg[name])
    return ParsedEmail(
        envelope_from,
        sender,
        to,
        cc,
        bcc,
        _addresses(msg, "Reply-To"),
        _header_value(msg.get("Subject", "(No Subject)")),
        text,
        html,
        attachments,
        headers,
    )
