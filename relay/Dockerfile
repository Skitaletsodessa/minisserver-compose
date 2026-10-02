# Task 23 fetch relay. Pinned by digest (python:3.13-slim-trixie, Python 3.13.16, OpenSSL 3.5.7, checked 2026-10-02).
FROM python@sha256:bb2988715db2cf7ace7b53f38f3cffbef7c7046a656bee66245eb0ed386e2e81
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin relay \
 && mkdir /app /state && chown relay:relay /state
COPY relay.py relayctl.py /app/
USER relay
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python", "/app/relay.py"]
