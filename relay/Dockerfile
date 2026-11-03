# Task 23 fetch relay. Pinned by digest (python:3.13.18-slim-trixie, rebuilt 2026-11-03; first pin was 3.13.16 of 2026-10-02).
FROM python@sha256:ae14d4ce8771606477a81446e7ef429edd6762305fc50c84507c1041fa8285d1
RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin relay \
 && mkdir /app /state && chown relay:relay /state
COPY relay.py relayctl.py /app/
USER relay
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1
EXPOSE 8080
CMD ["python", "/app/relay.py"]
