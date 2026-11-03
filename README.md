# SMTP to API Proxy

This application is designed to translate outbound SMTP transactional traffic to modern web APIs. This allows for more secure web transmission and avoids SMTP restrictions on popular VPS hosting providers.

This is particular useful for older applications that only support SMTP and do not support any web API based email providers.

# Installation

The best way to run this application is as a container. You can pull the image from `TBD`.

The application can be configured with environment variables.

## Docker Compose

TBD

# Supported Providers

Currently only Mailgun and SendGrid are supported by this application.