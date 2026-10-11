# SkyRules AI on a Hugging Face Docker Space (free CPU basic: 2 vCPU, 16 GB RAM).
#
# The Space repo holds only this file and deploy/hf/README.md (as README.md). The
# build clones the public GitHub repo at GIT_REF, installs the pinned requirements
# with CPU-only torch, and builds both indexes from the committed data/normalized/,
# exactly as `make chunk` and `make index` do on a laptop. Nothing large is committed.
#
# Space settings -> Variables and secrets:
#   secret   ANTHROPIC_API_KEY
#   variable LLM_PROVIDER=anthropic, ANTHROPIC_MODEL_ID=claude-haiku-5-5
#   variable DEMO_QUESTIONS_PER_HOUR=10, DEMO_DAILY_BUDGET_USD=2   (src/app/limits.py)
#   variable GIT_REF=<commit or tag> to pin the build (default: main)
FROM python:3.11-slim

RUN apt-get update && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

# Spaces run the container as uid 1000.
RUN useradd -m -u 1000 user
USER user
ENV HOME=/home/user \
    PATH=/home/user/.local/bin:$PATH \
    HF_HOME=/home/user/.cache/huggingface \
    PYTHONUNBUFFERED=1
WORKDIR /home/user

ARG GIT_REF=main
RUN git clone https://github.com/nayansathvikbukkapatnam/SkyRulesAI.git app \
    && cd app && git checkout "$GIT_REF" && git log -1 --format='built from %h %s'
WORKDIR /home/user/app

# CPU torch first (the Linux default pulls GB of CUDA), then the pinned set, as in CI.
RUN pip install --no-cache-dir --user --upgrade pip \
    && pip install --no-cache-dir --user \
       "torch==$(grep -i '^torch==' requirements.txt | cut -d= -f3)" \
       --index-url https://download.pytorch.org/whl/cpu \
    && pip install --no-cache-dir --user -r requirements.txt

# Both strategies: B3 uses struct@v1, the B2 compare view uses fixed@v1.
RUN python -m src.chunk.fixed && python -m src.index.store --strategy fixed \
    && python -m src.chunk.structure && python -m src.index.store --strategy struct

# Download the reranker now so the first visitor does not wait for it.
RUN python -c "from src.common.config import load_config; \
from src.retrieve.rerank import Reranker; Reranker.from_config(load_config('default')).model"

EXPOSE 7860
CMD ["python", "-m", "uvicorn", "src.app.server:app", "--host", "0.0.0.0", "--port", "7860"]
