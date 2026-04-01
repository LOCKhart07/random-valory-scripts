# source_content Cleaned-Only Storage — Impact Analysis

**Date:** 2026-04-01
**Baseline:** [source_content_impact_report.md](source_content_impact_report.md) (2026-03-24 to 2026-03-31)
**Change:** Only cleaned source_content is allowed to be stored (raw HTML stripped)

## Per-Tool Payload Sizes (measured)

| Tool                         |  Off   | Cleaned  |    Raw     | Reduction (raw to cleaned) |
|------------------------------|--------|----------|------------|----------------------------|
| prediction_request           | 102 B  | 6.6 KB   | 3,583.4 KB | 99.8%                      |
| prediction_request_sme       | 84 B   | 11.9 KB  | 1,243.7 KB | 99.0%                      |
| prediction_request_rag       | 102 B  | 83.9 KB  | 1,992.4 KB | 95.8%                      |
| prediction_request_reasoning | 102 B  | 17.1 KB  | 1,432.5 KB | 98.8%                      |
| prediction_url_cot           | 2 B    | 2 B      | 2 B        | 0.0% (no pages fetched)    |
| superforcaster               | 56 B   | 3.7 KB   | 3.8 KB     | 2.5% (Serper JSON)         |

RAG stands out at 83.9 KB cleaned vs 6.6-17.1 KB for the others. This is expected — RAG tools retrieve more content chunks and retain more structured text after cleaning. Superforcaster is nearly unchanged because it already uses Serper JSON snippets, not raw HTML.

## Daily Storage by Tool Group

Using daily request volumes from the baseline report (7-day average, Gnosis + Polygon):

| Tool group                          | Requests/day | Off        | Cleaned/day | Raw/day    |
|-------------------------------------|-------------|------------|-------------|------------|
| prediction_request (offline/online) | ~3,716      | 370 KB     | 24.0 MB     | 13.0 GB    |
| prediction_request_reasoning        | ~2,368      | 236 KB     | 39.5 MB     | 3.3 GB     |
| superforcaster                      | ~2,750      | 150 KB     | 9.9 MB      | 10.2 MB    |
| prediction_request_rag              | ~260        | 26 KB      | 21.3 MB     | 0.5 GB     |
| resolve-market-reasoning            | ~66         | 7 KB       | 1.1 MB      | 92.5 MB    |
| prediction_request_sme              | ~20         | 2 KB       | 0.2 MB      | 24.3 MB    |
| resolve-market-jury                 | ~8          | 1 KB       | 0.1 MB      | 11.2 MB    |
| **Total (affected tools)**          | **~9,188**  | **~792 KB**| **~96 MB**  | **~16.9 GB** |

## Storage Comparison (three modes)

| Metric               | Off (current) | Cleaned only | Raw (rejected) |
|----------------------|---------------|--------------|----------------|
| **Storage/day**      | ~0.8 MB       | ~96 MB       | ~16.9 GB       |
| Storage/week         | ~5.5 MB       | ~672 MB      | ~118 GB        |
| Storage/month (30d)  | ~24 MB        | ~2.9 GB      | ~507 GB        |
| Storage/year (365d)  | ~290 MB       | ~35 GB       | ~6.2 TB        |

## Key Takeaway

Cleaned source_content reduces storage overhead by **99.4%** compared to raw — from ~16.9 GB/day down to ~96 MB/day. This makes it feasible to store source_content by default without overwhelming IPFS or operator storage.

```
Off ----[x120]----> Cleaned ----[x176]----> Raw
0.8 MB/day          96 MB/day              16.9 GB/day
```

The 120x increase over off is the cost of retaining cleaned source_content. Whether this is acceptable depends on the value of having source provenance for debugging, auditing, and accuracy analysis.

## Where the Bytes Go

Breakdown of the 96 MB/day cleaned storage by tool group:

```
prediction_request_reasoning   39.5 MB  (41.1%)  ████████████████████
prediction_request (off/onl)   24.0 MB  (25.0%)  ████████████
prediction_request_rag         21.3 MB  (22.2%)  ███████████
superforcaster                  9.9 MB  (10.3%)  █████
other (sme, resolve-market)     1.4 MB  ( 1.5%)  █
```

Reasoning tools dominate cleaned storage despite having fewer requests than prediction_request — their cleaned output is 2.6x larger per request (17.1 KB vs 6.6 KB). RAG is third in volume but has the largest per-request cleaned payload (83.9 KB), so a volume increase there would shift the balance.

## Notes

- Cleaned payloads strip raw HTML but retain extracted text, metadata, and structure. The cleaning ratio varies by tool because each tool fetches different page types and quantities.
- superforcaster is nearly storage-neutral across all three modes since it uses Serper API JSON (pre-structured snippets), not web page fetches.
- prediction_url_cot is excluded — it returned empty content in testing (requires `embedding_provider` to be set).
- The `unknown` tool category (~3,536/day) is excluded from affected totals. If some of these are prediction tools, actual cleaned storage could be higher.
- resolve-market tools are approximated using prediction_request_reasoning payload sizes (both are reasoning-based).
- Daily volumes are from the baseline report's 7-day window (2026-03-24 to 2026-03-31). Volumes fluctuate.

## Data Sources

- Per-tool payload sizes: measured by Jenslee across off/cleaned/raw modes
- Daily request volumes: mech-marketplace subgraphs (Gnosis + Polygon), 7-day average
- Baseline report: [source_content_impact_report.md](source_content_impact_report.md)
