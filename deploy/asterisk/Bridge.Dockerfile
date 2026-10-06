FROM python:3.14-slim@sha256:caaf356f40667c496d405780745b9ac25771c189a51dfcc42430d531ea09f8a2
# Match the platform API image's existing security fixes.
RUN apt-get update && apt-get install -y --no-install-recommends libpcre2-8-0=10.46-1~deb13u3 espeak-ng \
    && rm -rf /var/lib/apt/lists/*
COPY scripts/fetch-openssl-patch.py /tmp/fetch-openssl-patch.py
RUN python /tmp/fetch-openssl-patch.py /tmp/openssl-patch \
    && dpkg -i /tmp/openssl-patch/*.deb \
    && python -c 'import ssl; assert ssl.OPENSSL_VERSION.startswith("OpenSSL 3.6.5 ")' \
    && rm -rf /tmp/openssl-patch /tmp/fetch-openssl-patch.py
WORKDIR /app
COPY pyproject.toml ./
COPY src ./src
RUN pip install --no-cache-dir '.[voice]' && useradd --uid 10001 --create-home app && mkdir /state && chown app:app /state
USER 10001:10001
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
CMD ["python", "-m", "agent_platform.asterisk_bridge"]
