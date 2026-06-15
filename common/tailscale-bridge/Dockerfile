FROM alpine:3.20

RUN apk add --no-cache tailscale ca-certificates

ENV TS_HOSTNAME={{.TS_HOSTNAME}}
ENV UPSTREAM_PORT={{.UPSTREAM_PORT}}
# Auth key is supplied at runtime (written to the shared persist volume by the
# upstream app), not baked into the image.
ENV TS_STATE_DIR=/data/state
ENV TS_AUTHKEY_FILE=/data/tailscale-authkey

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

CMD ["/entrypoint.sh"]
