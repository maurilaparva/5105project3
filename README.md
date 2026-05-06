# CSCI 5105 — Project #3
## Fault-Tolerant, Replicated Marketplace with Autoscaling

A distributed backend for a simplified online marketplace, built with
Python, gRPC, and Docker. Implements primary-backup replication across
three storage replicas, heartbeat-based failure detection, primary
failover, and demand-based autoscaling of the service tier.

---

## Architecture at a glance

```
                     ┌──────────────┐
       Clients ─────▶│  Controller  │  (routes requests, autoscales,
                     │  :50050      │   monitors service nodes)
                     └──────┬───────┘
                            │
              ┌─────────────┴─────────────┐
              ▼                           ▼
      ┌──────────────┐            ┌──────────────┐
      │ service-node │   ...      │ service-node │  (1..N, autoscaled)
      │      :50060  │            │      :5006X  │
      └──────┬───────┘            └──────┬───────┘
             │                           │
             └───────────┬───────────────┘
                         ▼
              ┌──────────────────────┐
              │  storage replicas    │
              │  primary + 2 backups │  (heartbeated, failover-able)
              │  :50051 / 52 / 53    │
              └──────────────────────┘
```

- **Controller** (`controller/controller.py`): single entry point, tracks
  service-node health, makes scaling decisions.
- **Service nodes** (`service/service.py`): stateless request handlers.
  Each holds its own view of the storage primary; coordinates writes
  with primary + replicates to backups.
- **Storage nodes** (`storage/storage_node.py`): hold replicated state.
  One is elected primary; others are backups.

---

## Prerequisites

- **Docker Desktop** (with WSL2 integration enabled if on Windows)
- **VS Code** with the *Dev Containers* extension (recommended for development)
- Linux, macOS, or Windows + WSL2

No need to install Python, gRPC, or any other dependencies on the host
machine — everything runs inside containers.

---

## File layout

```
5105project3/
├── client/
│   ├── client.py          # smoke-test client
│   └── load_test.py       # load generator for evaluation
├── controller/
│   └── controller.py      # central controller
├── service/
│   └── service.py         # service-node implementation
├── storage/
│   └── storage_node.py    # storage-replica implementation
├── proto/
│   ├── project3.proto     # gRPC schema
│   └── src/               # generated _pb2 files (regenerated on build)
├── docker/
│   ├── Dockerfile
│   └── docker-compose.yml
├── .devcontainer/
│   └── devcontainer.json
├── requirements.txt
└── README.md
```

---

## Quick start

The system runs as a docker-compose stack. Bring it up from the
project's `docker/` directory.

### 1. Start the system

```bash
cd docker
docker compose up --build
```

This builds a single image (`project3-image:latest`) and starts:
- 1 controller (port `50050`, exposed to host)
- 2 service nodes (`service-node-1`, `service-node-2`)
- 3 storage replicas (`storage-node-1/2/3`)

Wait until you see lines like:

```
controller       | [controller] listening on port 50050
service-node-1   | [service:service-node-1:50060] primary storage = storage-node-1:50051
service-node-2   | [service:service-node-2:50061] primary storage = storage-node-1:50051
```

at which point the system is ready for clients.

### 2. Run the smoke-test client

The client exercises every required RPC: `CreateItem`, `GetItem`,
`SearchItems`, `UpdateItem`, `PlaceBid`, and the streaming
`JoinAuction`.

In a **separate terminal**, from inside the devcontainer:

```bash
python client/client.py
```

Or, equivalently, from the host using a one-shot Docker container that
joins the system's network:

```bash
docker run --rm \
  --network project3_net \
  -e CONTROLLER_HOST=controller \
  -e CONTROLLER_PORT=50050 \
  -v "$(pwd):/app" -w /app \
  -e PYTHONPATH=/app:/app/proto/src \
  project3-image:latest \
  python -u client/client.py
```

You should see output like:

```
create: id=... title=Vintage Camera price=5000 version=1
get:    id=... title=Vintage Camera price=5000 version=1
search: keyword='' total=1
update: id=... version=2
bid:    bid_id=... amount=5500 is_winning=True
...
auction 1: status=joined
auction 2: status=bid_received
```

### 3. Run the load generator (used for evaluation)

```bash
# Baseline: modest load
python client/load_test.py --clients 10 --duration 15

# Heavy load: triggers autoscaling
python client/load_test.py --clients 20 --duration 30
```

Output reports per-operation count, errors, and average / p50 / p95 /
p99 latencies, plus overall throughput.

### 4. Stop the system

```bash
docker compose down
```

---

## Demonstrating fault tolerance

With the system running and idle (or under load), in another terminal:

```bash
# Find the current primary in the logs:
docker compose -f docker/docker-compose.yml logs service-node-1 | grep "primary"

# Kill it (assume storage-node-1 is the primary):
docker kill storage-node-1
```

Within ~5 seconds you should see in the compose logs:

```
service-node-1 | [service:...] storage storage-node-1:50051 marked dead
service-node-1 | [service:...] primary failed over to storage-node-2:50052
service-node-2 | [service:...] storage storage-node-1:50051 marked dead
service-node-2 | [service:...] primary failed over to storage-node-2:50052
```

Re-run the smoke-test client to confirm the system still serves requests
correctly with only two storage replicas surviving.

---

## Demonstrating autoscaling

The controller monitors *in-flight requests* against the service tier.
When the count exceeds `SCALE_UP_THRESHOLD`, it spawns an additional
service-node container; when it drops below `SCALE_DOWN_THRESHOLD`, it
stops one (subject to a cooldown).

Defaults are conservative for normal use. To make autoscaling visible
under our load test, lower the threshold in `docker/docker-compose.yml`:

```yaml
controller:
  environment:
    SCALE_UP_THRESHOLD: "3"
    SCALE_DOWN_THRESHOLD: "1"
```

Restart the stack and run the load generator with at least 20 clients.
You should see lines like the following in the controller log:

```
[autoscale] scaling up   → service-node-3 on port 50062
[autoscale] scaling down → stopping service-node-3
```

Verify the new container existed (or still exists) with:

```bash
docker ps -a --filter "name=service-node"
```

---

## Configuration

All configuration is via environment variables defined in
`docker-compose.yml`.

| Variable               | Default | Component           | Meaning |
|------------------------|---------|---------------------|---------|
| `CONTROLLER_PORT`      | 50050   | controller          | gRPC listen port |
| `SCALE_UP_THRESHOLD`   | 10      | controller          | in-flight requests above this triggers scale-up |
| `SCALE_DOWN_THRESHOLD` | 2       | controller          | in-flight requests below this triggers scale-down |
| `HEARTBEAT_INTERVAL`   | 5       | controller, service | seconds between heartbeats |
| `COOLDOWN_SECONDS`     | 30      | controller          | minimum seconds between scale events |
| `IMAGE_NAME`           | project3-image:latest | controller | image used for autoscaled containers |
| `NETWORK_NAME`         | project3_net | controller     | docker network for autoscaled containers |
| `GRPC_SERVER_PORT`     | varies  | service, storage    | gRPC listen port |

---

## Re-generating the proto files

The generated `_pb2.py` and `_pb2_grpc.py` files are produced inside the
docker build, so a normal `docker compose up --build` regenerates them
automatically.

If you need to regenerate them manually inside the devcontainer (e.g.
after editing `proto/project3.proto`):

```bash
python -m grpc_tools.protoc \
  -I proto \
  --python_out=proto/src \
  --grpc_python_out=proto/src \
  proto/project3.proto
```

---

## Troubleshooting

**`docker: command not found`** — you're inside the devcontainer.
Docker commands run on the WSL/Linux/macOS host, not inside the
devcontainer. Open a separate host terminal.

**`Conflict. The container name "/storage-node-X" is already in use`**
— a previous run wasn't cleaned up. Run:
```bash
docker compose down --remove-orphans
```

**Client can't connect / `host.docker.internal` not resolved** — the
client tries to reach the controller via `host.docker.internal:50050`
by default. Override with environment variables:
```bash
CONTROLLER_HOST=localhost CONTROLLER_PORT=50050 python client/client.py
```
or run the client inside the docker network as shown in section 2.

**Autoscaling never fires** — under low latency the in-flight count
rarely climbs above the default threshold of 10. Lower
`SCALE_UP_THRESHOLD` to 3 (see autoscaling section above) and rerun
under load.