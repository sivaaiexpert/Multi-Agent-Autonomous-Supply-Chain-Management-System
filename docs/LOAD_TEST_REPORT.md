# Load test report

Generated 2026-10-02 08:42:14. Reproduce with `make load-test` (`python -m evaluation.loadtest`).

**Scope.** Single-machine load test of one process. NOT a production-scale or capacity claim.

- Environment: Python 3.10.11, Windows-10-10.0.26200-SP0, 8 logical CPUs; uvicorn, 1 process, SQLite (WAL), local loopback
- Data: SYNTHETIC retailer profile (scale 0.06), 4 tenant(s); decisions made by the deterministic path (no LLM calls)
- A *workflow* is a complete lifecycle: start, wait at the human gate, approve, reach COMPLETED - all over real HTTP.

| Concurrent workflows | Wall s | Completed | Errors | Error rate | Throughput wf/s | E2E P50 / P95 / P99 ms | Queue wait P50 / P95 ms | DB lock errors |
|---:|---:|---:|---:|---:|---:|---|---|---:|
| 10 | 13.37 | 10 | 0 | 0.0% | 0.75 | 13231.52 / 13354.0 / 13359.88 | 0.63 / 14.14 | 0 |
| 50 | 30.61 | 50 | 0 | 0.0% | 1.63 | 22891.09 / 30242.23 / 30318.83 | 12706.19 / 21371.89 | 0 |
| 100 | 63.91 | 100 | 0 | 0.0% | 1.56 | 43957.37 / 62471.75 / 62788.8 | 30492.83 / 53208.92 | 0 |

## Detail

### 10 concurrent

- Outcomes: {'COMPLETED': 10}
- Error samples: none
- HTTP: 464 requests, 34.7 req/s
- Start request P50/P95/P99: 589.3 / 808.33 / 841.1 ms
- Poll request P50/P95/P99: 131.61 / 235.15 / 297.67 ms
- Time to human gate P50/P95: 9066.84 / 9480.66 ms
- Database: 2619 connections, held 159.32 s (writes 118.83 s) = 1192.0% of wall time. share can exceed 100% because many connections overlap; it is connection-held time, not CPU
- LLM latency: not measured: deterministic decision path (no model calls)

### 50 concurrent

- Outcomes: {'COMPLETED': 50}
- Error samples: none
- HTTP: 2366 requests, 77.3 req/s
- Start request P50/P95/P99: 1898.47 / 3775.59 / 3977.19 ms
- Poll request P50/P95/P99: 374.64 / 597.9 / 676.61 ms
- Time to human gate P50/P95: 21178.85 / 26772.39 ms
- Database: 8149 connections, held 771.56 s (writes 351.02 s) = 2520.3% of wall time. share can exceed 100% because many connections overlap; it is connection-held time, not CPU
- LLM latency: not measured: deterministic decision path (no model calls)

### 100 concurrent

- Outcomes: {'COMPLETED': 100}
- Error samples: none
- HTTP: 5173 requests, 80.9 req/s
- Start request P50/P95/P99: 2112.12 / 4630.18 / 5233.22 ms
- Poll request P50/P95/P99: 698.79 / 1202.46 / 1376.91 ms
- Time to human gate P50/P95: 41799.87 / 57838.08 ms
- Database: 17624 connections, held 1712.23 s (writes 727.79 s) = 2678.9% of wall time. share can exceed 100% because many connections overlap; it is connection-held time, not CPU
- LLM latency: not measured: deterministic decision path (no model calls)

## How to read this

- Queue wait is the time between a workflow being submitted and a worker thread starting it (the service has a small fixed worker pool, so it grows with concurrency).
- Numbers depend on the machine and on what else is running; compare runs on the same machine only.
- This does not establish how many tenants or workflows a production deployment can serve: that requires a test on the target infrastructure.
