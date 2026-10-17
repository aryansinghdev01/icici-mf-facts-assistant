# Mutual Fund FAQs — Facts-Only Q&A

## Milestone Brief

Build a small FAQ assistant that answers factual questions about mutual fund schemes using only official public pages. Examples include expense ratio, exit load, minimum SIP, ELSS lock-in, riskometer, benchmark, and downloading statements.

Every answer must include one source link. The assistant must not provide investment advice.

## Technology Decisions

- **LLM:** Gemini (`gemini-3.8-flash`, Google AI Studio free tier) generates grounded answers; refusals are fixed templates. (Changed from Grok on 2026-10-16; the model sits behind a provider-neutral adapter.)
- **Embedding model:** A BGE embedding model will create vector representations for the approved RAG corpus and user queries.
- The models must receive only the minimum approved evidence required for each request; credentials and model calls remain server-side.

## Intended Users

- Retail users comparing schemes.
- Support and content teams answering repetitive mutual-fund questions.

## Requirements

### Corpus Scope

- **Selected AMC:** ICICI Prudential Mutual Fund.
- **Selected schemes:** ICICI Prudential Silver ETF FoF, Large Cap Fund, Large & Mid Cap Fund, Flexicap Fund, and Smallcap Fund.
- **Approved RAG corpus:** 15 official ICICI Prudential AMC documents — one KIM, one SID, and one factsheet for each selected scheme.
- **Supplementary links:** Five Groww scheme pages are retained for reference only; they are excluded from retrieval, answer generation, and citations.
- Use the official KIM, SID, and factsheet documents to answer scheme-specific questions about fees, minimum investment amounts, exit loads, benchmarks, riskometer information, and other documented facts. Additional official AMC, SEBI, or AMFI pages may be added later when required for cross-scheme or statement-related FAQs.

### Selected Scheme Sources

Official ICICI Prudential AMC documents and factsheets are the assistant's approved retrieval and citation sources. Groww pages are supplied as supplementary scheme-reference links only and must not be used to answer questions or cite facts.

#### 1. ICICI Prudential Silver ETF FoF

- [Groww scheme page](https://groww.in/mutual-funds/icici-prudential-silver-etf-fof-direct-growth) — supplementary only.
- [Key Information Memorandum (KIM)](https://www.icicipruamc.com/blob/downloads/Key%20Information%20Memorandum%28KIM%29/ICICI%20Prudential%20Silver%20ETF%20FOF.pdf)
- [Scheme Information Document (SID)](https://www.icicipruamc.com/blob/downloads/Scheme%20Information%20Document%28SID%29/ICICI%20Prudential%20Silver%20ETF%20FOF.pdf)
- [Official factsheet](https://digitalfactsheet.icicipruamc.com/fact/icici-prudential-silver-etf-fund-of-fund.php?utm_source=chatgpt.com)

#### 2. ICICI Prudential Large Cap Fund

- [Groww scheme page](https://groww.in/mutual-funds/icici-prudential-large-cap-fund-direct-growth) — supplementary only.
- [Key Information Memorandum (KIM)](https://www.icicipruamc.com/blob/downloads/Key%20Information%20Memorandum%28KIM%29/ICICI%20Prudential%20Large%20Cap%20Fund.pdf)
- [Scheme Information Document (SID)](https://www.icicipruamc.com/blob/downloads/Scheme%20Information%20Document%28SID%29/ICICI%20Prudential%20Large%20Cap%20Fund.pdf)
- [Official factsheet](https://digitalfactsheet.icicipruamc.com/fact/icici-prudential-large-cap-fund.php?utm_source=chatgpt.com)

#### 3. ICICI Prudential Large & Mid Cap Fund

- [Groww scheme page](https://groww.in/mutual-funds/icici-prudential-top-100-fund-direct-growth) — supplementary only.
- [Key Information Memorandum (KIM)](https://www.icicipruamc.com/blob/downloads/Key%20Information%20Memorandum%28KIM%29/ICICI%20Prudential%20Large%20and%20Mid%20Cap%20Fund.pdf)
- [Scheme Information Document (SID)](https://www.icicipruamc.com/blob/downloads/Scheme%20Information%20Document%28SID%29/ICICI%20Prudential%20Large%20%26%20Mid%20Cap%20Fund.pdf)
- [Official factsheet](https://digitalfactsheet.icicipruamc.com/fact/icici-prudential-large-and-mid-cap-fund.php?utm_source=chatgpt.com)

#### 4. ICICI Prudential Flexicap Fund

- [Groww scheme page](https://groww.in/mutual-funds/icici-prudential-flexi-cap-fund-direct-growth) — supplementary only.
- [Key Information Memorandum (KIM)](https://www.icicipruamc.com/blob/downloads/Key%20Information%20Memorandum%28KIM%29/ICICI%20Prudential%20Flexicap%20Fund.pdf)
- [Scheme Information Document (SID)](https://www.icicipruamc.com/blob/downloads/Scheme%20Information%20Document%28SID%29/ICICI%20Prudential%20Flexicap%20Fund.pdf)
- [Official factsheet](https://digitalfactsheet.icicipruamc.com/fact/icici-prudential-flexicap-fund.php?utm_source=chatgpt.com)

#### 5. ICICI Prudential Smallcap Fund

- [Groww scheme page](https://groww.in/mutual-funds/icici-prudential-small-cap-fund-direct-growth) — supplementary only.
- [Key Information Memorandum (KIM)](https://www.icicipruamc.com/blob/downloads/Key%20Information%20Memorandum%28KIM%29/ICICI%20Prudential%20Smallcap%20Fund.pdf)
- [Scheme Information Document (SID)](https://www.icicipruamc.com/blob/downloads/Scheme%20Information%20Document%28SID%29/ICICI%20Prudential%20Smallcap%20Fund%20New.pdf)
- [Official factsheet](https://digitalfactsheet.icicipruamc.com/fact/icici-prudential-smallcap-fund.php?utm_source=chatgpt.com)

### FAQ Assistant

- Answer factual queries only, including questions about expense ratio, ELSS lock-in, minimum SIP, exit load, riskometer, benchmark, and downloading capital-gains statements.
- Display one clear citation link in every answer.
- Politely refuse opinionated or portfolio questions, such as “Should I buy or sell?”, with a facts-only message and a relevant educational link.

### User Interface

- A welcome line.
- Three example questions.
- The note: “Facts-only. No investment advice.”

## Constraints

- **Public sources only:** Do not use third-party blogs or app back-end screenshots as sources.
- **No PII:** Do not accept or store PAN, Aadhaar, account numbers, OTPs, email addresses, or phone numbers.
- **No performance claims:** Do not calculate or compare returns. When asked, link to the official factsheet instead.
- **Clarity and transparency:** Keep answers to three sentences or fewer and include “Last updated from sources: ”.

## Deliverables

- A working prototype link (app or notebook), or a demo video of three minutes or less if hosting is not possible.
- A CSV or Markdown source list containing the 15–25 URLs used.
- A README with setup steps, selected AMC and schemes, and known limitations.
- A sample Q&A file with 5–10 queries, assistant answers, and source links.
- The facts-only, no-advice disclaimer used in the UI.

## Skills Being Tested

| Skill | Focus |
| --- | --- |
| W1 — Thinking Like a Model | Identify the exact fact requested and determine whether to answer or refuse. |
| W2 — LLMs & Prompting | Use clear instructions, concise answers, polite safe refusals, and appropriate citation wording. |
| W3 — RAGs (only) | Build small-corpus retrieval with accurate citations from AMC, SEBI, and AMFI pages. |
