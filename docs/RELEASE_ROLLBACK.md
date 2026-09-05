# Panel Release Boundary

Canonical coordinator/runbook:
`../../grok2api-account-risk-guard/docs/RELEASE_ROLLBACK.md`.
Remote rollback: `/home/claw/rollback-grok-stack.sh check|show|rollback`.
The current baseline is `/home/claw/rollback-snapshots/20260905-current`.

Do not restart while registration, recovery, sync or import is active. Production
panel is a user systemd service at `/data/claw-relocated/grok-register-panel`;
health is port 1114 `/api/health`. Data/credentials remain outside Git. Preserve
the existing interpreter, browser runtime, UMask=0077 and external environment.

PANEL_STATUS_V2 defaults to 0. Do not enable until all C Stories and frontend
compatibility tests pass. Snapshot static check is not a completed restore drill.
Current script limitations and release blockers are in the canonical runbook.
