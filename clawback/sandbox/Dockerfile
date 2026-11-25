FROM python:3.12-slim

# A non-root user with a fixed uid so bind mounts behave predictably.
RUN useradd --create-home --uid 1000 --shell /bin/bash claw \
    && mkdir -p /work /opt/clawback \
    && chown claw:claw /work

ENV HOME=/tmp \
    PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1

USER claw
WORKDIR /work

# The container is kept alive and tools are run with `docker exec`.
CMD ["sleep", "infinity"]
