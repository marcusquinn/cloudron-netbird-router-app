FROM netbirdio/netbird:0.83.1@sha256:3c9d68ae8b730aaed841ff5374e3efad8be550099871a52b1a653d30e5f4faf2 AS client
FROM cloudron/base:6.0.1@sha256:7b1e729960c3ca312d26e886917ba89483241bba0219ff292858e10d9d5768ee

LABEL org.opencontainers.image.source="https://github.com/marcusquinn/cloudron-netbird-router-app"

RUN apt-get update && apt-get install -y --no-install-recommends \
    supervisor \
    iproute2 \
    nftables \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

RUN mkdir -p /app/code/bin
COPY --from=client /usr/local/bin/netbird /app/code/bin/netbird
COPY start.sh supervisord.conf router.py /app/code/
RUN chmod +x /app/code/bin/netbird /app/code/start.sh /app/code/router.py

EXPOSE 8000

CMD ["/app/code/start.sh"]
