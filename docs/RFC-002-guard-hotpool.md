# Panel and Guard Boundary

Panel launches registration/recovery and can honor a confirmed-risk pause.
Guard alone classifies accounts; API alone admits real inference. A capacity,
network, credential or quota outcome must not increment registration risk.

Existing exit ASN/ISP checks remain runtime-data-driven. Perform authorized
exit checks before a registration attempt; fail clearly on a denied/exhausted
exit, never rotate an active account identity mid-flow. Email/OTP failures do
not blacklist exits. Keep one proxy for the account lifecycle.

The panel consumes the shared maintenance boundary and risk-pause projection;
it does not implement risk classification, trusted-pool qualification or API
leases. Full trusted-pool rules and lease design belong to Guard RFC-002, not
the panel. Production rollout keeps strict admission and risk pause disabled
until the measured observation gate passes.
