# Panel Reliability Scope

Canonical stack PRD and executable A/B/C register:
`../../grok2api-account-risk-guard/docs/PRD-reliability.md` and `STORIES.md`.
The panel owns Story C and registration-side maintenance admission in A.3/B.5.
It does not classify Guard samples or lease API accounts.

Acceptance: status p95 <150ms, summary <20KB, proxy page <100KB, sustained
RSS <120MB and idle CPU <30% of a comparable measured baseline. No performance
claim is accepted without running/idle measurements and a 30-minute observation.
The existing panel and registration behavior remain default until the staged
production fallback, pagination and rotation checks pass. See RFC-003 for the
implementation contract and release measurements.
