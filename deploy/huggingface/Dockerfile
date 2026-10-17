# Hugging Face Spaces (Docker SDK) image for the facts-only assistant.
# The search index and chunks are bundled by deploy.py, so nothing is downloaded or OCR'd at startup.
FROM python:3.11-slim

RUN useradd -m -u 1000 user
ENV HOME=/home/user \
    HF_HOME=/home/user/.cache/huggingface \
    PIP_NO_CACHE_DIR=1 \
    PYTHONUNBUFFERED=1
WORKDIR /home/user/app

# CPU-only torch keeps the image small; the rest comes from pyproject.toml.
COPY --chown=user pyproject.toml README.md ./
COPY --chown=user app ./app
RUN pip install --index-url https://download.pytorch.org/whl/cpu "torch>=2.6,<2.9" \
 && pip install -e .

COPY --chown=user data ./data
USER user

# Bake the embedding model and reranker into the image so cold starts don't download them.
RUN python -c "from sentence_transformers import SentenceTransformer, CrossEncoder; \
SentenceTransformer('BAAI/bge-small-en-v1.5', device='cpu'); CrossEncoder('BAAI/bge-reranker-base', device='cpu')"

ENV APP_ENV=production \
    TRUST_PROXY_HEADERS=true \
    WARM_START=true
EXPOSE 7860
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "7860"]
