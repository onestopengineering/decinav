#!/usr/bin/env python3
"""Build "Parcel", a fictional demo repository for trying decinav.

Parcel is an invented delivery-tracking platform with twelve decisions. The demo
is built through the real workflow (init, propose, accept, attest, git commits),
so it shows every freshness state, a supersession, accepted and proposed
decisions, and a saved impact result.

    python scripts/build_demo.py /tmp/parcel
    decinav ui --project /tmp/parcel
"""
import json, os, shutil, subprocess, sys
from pathlib import Path

if len(sys.argv) != 2:
    sys.exit("usage: python scripts/build_demo.py <new-directory>")
ROOT = Path(sys.argv[1]).expanduser().resolve()
if ROOT.exists():
    sys.exit(f"refusing to overwrite existing path: {ROOT}")
# Prefer the installed command (pipx installs are isolated from this Python).
DECINAV = [shutil.which("decinav")] if shutil.which("decinav") else [sys.executable, "-m", "decinav"]
ENV = {**os.environ, "EDITOR": "true", "GIT_AUTHOR_NAME": "demo", "GIT_AUTHOR_EMAIL": "demo@example.com",
       "GIT_COMMITTER_NAME": "demo", "GIT_COMMITTER_EMAIL": "demo@example.com"}
ENV.pop("DECINAV_AI_CMD", None)

def run(*args):
    args = [part for arg in args for part in (arg if isinstance(arg, list) else [arg])]
    r = subprocess.run(args, cwd=ROOT, env=ENV, capture_output=True, text=True)
    if r.returncode != 0: sys.exit(f"FAILED {args}: {r.stdout}\n{r.stderr}")
    return r.stdout

def write(rel, text):
    p = ROOT / rel; p.parent.mkdir(parents=True, exist_ok=True); p.write_text(text)

def adr(i, title, status, date, domain, body, sources, depends=(), supersedes=()):
    rel = lambda items: "[]" if not items else "\n" + "\n".join(f'  - id: "{a}"\n    reason: {json.dumps(r)}' for a, r in items)
    ctx, dec, con = body
    return (f'---\nschema_version: 2\nid: "ADR-{i:04d}"\ntitle: {json.dumps(title)}\nstatus: "{status}"\n'
            f'date: "{date}"\ndomain: "{domain}"\ndepends_on: {rel(depends)}\nsupersedes: {rel(supersedes)}\n---\n\n'
            f"# {title}\n\n## Context\n\n{ctx}\n\n## {'Baseline' if status == 'baseline' else 'Decision'}\n\n{dec}\n\n"
            f"## Consequences\n\n{con}\n\n## Sources\n\n" + "".join(f"- {s}\n" for s in sources))

def slug(t): return "-".join("".join(c if c.isalnum() else " " for c in t.lower()).split())[:60].strip("-")

ROOT.mkdir(parents=True, exist_ok=True)
run("git", "init", "-q")
for rel, text in {
    "docker-compose.yml": "services:\n  api: {build: services/api}\n  ingest: {build: services/ingest}\n  web: {build: services/web}\n  postgres: {image: postgres:16}\n  redis: {image: redis:7}\n",
    "services/api/package.json": '{"name": "parcel-api", "dependencies": {"@nestjs/core": "^10"}}\n',
    "services/web/package.json": '{"name": "parcel-web", "dependencies": {"react": "^18"}}\n',
    "services/ingest/go.mod": "module parcel/ingest\n\ngo 1.22\n",
    "services/ingest/webhooks.go": "package ingest\n\n// Accept carrier webhooks and acknowledge fast.\n",
    "docs/specs/ingestion.md": "# Carrier ingestion\n", "docs/specs/data-model.md": "# Data model\n",
    "docs/specs/auth.md": "# Merchant auth\n", "docs/specs/status-machine.md": "# Status machine\n",
    "docs/specs/live-updates.md": "# Live updates\n", "docs/specs/eu-launch.md": "# EU launch\n",
    "docs/specs/estimates.md": "# Delivery estimates\n",
}.items(): write(rel, text)
run("git", "add", "."); run("git", "commit", "-qm", "Parcel monorepo")
run(DECINAV, "init", "--existing")

write("docs/adr/0001-parcel-platform-baseline.md", adr(1, "Parcel platform baseline", "baseline", "2025-03-04", "system", (
    "Parcel tracks shipments for online merchants across 40 carriers. This record captures the system as it stood when the team started recording decisions.",
    "A TypeScript API serves merchants and the dashboard. A Go service ingests carrier webhooks. PostgreSQL holds shipments, and Redis backs queues and caching. Everything runs as containers in one region.",
    "Later decisions refer to these components by name. Anything not listed here was not observed in the repository at the time."),
    ["docker-compose.yml", "services/api/package.json", "services/ingest/go.mod"]))

first = [
  (2, "Split carrier ingestion into a Go service", "2025-03-18", "system", (
    "Carrier webhooks arrive in bursts of thousands per minute at peak and stalled the API's event loop.",
    "Run ingestion as a separate Go service that accepts webhooks, acknowledges within 200 ms, and hands events onward. The API never receives carrier traffic directly.",
    "Webhook spikes no longer affect merchant API latency. There are two deployables instead of one, and carrier adapters live in Go."),
    ["docs/specs/ingestion.md", "services/ingest/go.mod"], [("ADR-0001", "Ingestion becomes one of the baseline's runtime components.")], []),
  (3, "Keep PostgreSQL as the system of record for shipments", "2025-03-20", "data", (
    "Support, billing, and carrier disputes all need one answer to where a shipment is.",
    "Every shipment and every status change is a row in PostgreSQL. Caches and queues are derived and can be rebuilt from it.",
    "There is one source of truth for audits. Read load is handled with indexes and replicas, not by moving data elsewhere."),
    ["docs/specs/data-model.md"], [("ADR-0001", "PostgreSQL is the baseline's primary store.")], []),
  (4, "Publish tracking events through Redis Streams", "2025-04-02", "infrastructure", (
    "The API needs every normalized tracking event, in order per shipment, without polling the ingestion service.",
    "The ingestion service writes each normalized event to a Redis Stream after the status row commits. The API consumes it with a consumer group.",
    "Cheap and already deployed. No cross-region replication. Redelivery must be idempotent."),
    ["docs/specs/ingestion.md"], [("ADR-0002", "The ingestion service is the producer."), ("ADR-0003", "Events publish only after the status row commits.")], []),
  (5, "Authenticate merchants with scoped API keys", "2025-04-15", "backend", (
    "Merchants integrate server to server and need separate rights for reading, writing, and webhooks.",
    "Merchants call the API with keys scoped to read, write, or webhooks. Keys are hashed at rest and shown once.",
    "Integrations stay simple. Dashboard users authenticate with sessions, which is a separate decision."),
    ["docs/specs/auth.md"], [("ADR-0001", "The API is the baseline's merchant entry point.")], []),
  (6, "Serve the dashboard as a React SPA behind the API", "2025-05-06", "frontend", (
    "The dashboard should never have capabilities the public API lacks.",
    "The merchant dashboard is a React single-page app that talks only to the public API. No server-side rendering.",
    "The dashboard dogfoods the API. First paint depends on API latency."),
    ["services/web/package.json"], [("ADR-0001", "The web client is a baseline component.")], []),
  (7, "Store raw carrier webhooks before parsing", "2025-06-10", "data", (
    "Carrier payloads change without notice, and a parser bug used to mean lost events.",
    "Every inbound webhook body is written unparsed to object storage with a request ID before any parsing happens.",
    "Parsing bugs can be replayed without asking carriers to resend. Storage grows by about 30 GB per month."),
    ["services/ingest/webhooks.go", "docs/specs/ingestion.md"], [("ADR-0002", "Raw capture happens at the ingestion boundary.")], []),
  (8, "Model shipment status as a server-side state machine", "2025-07-22", "backend", (
    "Carriers report out of order, and a late in-transit event used to overwrite delivered.",
    "Every status change goes through one state machine in the API that rejects illegal transitions such as delivered to in-transit.",
    "Out-of-order reports no longer corrupt status. Every new carrier status must be mapped explicitly."),
    ["docs/specs/status-machine.md"], [("ADR-0003", "Transitions are rows in the system of record.")], []),
  (9, "Stream live status to the dashboard over SSE", "2025-09-03", "frontend", (
    "Merchants kept refreshing the dashboard during peak season.",
    "The API pushes status changes to open dashboards over server-sent events.",
    "No polling. Long-lived connections need their own capacity planning."),
    ["docs/specs/live-updates.md"], [("ADR-0004", "The stream consumer is the event source."), ("ADR-0006", "The SPA is the client.")], []),
]
second = [
  (10, "Rate-limit webhook ingestion per carrier", "2026-08-18", "infrastructure", (
    "One carrier's retry storm delayed every other carrier's events for 40 minutes.",
    "Apply a token bucket per carrier at the ingestion edge, 2,000 requests per minute by default. Overflow returns 429 after raw capture.",
    "One misbehaving carrier cannot starve the others. Limits need per-carrier tuning."),
    ["docs/specs/ingestion.md"], [("ADR-0007", "Rejected bursts are still captured raw first.")], []),
  (11, "Move tracking events to a managed cross-region queue", "2026-09-02", "infrastructure", (
    "The EU launch requires tracking events to survive a regional outage.",
    "Replace Redis Streams with a managed queue that replicates across regions, keeping at-least-once delivery.",
    "Unblocks the EU launch. Adds a vendor dependency and a migration window with dual publishing."),
    ["docs/specs/eu-launch.md"], [("ADR-0002", "The ingestion service stays the producer.")],
    [("ADR-0004", "Redis Streams has no cross-region replication, which the EU launch requires.")]),
  (12, "Keep delivery estimates informational", "2026-09-20", "backend", (
    "Sales asked whether estimated delivery dates could trigger automatic refunds.",
    "Estimated delivery dates are shown to merchants but never drive status, refunds, or SLAs.",
    "A wrong estimate cannot trigger a wrong refund. Guaranteed windows would need their own decision."),
    ["docs/specs/estimates.md"], [("ADR-0008", "Estimates derive from the status history.")], []),
]

def propose(batch, name):
    records, sources = [], []
    for i, title, date, domain, body, srcs, deps, sups in batch:
        records.append({"filename": f"{i:04d}-{slug(title)}.md", "content": adr(i, title, "proposed", date, domain, body, srcs, deps, sups)})
        sources += [s for s in srcs if s not in sources]
    env = ROOT / ".decinav" / "proposals" / f"{name}.json"; env.parent.mkdir(parents=True, exist_ok=True)
    env.write_text(json.dumps({"records": records, "provenance": {"generator": "coding-agent", "sources": sources}}, indent=2))
    run(DECINAV, "propose", "--from-file", str(env))

propose(first, "history")
run(DECINAV, "accept", *[f"ADR-{i:04d}" for i in range(2, 10)])
run(DECINAV, "attest", "ADR-0001")
run("git", "add", "."); run("git", "commit", "-qm", "Record existing decisions [skip-adr]")
write("services/ingest/webhooks.go", "package ingest\n\n// Accept carrier webhooks, capture raw bodies, acknowledge fast.\n// Retries are deduplicated by request ID.\n")
run("git", "add", "."); run("git", "commit", "-qm", "Deduplicate carrier retries [ADR-0007]")
run(DECINAV, "attest", "ADR-0001", "ADR-0003", "ADR-0005", "ADR-0006", "ADR-0008")
propose(second, "eu-launch")
head = run("git", "rev-parse", "HEAD").strip()
(ROOT / ".decinav" / "last-impact.json").write_text(json.dumps({
  "schema_version": 1, "prd": "docs/prds/guaranteed-delivery-windows.md", "generated_at": "2026-10-11T09:30:00Z", "commit": head,
  "directly_affected_adrs": [
    {"id": "ADR-0012", "reason_for_change": "The PRD makes delivery estimates binding, which this decision explicitly rules out."},
    {"id": "ADR-0008", "reason_for_change": "Guaranteed windows need a new late status and the transitions into it."}],
  "prerequisite_adrs": [
    {"id": "ADR-0003", "reason_required": "Window commitments must be stored as shipment rows in the system of record."},
    {"id": "ADR-0009", "reason_required": "Merchants see window changes live through the existing SSE stream."}],
  "architectural_risks": [
    "Binding windows conflict with ADR-0012: refunds could fire on bad carrier data.",
    "Late detection depends on carriers reporting in order, which ADR-0008 tolerates but does not guarantee."],
  "proposed_new_adrs": ["Define how delivery-window guarantees are computed and refunded"],
  "mermaid": None}, indent=2))
print(run(DECINAV, "validate").strip())
print(f"Demo ready at {ROOT}\nOpen it with: decinav ui --project {ROOT}")
