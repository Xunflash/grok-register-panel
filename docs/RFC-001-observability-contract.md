# Panel Observability Contract

Panel projections own bounded output, not risk decisions. Never infer account
risk from Guard timeout, credential errors, quota or failed exit health.
Canonical outcome/decision metadata is defined in the sibling Guard RFC-001.

`webui/api_projection.py` allowlists v2 summary fields and strips unknown nested
objects. Raw logs and full proxies are not part of summary. Operational GETs
require the existing monitor token when configured; write APIs always require it.
Production must configure a strong token. Health and static shell reveal no
runtime identities. Pagination details require the same authentication.

State version is a content digest, not a sampling timestamp. `sampled_at` and
`stale` let readers distinguish freshness from unchanged content. ETag must never
allow a previously authenticated response to bypass authentication on a 304.
