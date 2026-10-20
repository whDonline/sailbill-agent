FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.md LICENSE ./
COPY src ./src
RUN pip install --upgrade pip && pip install -e ".[llm]"

COPY scripts ./scripts
COPY samples ./samples
COPY harness ./harness
COPY eval ./eval

# regenerate samples so the image never ships stale fixtures
RUN python scripts/make_samples.py

ENTRYPOINT ["sailbill"]
CMD ["demo"]
