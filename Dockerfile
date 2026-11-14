# EMS-1000 virtual device simulator.
# Build:  docker build -t ems1000-device-sim .
# Run:    docker run --rm -e IOTHUB_DEVICE_CONNECTION_STRING ems1000-device-sim
#         (passes the variable from your shell; never bake secrets into the image)
FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY cli.py .
COPY sim/ sim/
COPY config/ config/

# Unprivileged user; .state (boot counter, daily message count) lives in a volume
RUN useradd --create-home --uid 10001 sim && mkdir -p /app/.state && chown sim /app/.state
USER sim
VOLUME ["/app/.state"]

# Headless: no keyboard. Extra arguments are appended, e.g.
#   docker run ... ems1000-device-sim --interval 10 --scenario overload
ENTRYPOINT ["python", "cli.py", "run", "--headless"]
