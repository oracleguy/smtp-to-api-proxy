import os

# Settings validate at import time; tests never need live provider credentials.
os.environ.setdefault("EMAIL_PROVIDER", "sendgrid")
os.environ.setdefault("SENDGRID_API_KEY", "test-only-sendgrid-key")
os.environ.setdefault("MAILGUN_API_KEY", "test-only-mailgun-key")
os.environ.setdefault("MAILGUN_DOMAIN", "sandbox.mg")
