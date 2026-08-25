# Base: official Playwright MCP image (node, @playwright/mcp, chromium +
# system deps already correctly provisioned -- not worth reproducing by
# hand). We add Python on top for the guardian wrapper (see guardian/app.py
# for why the wrapper exists) and fully override ENTRYPOINT: the base
# image's own entrypoint runs the Playwright MCP CLI directly as PID 1,
# but here the *Python* process is PID 1 and spawns the Playwright MCP CLI
# itself as a child, on a loopback-only internal port, so it can connect to
# it as an MCP client. See guardian/app.py for the full request flow.
FROM mcr.microsoft.com/playwright/mcp:latest

# Base image runs as `node` (non-root); apt needs root, browsers/npm
# packages already installed for `node`, so switch back afterwards.
USER root
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv python3-pip \
    && rm -rf /var/lib/apt/lists/*

RUN python3 -m venv /opt/venv
ENV PATH="/opt/venv/bin:${PATH}"

WORKDIR /guardian
COPY pyproject.toml ./
COPY guardian ./guardian
RUN pip install --no-cache-dir .

# Single source of truth for the domain allowlist: semicolon-separated,
# same format @playwright/mcp itself uses for PLAYWRIGHT_MCP_ALLOWED_ORIGINS
# -- handed to the spawned upstream process AND used to annotate tool
# descriptions / initialize instructions, from this one value.
ENV ALLOWED_ORIGINS="" \
    PORT=8080

EXPOSE 8080

USER node

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python3 -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/readyz', timeout=3)" || exit 1

ENTRYPOINT ["playwright-mcp-guarded"]
