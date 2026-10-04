FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2
# Patch CVE-2026-103111 from the signed Debian 13 security repository.
RUN apt-get update && apt-get install -y --no-install-recommends libpcre2-8-0=10.46-1~deb13u3 \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/fetch-openssl-patch.py /tmp/fetch-openssl-patch.py
RUN python /tmp/fetch-openssl-patch.py /tmp/openssl-patch \
    && dpkg -i /tmp/openssl-patch/*.deb \
    && python -c 'import ssl; assert ssl.OPENSSL_VERSION.startswith("OpenSSL 3.6.5 ")' \
    && rm -rf /tmp/openssl-patch /tmp/fetch-openssl-patch.py
WORKDIR /app
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir . && useradd --uid 10001 --create-home app
USER app
EXPOSE 4060
CMD ["uvicorn", "agent_platform.app:app", "--host", "0.0.0.0", "--port", "4060"]
