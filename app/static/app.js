// Facts-only assistant UI. All server text is inserted with textContent (never innerHTML),
// links come only from the API's citation fields, and nothing is stored in the browser.
(() => {
  "use strict";

  const MAX_CHARS = 500;
  const form = document.getElementById("ask-form");
  const input = document.getElementById("question");
  const submit = document.getElementById("submit");
  const thread = document.getElementById("thread");
  const count = document.getElementById("count");
  let busy = false;

  // response_type -> visual state and label (refusals, unverified, and errors look distinct from facts)
  const STATES = {
    fact: ["state-fact", "Answer"],
    navigation: ["state-info", "Official page"],
    clarify: ["state-info", "Which scheme?"],
    out_of_scope: ["state-info", "Outside supported schemes"],
    unverified: ["state-caution", "Couldn't verify"],
    refusal_advice: ["state-refusal", "No investment advice"],
    refusal_performance: ["state-refusal", "No return calculations"],
    refusal_pii: ["state-refusal", "Personal information not accepted"],
    refusal_policy: ["state-refusal", "Not available"],
    invalid: ["state-caution", "Please rephrase"],
    unavailable: ["state-error", "Temporarily unavailable"],
    rate_limited: ["state-error", "Slow down"],
    network_error: ["state-error", "Connection problem"],
  };
  const ALLOWED_LINK_HOSTS = ["icicipruamc.com", "sebi.gov.in", "amfiindia.com"];

  function el(tag, className, text) {
    const node = document.createElement(tag);
    if (className) node.className = className;
    if (text !== undefined) node.textContent = text;
    return node;
  }

  function safeOfficialUrl(url) {
    try {
      const parsed = new URL(url);
      const host = parsed.hostname.toLowerCase();
      const ok = parsed.protocol === "https:" && ALLOWED_LINK_HOSTS.some((d) => host === d || host.endsWith("." + d));
      return ok ? parsed.href : null;
    } catch (_) {
      return null;
    }
  }

  function formatDate(iso) {
    const date = new Date(iso + "T00:00:00");
    if (Number.isNaN(date.getTime())) return iso;
    return date.toLocaleDateString("en-IN", { day: "numeric", month: "short", year: "numeric" });
  }

  function sourceLine(source, prefix) {
    const url = safeOfficialUrl(source.url);
    if (!url) return null;
    const line = el("p");
    line.append(document.createTextNode(prefix));
    const link = el("a", null, source.title + (source.page ? " (page " + source.page + ")" : ""));
    link.href = url;
    link.target = "_blank";
    link.rel = "noopener noreferrer";
    line.append(link);
    return line;
  }

  function renderResponse(data) {
    let type = data.response_type in STATES ? data.response_type : "unverified";
    // UI-02: never present text as a factual answer without a citation.
    if (type === "fact" && !(data.source && safeOfficialUrl(data.source.url) && data.last_updated_from_sources)) {
      type = "unverified";
      data = { ...data, answer: "I couldn't verify that from the approved official sources." };
    }
    const [stateClass, label] = STATES[type];
    const card = el("article", "card " + stateClass);
    card.append(el("span", "label", label));
    card.append(el("p", "answer-text", data.answer || ""));

    const meta = el("div", "meta");
    const sources = [data.source, ...(data.additional_sources || [])].filter(Boolean);
    sources.forEach((source, i) => {
      const line = sourceLine(source, type === "fact" ? (i === 0 ? "Source: " : "Also: ") : "Official link: ");
      if (line) meta.append(line);
    });
    if (type === "fact") meta.append(el("p", null, "Last updated from sources: " + formatDate(data.last_updated_from_sources)));
    if (meta.childNodes.length) card.append(meta);
    card.append(el("p", "card-disclaimer", data.disclaimer || "Facts-only. No investment advice."));
    return card;
  }

  async function ask(question) {
    if (busy) return; // API-12/P4-07: one request at a time
    const text = question.trim();
    if (!text) {
      input.focus();
      return;
    }
    if (text.length > MAX_CHARS) return;

    busy = true;
    submit.disabled = true;
    input.value = "";
    updateCount();
    thread.append(el("div", "question-bubble", text));
    const loading = el("div", "loading");
    loading.append(el("span", "spinner"), el("span", null, "Checking the official documents…"));
    thread.append(loading);
    loading.scrollIntoView({ behavior: "smooth", block: "end" });

    let card;
    try {
      const response = await fetch("/api/chat", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ question: text }),
      });
      let data = null;
      try { data = await response.json(); } catch (_) { /* handled below */ }
      if (data && typeof data.answer === "string") {
        card = renderResponse(data);
      } else {
        card = renderResponse({ response_type: "invalid", answer: "That question couldn't be processed. Please rephrase it." });
      }
    } catch (_) {
      card = renderResponse({ response_type: "network_error", answer: "I couldn't reach the service. Please check your connection and try again." });
    } finally {
      loading.remove();
      busy = false;
      submit.disabled = false;
    }
    thread.append(card);
    card.scrollIntoView({ behavior: "smooth", block: "nearest" });
    input.focus();
  }

  function updateCount() {
    count.textContent = input.value.length + "/" + MAX_CHARS;
  }

  form.addEventListener("submit", (event) => {
    event.preventDefault();
    ask(input.value);
  });
  input.addEventListener("keydown", (event) => {
    if (event.key === "Enter" && !event.shiftKey) {
      event.preventDefault();
      ask(input.value);
    }
  });
  input.addEventListener("input", updateCount);
  document.querySelectorAll(".example").forEach((button) => {
    button.addEventListener("click", () => ask(button.textContent));
  });
  updateCount();
})();
