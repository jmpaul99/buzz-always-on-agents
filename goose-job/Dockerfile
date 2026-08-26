# Playwright image already has Chromium + Node. linux/amd64 only.
FROM mcr.microsoft.com/playwright:v1.55.1-noble

ENV DEBIAN_FRONTEND=noninteractive \
    GOOSE_DISABLE_KEYRING=1 \
    GOOSE_PROVIDER=litellm \
    GOOSE_MODEL=goose \
    GOOSE_MODE=auto \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright \
    PLAYWRIGHT_SKIP_BROWSER_DOWNLOAD=1 \
    HOME=/home/goose \
    GOOSE_RECIPE_PATH=/home/goose/recipes \
    PATH=/opt/sprig:/opt/goose:/usr/local/bin:${PATH}

RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl python3 python3-pip python3-venv tar xz-utils unzip \
    && rm -rf /var/lib/apt/lists/* \
    && useradd -m -u 10001 goose \
    && curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

# Sprig multicall: buzz / buzz-cli
ARG SPRIG_URL=https://github.com/block/buzz/releases/download/sprig-latest/sprig-x86_64-unknown-linux-musl.tar.gz
RUN mkdir -p /opt/sprig /tmp/sprig && cd /tmp/sprig \
    && curl -fsSL "${SPRIG_URL}" -o sprig.tar.gz \
    && tar -xzf sprig.tar.gz \
    && SRC=$(find . -type f \( -name sprig -o -name buzz-acp -o -name buzz \) | head -n1) \
    && install -m 755 "${SRC}" /opt/sprig/sprig \
    && ln -sfn /opt/sprig/sprig /opt/sprig/buzz \
    && ln -sfn /opt/sprig/sprig /usr/local/bin/buzz \
    && ln -sfn /opt/sprig/sprig /usr/local/bin/sprig \
    && rm -rf /tmp/sprig

ARG GOOSE_URL=https://github.com/block/goose/releases/latest/download/goose-x86_64-unknown-linux-gnu.tar.gz
RUN mkdir -p /opt/goose /tmp/goose && cd /tmp/goose \
    && curl -fsSL "${GOOSE_URL}" -o goose.tar.gz \
    && tar -xzf goose.tar.gz \
    && SRC=$(find . -type f -name goose | head -n1) \
    && install -m 755 "${SRC}" /opt/goose/goose \
    && ln -sfn /opt/goose/goose /usr/local/bin/goose \
    && rm -rf /tmp/goose

COPY goose-job/litellm_proxy.py goose-job/entrypoint.sh goose-job/worker.py goose-job/agenthome.py goose-job/nip44.py goose-job/observer.py goose-job/activity.py /app/
COPY listener/nostrutil.py /app/nostrutil.py
RUN mkdir -p /home/goose/.config/goose /home/goose/.config/gcloud /opt/buzz/local-mcp /home/goose/recipes
COPY goose/config.yaml /home/goose/.config/goose/config.yaml
COPY goose/.goosehints /home/goose/.config/goose/.goosehints
COPY goose/guardrails.md /home/goose/.config/goose/guardrails.md
COPY goose/local-mcp/google_adc_mcp.py /opt/buzz/local-mcp/google_adc_mcp.py
COPY listener/taskmcp.py /tmp/gen/taskmcp.py
COPY goose/generate_recipes.py /tmp/gen/generate_recipes.py
RUN python3 /tmp/gen/generate_recipes.py /home/goose/.config/goose/config.yaml /home/goose/recipes \
    && rm -rf /tmp/gen
ENV GOOSE_RECIPE_PATH=/home/goose/recipes
RUN pip3 install --break-system-packages --no-cache-dir \
      'google-auth==2.40.3' 'requests==2.32.5' \
      'websockets==15.0.1' 'bech32==1.2.0' 'coincurve==21.0.0' 'cryptography==44.0.2' \
    && sed -i 's/\r$//' /app/entrypoint.sh \
    && chmod +x /app/entrypoint.sh \
    && chown -R goose:goose /home/goose /app /opt/buzz

USER goose
WORKDIR /home/goose
ENTRYPOINT ["/app/entrypoint.sh"]
