# Task 27 portal-schedules. Base pinned by digest (python:3.13-slim-trixie, Python 3.13.16, pulled 2026-10-19; Diun watches the
# upstream repo through /srv/compose/diun/caddy.yml). Dependencies are installed ONLY with --require-hashes.
FROM python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f
RUN useradd --system --uid 1000 --no-create-home --shell /usr/sbin/nologin portal \
 && mkdir /app /data && chown 1000:1000 /data
COPY requirements.txt /app/requirements.txt
RUN pip install --no-cache-dir --require-hashes -r /app/requirements.txt
COPY sched /app/sched
COPY seed /app/seed
USER 1000:1000
WORKDIR /app
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PORTAL_DATA=/data
CMD ["python", "-m", "sched.server"]
