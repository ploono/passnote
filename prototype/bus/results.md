# Token test (session-b) — 10-item task handoff
Proxy estimate (no local tokenizer; BPE-ish heuristic). WRAP ≈180 tok harness overhead per received msg (assumed).
| fmt | chars | est tok | + wrap |
|---|---|---|---|
| A plain English | 608 | 138 | 318 |
| B codebook one-liners | 198 | 61 | 241 |
| C ref + 1 line | 50 | 23 | 203 (+~75 for receiver Read = 278) |
Findings: B is about 2.3x cheaper than A on payload. C only wins when the receiver skips or partially reads the file, or when the payload is large.
The fixed wrapper dominates small messages, so cutting the NUMBER of messages beats shortening them.
Rule: inline codebook for payloads under ~200 tok; REF above that.

## v2: tiktoken cl100k (still a proxy for Claude's tokenizer)
A plain=145, B codebook=68 (2.1x less), C ref=22 (+51 body if read, plus Read overhead).
Micro-check: 'ASK'=1, 'x7q#'=4, '🚀'=3, 'ratelimit'=3, '42 17 903'=5, so ciphers, emoji and numeric codes cost more. Confirms H4.
