# Stage 1: build o!ily's copy of Akatsuki's pp calculator (lazer/pp) into a Python wheel.
FROM python:3.11-slim-bookworm AS pp
RUN apt-get update && apt-get install -y --no-install-recommends curl ca-certificates build-essential \
    && rm -rf /var/lib/apt/lists/* \
    && curl -sSf https://sh.rustup.rs | sh -s -- -y --profile minimal --default-toolchain 1.97.0
ENV PATH=/root/.cargo/bin:$PATH
RUN pip install --no-cache-dir "maturin>=0.14,<0.15"
COPY pp /pp
RUN cd /pp/py && maturin build --release -i python3.11 -o /wheels

# Stage 2: the lazer server.
FROM python:3.11-slim-bookworm
COPY requirements.txt /tmp/requirements.txt
COPY --from=pp /wheels /tmp/wheels
RUN pip install --no-cache-dir -r /tmp/requirements.txt /tmp/wheels/*.whl
WORKDIR /app
COPY *.py ./
ENV DATA_DIR=/data
CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
