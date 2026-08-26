FROM ghcr.io/berriai/litellm:main-stable
COPY goose/config.yaml /tmp/goose-config.yaml
COPY litellm/config.yaml /tmp/litellm-config.yaml
COPY litellm/merge_extension_keywords.py /tmp/merge_extension_keywords.py
RUN python /tmp/merge_extension_keywords.py /tmp/goose-config.yaml /tmp/litellm-config.yaml /app/config.yaml
ENV PORT=8080
# Cloud Run injects PORT; bind all interfaces. Auth is Cloud Run IAM + master key header.
CMD ["--config", "/app/config.yaml", "--port", "8080", "--host", "0.0.0.0", "--num_workers", "1"]
