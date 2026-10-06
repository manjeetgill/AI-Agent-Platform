FROM coturn/coturn@sha256:bbefd3e1fdfdc0d58770fe01b581fd8b00d9f3a5580d00acb77cf719a6bc78e3
USER root
# Plain copy drops file capabilities; this listener uses only unprivileged ports.
RUN cp /usr/bin/turnserver /usr/bin/turnserver-unprivileged && chmod 0755 /usr/bin/turnserver-unprivileged
USER nobody:nogroup
ENTRYPOINT ["/usr/bin/turnserver-unprivileged"]
