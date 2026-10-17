# Project Guide: Mutual Fund Facts Assistant

A complete explanation of this project: what it does, how it works, the technology and terms involved, how it was built, and the problems that were solved along the way.

- **Live app:** <https://icici-mf-facts-assistant-b7ypyfssriac4pcnf8nybb.streamlit.app/>
- **Source code:** <https://github.com/aryansinghdev01/icici-mf-facts-assistant>
- **Setup instructions:** [README.md](../README.md)

---

## 1. What it is

The Mutual Fund Facts Assistant is a chatbot that answers **factual questions about five ICICI Prudential mutual fund schemes**, such as their expense ratio, exit load, minimum SIP, benchmark, riskometer level, or fund managers. It answers **only** from 17 official documents published by ICICI Prudential AMC and SEBI.

Every factual answer:

- is at most **three sentences**,
- cites **one official source link** (with the page number),
- shows **"Last updated from sources"** (the document's date), and
- carries the disclaimer **"Facts-only. No investment advice."**

It **refuses** to give investment advice ("Should I buy this fund?"), to calculate or compare returns, or to accept personal information (PAN, Aadhaar, account numbers, OTPs, email, phone).

**Example:**

> **You:** What is the exit load of ICICI Prudential Flexicap Fund?
>
> **Assistant:** The exit load is 1% of the applicable NAV if the amount is redeemed or switched out within one month from the date of allotment, and Nil after one month (w.e.f. April 6, 2026).
> *Source: ICICI Prudential Flexicap Fund — Factsheet (page 1). Last updated from sources: 31 Jul 2026.*

### Who it helps

- **Retail investors** comparing schemes who want quick, trustworthy facts without reading 100-page documents.
- **Support and content teams** who answer the same mutual fund questions repeatedly.

---

## 2. The problem and its constraints

General-purpose AI chatbots are risky for financial facts. They can **hallucinate** numbers, quote outdated figures, cite unofficial blogs, or drift into giving advice. This project was built to the opposite standard:

| Constraint | How the project meets it |
| --- | --- |
| Public, official sources only | 17 documents from ICICI Prudential AMC and SEBI; third-party sites (e.g. Groww) are blocked from ingestion and citation |
| One citation per answer | Citations come from the source registry, never from the AI model's text |
| No investment advice | Advice questions are detected and refused before any search or AI call |
| No performance claims | Return/performance questions are refused with a link to the official factsheet |
| No personal information | PII is detected and refused first; it never reaches the search, the AI model, or the logs |
| At most 3 sentences + "Last updated" | Enforced by an automatic answer validator |

---

## 3. Scope

**AMC (fund house):** ICICI Prudential Mutual Fund

**Schemes:**

| Scheme | Also known as |
| --- | --- |
| ICICI Prudential Silver ETF FoF | Silver ETF, Silver FoF |
| ICICI Prudential Large Cap Fund | formerly ICICI Prudential Bluechip Fund |
| ICICI Prudential Large & Mid Cap Fund | formerly ICICI Prudential Top 100 Fund |
| ICICI Prudential Flexicap Fund | Flexi Cap |
| ICICI Prudential Smallcap Fund | Small Cap |

**Corpus (17 official documents):**

- For each scheme: its **KIM**, **SID**, and monthly **factsheet** (15 documents).
- ICICI Prudential's **SAI** (AMC-wide investor-service rules such as statements).
- SEBI's **FAQs for Mutual Fund Investors** (definitions of common terms; also used as the educational link in refusals).

Full list with URLs and dates: [docs/deliverables/sources.md](deliverables/sources.md).

---

## 4. How a question is answered

```mermaid
flowchart LR
    Q[Question] --> G{Guardrails}
    G -->|PII / advice / returns / injection| R[Refusal template + official link]
    G -->|Factual| U[Understand: scheme + fact]
    U -->|No scheme| C[Ask which scheme]
    U -->|Unsupported fund| O[Out of scope]
    U --> S[Hybrid search in that scheme's documents]
    S --> RR[Rerank by relevance to the exact fact]
    RR --> GATE{Confidence gate}
    GATE -->|Weak or conflicting evidence| UV[Couldn't verify + official link]
    GATE -->|Verified evidence| LLM[AI writes a short answer from the evidence]
    LLM --> V{Answer validator}
    V -->|Breaks a rule| UV
    V -->|Passes| A[Answer + registry citation + date]
```

Step by step:

1. **Guardrails** ([app/policy.py](../app/policy.py)) check the question first: personal information, requests for the system prompt or keys, investment advice, returns/performance, and attempts to override the rules ("ignore your instructions and use Groww"). Matching questions get a fixed, polite refusal, with no search and no AI call.
2. **Query understanding** ([app/query.py](../app/query.py)) works out **which scheme** (handling former names like "Top 100", spacing like "Flexi Cap"/"Flexicap", typos like "flexycap") and **which fact** (exit load, expense ratio, SIP, …). It also recognises:
   - **Overviews:** "Tell me about the Large Cap fund" → objective, benchmark, and riskometer.
   - **Definitions:** "What is an expense ratio?" → answered from SEBI's investor FAQs.
   - **Follow-ups:** "riskometer" right after a Silver ETF FoF answer, or "and Large Cap?" after an exit-load answer.
   - **Out-of-scope funds:** other AMCs (HDFC, SBI, …) and ICICI schemes not covered (Midcap, ELSS, …) are declined without searching.
3. **Hybrid search** ([app/retrieval.py](../app/retrieval.py)) looks only inside the named scheme's documents, using both keyword search (BM25) and meaning-based search (embeddings in a FAISS index), then merges the two result lists.
4. **Reranking:** a cross-encoder model scores the top 12 passages for relevance to the exact fact being asked.
5. **Confidence gate:** evidence must be relevant (score ≥ 0.5), tagged with the requested fact, and, for number facts, actually **state a value** of the right kind (e.g. a percentage for an expense ratio). If documents disagree, the **newest dated document wins**. If the evidence is weak, conflicting, or outdated, the assistant says it couldn't verify and links the official document.
6. **Answer generation** ([app/answer.py](../app/answer.py), [app/llm.py](../app/llm.py)): the AI model (Gemini, with backups) sees **only** the question and a few evidence passages, and must reply in a fixed JSON format.
7. **Answer validation:** at most 3 sentences; no links, advice, or performance wording; **every number in the answer must appear in the cited evidence**. One retry if it fails, otherwise "couldn't verify".
8. **Citation:** the link, title, page, and date come from the **source registry** record of the evidence, never from the AI's text.

---

## 5. Technology stack

| Layer | Technology | Why it was chosen |
| --- | --- | --- |
| Language | Python 3.11+ | Standard for AI and document processing |
| Backend API | FastAPI + Uvicorn | Small, fast, typed API (`/api/chat`, `/api/sources`, `/api/health`) |
| Hosted UI | Streamlit (Streamlit Community Cloud) | Free hosting with enough memory for the search models |
| Local UI | Plain HTML/CSS/JavaScript | Lightweight page with strict security headers |
| PDF text extraction | PyMuPDF | Fast, page-accurate text extraction |
| OCR | Tesseract 5 | The factsheets are image-like PDFs that can only be read with OCR |
| Tokenizer | Hugging Face `tokenizers` (BGE tokenizer) | Measure chunk sizes in the embedding model's own tokens |
| Embeddings | `BAAI/bge-small-en-v1.5` (384 dimensions) via sentence-transformers | Strong, small open embedding model that runs on a CPU |
| Vector index | FAISS (`IndexFlatIP`) | Fast exact similarity search over 2,273 chunks |
| Keyword search | BM25 (`rank-bm25`) | Exact finance terms like "exit load" and "TER" |
| Reranker | `BAAI/bge-reranker-base` (cross-encoder) | Gives calibrated 0–1 relevance scores, used for the confidence threshold |
| Answer model | Google Gemini (`gemini-3.8-flash` + four fallback Flash/Flash-Lite models) | Free tier, structured JSON output |
| Backup model | Sarvam AI `sarvam-105b` | Used only if every Gemini model is unavailable |
| Validation | Pydantic | Typed, validated registry, chunks, and API requests |
| Testing | pytest, Streamlit AppTest | 140 automated tests, no network or AI calls |
| Version control | Git + GitHub | Public repository |

---

## 6. Glossary

### Mutual fund terms

| Term | Meaning |
| --- | --- |
| **Mutual fund** | A pool of money from many investors, managed by professionals and invested in stocks, bonds, or other assets |
| **AMC (Asset Management Company)** | The company that runs mutual funds, here ICICI Prudential AMC |
| **Scheme** | One specific fund offered by an AMC, e.g. ICICI Prudential Flexicap Fund |
| **NAV (Net Asset Value)** | The price of one unit of a fund on a given day |
| **KIM (Key Information Memorandum)** | A short official summary of a scheme: fees, minimums, exit load, risks |
| **SID (Scheme Information Document)** | The detailed official document describing a scheme |
| **SAI (Statement of Additional Information)** | AMC-wide document covering rules common to all its schemes (statements, services, legal details) |
| **Factsheet** | A monthly official report with a scheme's current expense ratio, portfolio, fund managers, and more |
| **Expense ratio / TER (Total Expense Ratio)** | The yearly fee charged by the fund, as a percentage of assets |
| **Direct plan vs Regular ("Other") plan** | Direct plans are bought straight from the AMC and have lower expense ratios; regular plans go through a distributor |
| **Exit load** | A fee charged if you redeem (sell) units before a set period, e.g. 1% within one month |
| **SIP (Systematic Investment Plan)** | Investing a fixed amount at regular intervals (daily, monthly, quarterly) |
| **Choti SIP** | A small-ticket monthly SIP (Rs. 250) aimed at first-time investors |
| **Lump sum** | A one-time investment |
| **Benchmark** | The market index a fund is measured against, e.g. NIFTY 100 TRI |
| **TRI (Total Return Index)** | An index that includes reinvested dividends |
| **Riskometer** | SEBI's six-level risk label for schemes: Low, Low to Moderate, Moderate, Moderately High, High, Very High |
| **Fund of Funds (FoF)** | A fund that invests in other funds, e.g. the Silver ETF FoF invests in ICICI Prudential Silver ETF |
| **ETF (Exchange Traded Fund)** | A fund traded on the stock exchange like a share |
| **ELSS / lock-in** | Tax-saving equity schemes with a 3-year lock-in; none of the five schemes here is an ELSS |
| **IDCW** | Income Distribution cum Capital Withdrawal, the payout option (formerly "dividend") |
| **CAS (Consolidated Account Statement)** | One monthly statement covering all your mutual fund holdings |
| **Capital gains statement** | A statement of gains/losses on redemptions, used for tax filing |
| **w.e.f.** | "With effect from", the date a rule starts applying |
| **SEBI** | Securities and Exchange Board of India, the market regulator |
| **AMFI** | Association of Mutual Funds in India, the industry body |
| **PAN / Aadhaar / folio number** | Personal tax ID / national ID / mutual fund account number, all treated as personal information and refused |

### AI and engineering terms

| Term | Meaning |
| --- | --- |
| **RAG (Retrieval-Augmented Generation)** | The AI first *retrieves* relevant passages from trusted documents, then *generates* an answer only from them |
| **LLM (Large Language Model)** | The AI model that writes the answer text (Gemini, Sarvam) |
| **Hallucination** | When an AI states something not supported by its sources; the validator blocks unsupported numbers |
| **Grounding** | Making every claim traceable to a source passage |
| **Corpus** | The set of documents the system answers from |
| **Ingestion pipeline** | Download → extract text → quality checks → split into chunks |
| **OCR (Optical Character Recognition)** | Reading text from images; used because the factsheets have no real text layer |
| **Chunk** | A small piece of a document (about 400 tokens here) that can be searched and cited |
| **Token** | A word piece the models count in; chunk sizes are measured in tokens |
| **Provenance** | The record of exactly where a chunk came from: document, page, date, URL |
| **Embedding** | A list of numbers that represents a text's meaning, so similar meanings are close together |
| **Vector index (FAISS)** | A data structure that finds the most similar embeddings quickly |
| **BM25** | A classic keyword-ranking algorithm, good at exact terms |
| **Hybrid search** | Combining keyword and embedding search |
| **RRF (Reciprocal Rank Fusion)** | A simple method to merge two ranked lists into one |
| **Reranker / cross-encoder** | A model that reads the question and a passage together and scores how relevant the passage is |
| **Confidence gate** | The rules deciding whether evidence is strong enough to answer |
| **Guardrails** | Checks that block unsafe requests (advice, PII, injection) before processing |
| **Prompt injection** | An attempt to make the AI ignore its rules ("ignore your instructions…") |
| **Structured output / JSON schema** | Forcing the AI to reply in a fixed machine-readable format |
| **Source registry** | The versioned list of approved documents ([data/source_registry.json](../data/source_registry.json)); the single source of truth for citations |
| **Allowlist** | The only website domains allowed: icicipruamc.com, sebi.gov.in, amfiindia.com |
| **Rate limit / quota** | Limits on how many requests are allowed per minute or per day |
| **Fallback chain** | Trying backup models in order when the main one is unavailable |
| **Reasoning model** | An AI model that "thinks" before answering (sarvam-105b) |
| **CSP (Content Security Policy)** | Browser rules that block injected scripts |
| **Audit log** | A record of what kind of answer was given (never the question text or personal data) |

---

## 7. How it was built

The project followed a phased plan ([docs/implementationPlan.md](implementationPlan.md)), with a test gate at the end of each phase ([docs/eval.md](eval.md)).

| Phase | What was built |
| --- | --- |
| **0 · Foundation** | FastAPI skeleton, environment-based configuration, domain allowlist (Groww blocked), PII detector, health endpoint |
| **1 · Ingestion** | Source registry; safe downloader (allowlist checked before and after redirects, SHA-256 hashes, idempotent reruns); page-by-page text extraction with two-pass OCR; quality gate (scheme name check, document date, duplicates, quarantine); chunking with full provenance; ingestion report |
| **2 · Retrieval** | Scheme/alias resolution, BGE + FAISS vector index, BM25, rank fusion, cross-encoder reranking, confidence gate with the newest-document rule, retrieval evaluation |
| **3 · Answers and guardrails** | Policy guardrails, AI answer generation with JSON output, answer validator, registry-based citations, `/api/chat` and `/api/sources`, rate limiting, privacy-safe audit log |
| **4 · User interface** | Single-page web UI with distinct states for answers, refusals, and errors; security headers |
| **5 · Evaluation and release** | End-to-end evaluation, sample Q&A, source list, README, disclaimer, GitHub repository |
| **After release** | Overview answers, follow-up questions with conversation context, SEBI/SAI general sources, Gemini quota handling and fallbacks, Sarvam AI backup, stale-answer protection, Streamlit hosting |

### By the numbers

- **17** official documents, about **950** pages processed
- **2,273** searchable chunks (about 400 tokens each, 60-token overlap, never crossing a page)
- **96%** of numbers on OCR-read factsheet pages confirmed by a second independent OCR pass
- **140** automated tests
- **25/25** retrieval evaluation cases passed
- **22/23** live end-to-end cases passed on the latest run (see [Evaluation](#10-evaluation))

---

## 8. Key engineering challenges and solutions

| Challenge | What happened | Solution |
| --- | --- | --- |
| **Python "froze" at startup** | The project sits on an iCloud-synced Desktop; the virtual environment's thousands of files loaded about 1000× slower | Moved the virtual environment outside iCloud (`~/.venvs/rag-chatbot`) |
| **HTTPS downloads failed** | The python.org macOS build ships without root certificates, and the factsheet server omits an intermediate certificate | Used the `certifi` bundle and shipped the missing public Sectigo intermediate (verified against a trusted root), keeping verification on |
| **Broken document links** | Two SID URLs in the plan returned 404; factsheet links were navigation pages with no data | Found the correct SID file names and the per-scheme factsheet PDFs |
| **Factsheets had no text** | They are Adobe Illustrator exports with every letter converted to a shape | Tesseract OCR on high-resolution page images, read **twice** (300 and 400 dpi); a number is trusted only if both readings agree |
| **Documents disagree** | e.g. Flexicap's October 2025 KIM says the exit load applies within 12 months; the July 2026 factsheet says one month | **Newest dated document wins**, and an older value is never shown if a newer document states a different one |
| **Footnotes looked like answers** | "@@ Base Expense Ratio is as on the last day of the month" scored as highly relevant but contains no number | Number facts must state a value of the right kind (a percentage, rupee amount, or duration) |
| **Noisy OCR passages ranked low** | Long chunks full of OCR noise diluted the relevant sentence | The reranker scores only the text around the requested fact |
| **Reranker returned NaN / crashed** | `transformers` 5.x produced NaN scores; FAISS and PyTorch each bundle OpenMP and crashed when FAISS loaded first | Pinned `transformers` < 5, switched to `bge-reranker-base`, and import PyTorch before FAISS |
| **AI quota ran out** | Gemini's free tier allows about 20 requests per day per model and is sometimes overloaded (HTTP 503) | Retries with backoff, skips exhausted models, five Gemini fallbacks, a one-hour answer cache, and Sarvam AI as a final backup |
| **Backup model returned nothing** | `sarvam-105b` spent its whole token budget "thinking"; with thinking off it mislabelled facts | Low reasoning effort, a larger budget, JSON mode, and truncated replies rejected |
| **Vague questions dead-ended** | "Tell me about the Large Cap fund" named no specific fact | Overview answers (objective, benchmark, riskometer), each verified separately |
| **Conversation went in circles** | Each question was handled in isolation, and "Silver ETF" wasn't recognised | Follow-up context (scheme and fact names only) and the "Silver ETF" alias |
| **Follow-up gave an outdated value** | "and Large Cap?" searched only those words and fell back to an older document | Carried-over facts are named in the search, plus the stale-value check |
| **Free hosting** | Hugging Face now requires a paid plan for Docker apps; most free hosts lack the ~1.5 GB of memory the models need | A Streamlit front end over the same engine, hosted free on Streamlit Community Cloud |

---

## 9. Safety and privacy design

- **Official sources only**, enforced by a domain allowlist at download time and again for every citation.
- **Citations come from the registry**, never from AI-generated text; links are shown only for allowlisted domains.
- **PII never travels:** it is detected first, refused, and never sent to search, the AI model, or the logs (only the category, e.g. "PAN", is logged).
- **No advice, no returns:** detected before any AI call, including "hypothetically" and "ignore your rules" phrasings.
- **Fail safe:** weak evidence, conflicts, AI outages, or validation failures all produce "couldn't verify" or "temporarily unavailable" with an official link, never a guessed answer.
- **Injection-resistant UI:** text is shown literally (never as HTML or links), plus a strict Content Security Policy on the local site.
- **No chat history stored:** conversation context lives only in the browser session.
- **Secrets** live only in `.env` (git-ignored) or the host's secret store, never in the repository.

---

## 10. Evaluation

| Check | Result |
| --- | --- |
| Automated tests (no network or AI calls) | 140 passed |
| Retrieval evaluation (25 cases) | Scheme resolution 100%, top-10 evidence recall 100%, cross-scheme evidence 0%, low-confidence leakage 0% |
| End-to-end evaluation (23 live cases) | 22/23 passed. The miss: "Top 100 benchmark" returned "couldn't verify" while running on the smallest backup model (a safe failure; it passes on the main model) |
| Manual verification | Every value in the sample Q&A was checked against the cited official page |

Reports are generated by `python -m app.eval_retrieval` and `python -m app.eval_e2e`. Sample answers: [docs/deliverables/sample_qa.md](deliverables/sample_qa.md).

---

## 11. Known limitations

- **Free-tier AI quotas:** about 20 requests per day per Gemini model; backups make answers slower (up to about a minute on Sarvam).
- **OCR text** on factsheets can contain minor noise, although numbers are double-checked.
- **Capital-gains statement steps** live on a JavaScript-only ICICI page that can't be ingested, so those questions get the official link instead of instructions.
- **Snapshot corpus:** documents were fetched in October 2026 and need a manual refresh (`python -m app.ingest && python -m app.index`); automatic monthly refresh is not built yet.
- **Coverage:** five schemes, English only.
- **Rule-based guardrails** may occasionally refuse an unusual but harmless phrasing.
- **Hosting:** the free Streamlit app sleeps after a few days without visitors and takes about a minute to wake.

---

## 12. Repository map

```
app/
  downloader.py   extractor.py   quality.py   chunker.py   ingest.py    ← ingestion pipeline
  index.py        query.py       retrieval.py                            ← search and confidence gate
  policy.py       llm.py         answer.py                               ← guardrails, AI models, answers
  main.py         static/                                                ← FastAPI API and local web page
  eval_retrieval.py   eval_e2e.py                                        ← evaluations
streamlit_app.py  ← hosted UI (Streamlit Community Cloud)
data/             ← source registry, pre-built chunks and search index
docs/             ← this guide, design documents, deliverables/
deploy/           ← Hugging Face Docker setup (requires a paid plan)
tests/            ← automated tests
```

For setup and commands, see the [README](../README.md).
