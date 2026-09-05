# RFC-003: Incremental Panel Status

## Ownership

`monitor.py` routes HTTP and serves the existing shell. `status_snapshot.py`
owns periodic sampling/singleflight. `log_index.py` owns inode/offset parsing.
`api_projection.py` owns summary and pagination. `proxy_store.py` owns proxy
mutation, health and redaction. `batch_traffic.py` owns traffic accounting.

## Implemented

- PANEL_STATUS_V2=1 starts the shared worker; disabling it stops background cost.
- Reader threads copy the latest snapshot; producer failure keeps last good
  value with stale state. Running interval 2s, idle 10s; one refresh at a time.
- Log cursor uses device/inode/path/offset; truncate/rotation resets; partial
  lines wait for newline. Work is capped at 4MiB per sample, tail and row counts
  bounded, huge lines dropped, failure/worker cardinalities bounded.
- `/api/status/v2` is an allowlisted summary with ETag/304, state version,
  sampled_at and stale. It requires the same auth as existing operational reads.
- `/api/logs?page=&page_size=` is bounded. `/api/proxies?page=` supports opt-in
  pages and rejects invalid pagination. Proxy data cached 3s; statistics 30s.
- Existing UI poll avoids overlap/hidden-tab work; running 2s, idle 10s, error
  backoff up to 30s. Fonts use one-day caching, not immutable unversioned caching.

## Remaining Before Activation

Legacy `/api/status` and unpaged proxies are retained for existing UI consumers.
The current UI still reads the legacy status shape, even with a shared producer;
it must not be described as fully switched to the small v2 summary. Add complete
traffic/rates/control projections, fetch details separately, and fall back on
incomplete v2 state. A warmup 503 is expected while the worker has no snapshot.

Cursor generation under concurrent updates, default proxy pagination, quarantine
adapter, hashed immutable static assets and producer-aware log rotation remain.
Never truncate a live log behind an open writer. Retention must constrain age and
total bytes without deleting active logs or changing accounting source data.
Statistics still use the existing full history scan on cache refresh; avoid
claiming completely incremental statistics or the final CPU target yet.

## Tests

`tests/test_status_snapshot.py`: append/no repeated parse, partial line,
rotation/truncation, summary redaction/size, pagination, ETag changes, concurrent
refresh, stale last-good value. `tests/test_monitor_http.py` retains legacy auth
and operator-action behavior. `tests/test_status_v2_http.py` verifies auth before
304, health anonymity, warmup and invalid pages. Browser responsive parity, rotation and
30-minute resource tests remain release gates in C.2-C.5.
