# --- Build Stage ---
FROM python:3.14-slim AS builder

WORKDIR /app

# Install build dependencies
RUN pip install --no-cache-dir build

# Copy configuration and source files into the builder container
COPY pyproject.toml README.md ./
COPY src/ ./src/

# Compile the application package into a standard wheel distribution
RUN python -m build --wheel

# --- Runtime Stage ---
FROM python:3.14-slim

WORKDIR /app

# Copy the pre-compiled wheel distribution from the builder stage
COPY --from=builder /app/dist/*.whl .

# Install the wheel (automatically fetches core production dependencies from pyproject.toml)
RUN pip install --no-cache-dir *.whl && rm *.whl

# Run as a non-root service user for enhanced container security
USER whistleblower:whistleblower

# Expose the non-privileged SMTP proxy port defined in your configuration
EXPOSE 1025

# Launch the script shortcut defined inside your pyproject.toml metadata
ENTRYPOINT ["smtp-api-proxy"]
