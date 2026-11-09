# Loki 3.7 images are distroless (no shell, no wget), so a Compose healthcheck has
# nothing to run. Add one static busybox binary for `busybox wget`; Loki is unchanged.
FROM busybox:1.38.0-musl@sha256:80e38f9a1b39188b8bdb28879178edb322a4402b081a9aedda1924609decd6f6 AS busybox

FROM grafana/loki:3.7.9@sha256:0db216b630ae1f348a1584c67f3cead8b4c1fc839169a39501db664744158e28
COPY --from=busybox /bin/busybox /usr/local/bin/busybox
