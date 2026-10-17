# Mutual Fund Facts Assistant (ICICI Prudential)

A small retrieval-augmented FAQ assistant that answers **factual** questions about five ICICI Prudential mutual fund schemes, using only official AMC and SEBI documents. Every answer cites one official source and shows when that source was last updated. It refuses investment advice, return calculations, and personal information.

> **Facts-only. No investment advice.**

## Scope

**AMC:** ICICI Prudential Mutual Fund

**Schemes:** Silver ETF FoF · Large Cap Fund (formerly Bluechip) · Large & Mid Cap Fund (formerly Top 100) · Flexicap Fund · Smallcap Fund

**Corpus (17 official documents):** the KIM, SID, and monthly factsheet for each scheme (15), plus ICICI Prudential's Statement of Additional Information (statement and service rules) and SEBI's *FAQs for Mutual Fund Investors* (definitions; also the educational link in refusals). Groww pages were reference-only and are blocked from retrieval and citations. The full list is in [docs/deliverables/sources.md](docs/deliverables/sources.md).

**Answers:** expense ratio, exit load, minimum investment and SIP, benchmark, riskometer, fund managers, NAV, plans and options, investment objective, scheme document links, definitions of common terms, and where to get account or capital-gains statements.

## Deliverables

| Deliverable | Location |
| --- | --- |
| Working prototype | Run locally (see below), open <http://127.0.0.1:8000> |
| Source list (17 URLs) | [docs/deliverables/sources.md](docs/deliverables/sources.md) · [sources.csv](docs/deliverables/sources.csv) |
| Sample Q&A (10 real answers with links) | [docs/deliverables/sample_qa.md](docs/deliverables/sample_qa.md) |
| UI disclaimer snippet | [docs/deliverables/disclaimer.md](docs/deliverables/disclaimer.md) |
| Evaluation reports | `data/processed/e2e_eval.md`, `retrieval_eval.md`, `ingestion_report.md` (generated) |
| Design documents | [docs/](docs/): problem statement, architecture, implementation plan, edge cases, evaluation plan |

## Setup

**Prerequisites:** Python 3.11+, [Tesseract OCR](https://github.com/tesseract-ocr/tesseract) (`brew install tesseract` on macOS), and a free Gemini API key from [Google AI Studio](https://aistudio.google.com).

```bash
# 1. Virtual environment (keep it outside iCloud/Dropbox-synced folders; see Platform notes)
python3 -m venv ~/.venvs/rag-chatbot && source ~/.venvs/rag-chatbot/bin/activate
pip install -e '.[dev]'

# 2. Configuration: put your key in GEMINI_API_KEY. Never commit .env.
cp .env.example .env

# 3. Build the corpus: download, extract (with OCR), quality-check, chunk (~2 min)
python -m app.ingest

# 4. Build the search index (~3 min; first run downloads the BGE models, ~1.2 GB)
python -m app.index

# 5. Run the app and open http://127.0.0.1:8000
uvicorn app.main:app
```

**Checks:**

```bash
pytest                         # 115 automated tests, no network or model calls
python -m app.eval_retrieval   # retrieval metrics -> data/processed/retrieval_eval.md
python -m app.eval_e2e         # live end-to-end run -> e2e_eval.md + regenerates docs/deliverables/
```

## How it works

```
question ─► guardrails ─► scheme + fact detection ─► hybrid search ─► confidence gate ─► Gemini ─► answer checks ─► cited answer
            (PII, advice,   ("Top 100" → Large &      (BM25 + BGE,       (relevance, value     (evidence    (≤3 sentences,
             returns,        Mid Cap; unsupported      one scheme's       present, newest       passages     numbers must be
             injection)      funds declined)           documents only)    document wins)        only)        in the evidence)
```

1. **Guardrails run first** ([app/policy.py](app/policy.py)). Personal information (PAN, Aadhaar, account or folio numbers, OTPs, email, phone) is refused before search or the model, and is never logged. Advice ("should I buy", "which is best", including "hypothetically" framing) is refused with SEBI's investor FAQs. Return and performance questions are refused with the scheme's factsheet.
2. **Query understanding** ([app/query.py](app/query.py)) resolves the scheme, including former names, spacing variants, and typos, and the fact being asked. Questions with no scheme ("What is the exit load?") get a clarification. Other fund houses and unsupported ICICI schemes are declined.
3. **Hybrid retrieval** ([app/retrieval.py](app/retrieval.py)) runs BM25 and `bge-small-en-v1.5` vectors (FAISS) over only that scheme's chunks, then reranks with `bge-reranker-base`. The gate requires relevant evidence that actually states a value of the right kind (e.g. a percentage for an expense ratio).
4. **Source authority:** when documents disagree, the **newest dated document wins**. For example, the Flexicap KIM (Oct 2025) says the exit load applies within 12 months; the July 2026 factsheet says one month (w.e.f. April 6, 2026), and the factsheet is cited. Same-date conflicts return "couldn't verify".
5. **Generation and validation** ([app/answer.py](app/answer.py)). Gemini sees only the question and up to three evidence passages per scheme and returns structured JSON. Answers must be at most three sentences, contain no links, advice, or performance language, and **every number must appear in the cited evidence**. One retry, otherwise "couldn't verify" with the official link.
6. **Citations come from the registry**, never from the model. Each has the title, official URL, page, and document date shown as *Last updated from sources*.

**API:** `POST /api/chat` accepts only `{"question": "..."}`. `GET /api/sources` lists approved sources. `GET /api/health` reports model, index, and storage status without revealing credentials.

## Results

| Check | Result |
| --- | --- |
| Automated tests | 115 passed |
| Retrieval evaluation (24 cases) | Scheme resolution 100%, top-10 evidence recall 100%, cross-scheme evidence 0%, low-confidence leakage 0% |
| End-to-end evaluation (19 cases, eval plan P5-01–P5-09) | 19/19 passed: facts for all five schemes, document links, clarification, scope, and all refusal types |
| Manual verification | Every value in the sample Q&A was checked against the cited official page |

## Known limitations

- **Gemini free tier:** responses take about 5–25 seconds. During heavy load, Google returns "overloaded" (HTTP 503). The app retries and tries fallback models, then shows "temporarily unavailable" and never an unverified answer. On one test day about half of the calls failed this way; on the next, none did.
- **Factsheets are image-like PDFs** (outlined text with no fonts), so they are read with OCR. Each page is read twice at different resolutions, and a number is used only if both readings agree; OCR wording can still contain minor noise.
- **Capital-gains statement steps** live on ICICI Prudential's JavaScript-only Investor Services page, which cannot be ingested. Those questions are answered with the official link, not step-by-step instructions.
- **Snapshot corpus:** documents were fetched in October 2026 (factsheets dated July 31, 2026; KIM/SID October 2025, Silver ETF FoF February 2026). There is no automatic refresh yet (planned Phase 6); rerun `python -m app.ingest && python -m app.index` to update.
- **Coverage:** five schemes only. ELSS lock-in questions are declined because none of the selected schemes is an ELSS. English only.
- **Rule-based guardrails:** advice, returns, and PII detection use patterns tuned to the edge-case catalogue; unusual phrasings may be refused conservatively or need rephrasing.
- **Local prototype:** the rate limit and audit log are per process. There is no hosting, authentication, or HTTPS (required before any deployment).

## Project layout

```
app/        ingestion (downloader, extractor, quality, chunker, ingest), search (index, query, retrieval),
            answering (policy, llm, answer), API (main), UI (static/), evaluations (eval_retrieval, eval_e2e)
data/       source_registry.json (committed); raw_sources/, extracted_text/, processed/, indexes/, logs/ (generated, not committed)
docs/       design documents and deliverables/
tests/      automated tests
```

## Platform notes (macOS)

- Keep the virtual environment **outside iCloud-synced folders**. On an iCloud Desktop, imports were about 1000× slower.
- `torch` must be imported before `faiss` (both bundle OpenMP; the reverse order crashes); [app/index.py](app/index.py) enforces this.
- `transformers` 5.x and `sentence-transformers` 6.x produced invalid reranker scores here, so they are pinned below those versions.
- python.org builds ship without root certificates; downloads use `certifi`. The factsheet host omits an intermediate certificate, which is bundled in [app/certs/](app/certs/) after being verified against a trusted root.
