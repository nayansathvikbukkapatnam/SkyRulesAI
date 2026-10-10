# SkyRules AI Diagrams (Mermaid source)

Rendered images are saved next to this file (`fig1_architecture.png`, `fig2_query_flow.png`, `fig3_rechunk.png`) and are embedded in the Word proposal.
GitHub also renders Mermaid code blocks directly, so these blocks can be pasted into Markdown files in the repo.

## Figure 1. System architecture (`fig1_architecture.png`)

```mermaid
flowchart LR
    subgraph OFFLINE["Offline: ingestion and indexing (run once, re-chunk from saved text)"]
        direction TB
        A["faa.gov drone section<br/>HTML pages + PDFs"] -->|"robots.txt checked,<br/>1 req/s, User-Agent with email"| B["1. Scraper"]
        B --> C[("Raw store<br/>bytes, URL, timestamp, SHA-256<br/>manifest.jsonl")]
        C --> D["2. Extraction<br/>remove nav and footers, PDF text,<br/>tables as Markdown, OCR,<br/>dedupe, PII filter"]
        D --> E[("Normalized docs JSON<br/>doc_id, url, title, heading_path,<br/>page_type, effective_date, crawl_ts")]
        E --> F["3. Chunking<br/>A: fixed 512 tokens, 64 overlap<br/>B: structure-aware, heading path"]
        F --> G["4. Embedding<br/>bge-small-en-v1.5, 384 dim,<br/>L2-normalized, cosine"]
        G --> H[("5. Chroma vector DB<br/>deterministic IDs,<br/>page_type filter, delete path")]
        F --> I[("BM25 index")]
    end

    subgraph ONLINE["Online: question answering"]
        direction TB
        U["User question + chat history"] --> R["Follow-up rewriter"]
        R --> S["Safety router<br/>location, live, high-stakes,<br/>pilot type, injection"]
        S --> J["7. Hybrid retrieval<br/>dense + BM25, RRF fusion<br/>pool of 20"]
        J --> K["8. Cross-encoder reranker<br/>top 5"]
        K --> L{"Evidence<br/>gate"}
        L -->|"Pass"| N["9. Generation<br/>Claude Haiku 5.5 on Bedrock<br/>JSON sentences + chunk IDs"]
        L -->|"Below threshold"| M["Refusal message<br/>+ FAA referral"]
        N --> O["Answer checks<br/>citation validity, claim support,<br/>number and date match,<br/>25-word quote limit"]
        O -->|"Pass"| P["Chat interface<br/>cited answer, page dates,<br/>retrieval trace"]
        O -->|"Fail or error"| M
        M --> P
        P --> Q[("Query log<br/>queries.jsonl")]
    end

    OFFLINE ==>|"Chroma + BM25 indexes<br/>feed step 7 retrieval"| ONLINE
```

## Figure 2. Query data flow (`fig2_query_flow.png`)

Step numbers match the data flow description in Section 8 of the proposal.

```mermaid
flowchart LR
    subgraph P1["Steps 1 to 5: understand and retrieve"]
        direction TB
        A["1. Input<br/>user question + chat history"] --> B["2. Rewrite<br/>follow-ups become<br/>standalone questions"]
        B --> C["3. Route<br/>tag: normal, location or live,<br/>high-stakes, pilot type unclear,<br/>injection-like"]
        C --> D["4. Retrieve<br/>dense top 20 + BM25 top 20,<br/>RRF fusion to a pool of 20"]
        D --> E["5. Rerank<br/>cross-encoder keeps top 5"]
    end
    subgraph P2["Steps 6 to 10: answer or refuse"]
        direction TB
        F{"6. Gate<br/>score at or<br/>above τ?"}
        F -->|"Yes"| G["7. Generate<br/>JSON sentences with<br/>chunk IDs, or insufficient"]
        F -->|"No"| R["Refusal<br/>I don't have that information<br/>in the FAA pages I can search"]
        G -->|"Insufficient"| R
        G -->|"Answer"| H["8. Check each sentence<br/>valid citation, claim supported,<br/>numbers and dates match,<br/>no quote over 25 words"]
        H -->|"Too few supported<br/>sentences, or error"| R
        H -->|"Pass"| I["Cited answer<br/>source links +<br/>last updated dates"]
        I --> J["9. Display<br/>FAA referral if tagged location,<br/>live or high-stakes;<br/>retrieval trace"]
        R --> J
        J --> K[("10. Log<br/>query, scores, verdicts,<br/>latency, cost")]
    end
    P1 ==>|"top 5 chunks<br/>+ scores"| P2
```

## Figure 3. Re-chunk migration (`fig3_rechunk.png`)

```mermaid
flowchart LR
    subgraph S1["Rebuild from saved text"]
        direction TB
        A[("data/normalized/<br/>saved normalized text<br/>(no re-crawl)")] --> B["Generate struct@v1 chunks<br/>with deterministic IDs"]
        B --> C["Write new chunk manifest<br/>listing every expected chunk ID"]
        C --> D["Delete chunks where<br/>strategy == fixed@v1<br/>(Chroma metadata filter)"]
        D --> E["Upsert struct@v1 chunks<br/>into Chroma and rebuild<br/>the BM25 index"]
    end
    subgraph S2["Verify and measure"]
        direction TB
        F["Orphan check<br/>index IDs minus manifest IDs = empty<br/>manifest IDs minus index IDs = empty<br/>old strategy count = 0"]
        F --> G{"All three<br/>pass?"}
        G -->|"No"| H["Stop with an error<br/>and list mismatched IDs"]
        G -->|"Yes"| I["Re-run the dev evaluation set<br/>and report metric changes<br/>in 02_chunking"]
    end
    S1 ==> S2
```
