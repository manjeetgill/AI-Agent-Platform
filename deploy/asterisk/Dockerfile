FROM alpine:3.23@sha256:85fe1e81d6758c208f3e1eed4338a1997e19d4be002d4dd32d3100c9a8c010a0
RUN apk add --no-cache asterisk=20.15.2-r1 \
    && addgroup -g 10001 agentvoice && adduser -D -u 10001 -G agentvoice agentvoice
COPY deploy/asterisk/config/ /opt/asterisk/
COPY deploy/asterisk/entrypoint.sh /usr/local/bin/start-asterisk
COPY deploy/asterisk/healthcheck.sh /usr/local/bin/asterisk-healthcheck
RUN chmod 755 /usr/local/bin/start-asterisk /usr/local/bin/asterisk-healthcheck
USER 10001:10001
ENTRYPOINT ["/usr/local/bin/start-asterisk"]
