# SMTP to API Proxy

This application is designed to translate outbound SMTP transactional traffic to modern web APIs. This allows for more secure web transmission and avoids SMTP restrictions on popular VPS hosting providers.

This is particular useful for older applications that only support SMTP and do not support any web API based email providers.

# Installation

The best way to run this application is as a container. You can pull the image from `ghcr.io/oracleguy/smtp-to-api-proxy:latest`.

The application can be configured with environment variables.

## Docker Compose

TBD

# Supported Providers

Currently only Mailgun and SendGrid are supported by this application.

## Transactional email forwarding

The proxy supports:

- To and CC recipients with display names, restricted to recipients actually
  supplied in SMTP `RCPT TO`. Envelope-only recipients are forwarded as BCC.
  Duplicate recipients are removed (case-insensitively); To takes precedence
  over CC. BCC addresses are never copied into visible message headers.
- The message's From address/display name and one or more Reply-To addresses.
  If From is absent, the SMTP envelope sender is used. The envelope sender is
  otherwise kept separate; provider-managed bounce addresses/Return-Path are
  not overridden.
- Decoded subjects, declared body charsets, plain text, HTML, and
  multipart/alternative messages containing both.
- Ordinary binary/text attachments and CID inline images, preserving decoded
  filenames, content types, file bytes, dispositions, and content IDs.
- Message-ID, In-Reply-To, and References for threading, plus explicitly allowlisted
  application X-* headers. The default allowlist is X-Correlation-ID and
  X-Request-ID. To replace it, set `FORWARDED_X_HEADERS` to a JSON array:
  `["X-App-ID", "X-Request-ID"]`. Set it to `[]` to disable custom header
  forwarding. Provider-reserved/transport X-* names are not accepted in this
  setting.

Mailgun uses its `messages.mime` API so delivery recipients remain independent
of the visible MIME headers, including for BCC-only or CC-only messages.
SendGrid requires at least one SMTP envelope recipient also present in the To
header: messages without one are rejected with SMTP **554**, rather than
exposing BCC addresses, promoting CC to To, or adding unintended recipients.
SendGrid also rejects explicitly empty subjects and zero-byte attachments with
SMTP **554**, since its API cannot represent them; Mailgun preserves these.
Both providers require the visible From address/domain to be authorized for
the configured account.

Malformed or unsupported content is rejected with SMTP **554**, not silently
discarded. Supported multipart structures are mixed, alternative, and related;
signed/encrypted messages, attached multipart messages (including forwarded
message/rfc822 messages), messages without a text/HTML body, and ambiguous
multiple bodies of the same type are unsupported. Old DKIM signatures,
Received, Authentication-Results, Return-Path, and arbitrary headers are not
forwarded; the provider generates its own transport/authentication headers.
Provider/network failures return SMTP **451** so the sending application can
retry. This proxy does not provide a durable delivery queue.

## Development tests

Install the development dependencies with `pip install -e '.[dev]'`, then run:

```sh
pytest -q
ruff check src/ tests/
```

Tests supply fake configuration, exercise real local SMTP exchanges, and mock
all outbound provider requests; no live API credentials are required.