# Use an official Python runtime as a parent image - matches the version
# this app was actually developed and tested against.
FROM python:3.12-slim

# System dependencies:
# - libreoffice / libreoffice-writer / python3-uno / xvfb: docx -> pdf conversion
# - fonts-liberation: Arial / Times New Roman equivalents (Liberation Sans/Serif)
# - fonts-crosextra-carlito / fonts-crosextra-caladea: Calibri / Cambria
#   equivalents. Without these, LibreOffice silently substitutes DejaVu Sans
#   for any Calibri template (Word's modern default font) - DejaVu Sans is
#   NOT metric-compatible, so the generated PDF reflows differently (different
#   line breaks, page breaks) than the template looked in Word. Confirmed by
#   direct testing - this is a real, previously-fixed bug, not a guess.
# - gcc / libpq-dev: asyncpg ships prebuilt wheels for most platforms, but if
#   none matches this exact base image, pip falls back to compiling its C
#   extension from source, which fails outright without these.
RUN apt-get update && apt-get install -y --no-install-recommends \
    libreoffice \
    libreoffice-writer \
    fonts-liberation \
    fonts-crosextra-carlito \
    fonts-crosextra-caladea \
    python3-uno \
    xvfb \
    gcc \
    libpq-dev \
    git \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# Set the working directory
WORKDIR /app

# Install Python dependencies first, separately from the app code, so
# editing main.py/app/ doesn't invalidate this layer's Docker build cache
# and force a full pip reinstall on every rebuild.
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Internal shared billing package - private repo, installed via a Render
# Secret File (not an ARG/ENV) so the token never lands in an image layer.
# BuildKit's --mount=type=secret only exists for the duration of this one
# RUN instruction; `docker history` on the built image shows nothing.
#
# Setup required in the Render dashboard (one-time, per environment):
#   Service -> Environment -> Secret Files -> Add Secret File
#     Filename: dodman_billing_pat
#     Contents: the raw GitHub fine-grained PAT (read-only, scoped to
#               just the dodman-billing repo) - no KEY=VALUE, just the token
RUN --mount=type=secret,id=dodman_billing_pat,dst=/run/secrets/dodman_billing_pat \
    pip install --no-cache-dir \
    "git+https://$(cat /run/secrets/dodman_billing_pat)@github.com/dodman-group-hq/dodman-billing.git@main"

# Copy project files to the container
COPY . .

# Render injects $PORT at runtime - the app must bind to whatever value
# that is, not a hardcoded port. 8000 here is just the documented default
# for local/manual runs where $PORT isn't set.
EXPOSE 8000

# Runs the actual ASGI app via uvicorn. `python main.py` would just import
# the module and exit immediately - main.py has no __main__ block that
# starts a server, so that CMD would build fine and then do nothing.
# --workers 1 is required, not a tuning choice: progress/download tracking
# lives in in-process memory (see app/routes/generate_statements.py) - a
# second worker process would never see jobs started on the first one.
# Shell form (not exec-array form) is required here too, specifically so
# ${PORT:-8000} actually gets expanded - the exec form doesn't invoke a
# shell and would pass the literal string "$PORT" through unexpanded.
CMD uvicorn main:app --host 0.0.0.0 --port ${PORT:-8000} --workers 1