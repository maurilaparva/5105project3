## Project 3 Start-Up Instructions

*Make sure Docker Desktop is running*

### 1. Start the system

```bash
cd docker
docker compose up --build
```

Wait until you see lines like:

```
controller       | [controller] listening on port 50050
service-node-1   | [service:service-node-1:50060] primary storage = storage-node-1:50051
service-node-2   | [service:service-node-2:50061] primary storage = storage-node-1:50051
```

at which point the system is ready for clients.

### 2. Run the test client

The client exercises every required RPC: `CreateItem`, `GetItem`,
`SearchItems`, `UpdateItem`, `PlaceBid`, and the streaming
`JoinAuction`.

In a **separate terminal**, reopen in a devcontainer, and from inside the devcontainer:

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

Re-run the test client to confirm the system still serves requests
correctly with only two storage replicas surviving.

---

## Demonstrating autoscaling

Defaults are conservative for normal use. To make autoscaling visible
under our load test, lower the threshold in `docker/docker-compose.yml`:

```yaml
controller:
  environment:
    SCALE_UP_THRESHOLD: "3"
    SCALE_DOWN_THRESHOLD: "1"
```

Restart the stack with "docker compose up --build" and run the load generator with at least 20 clients.
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


## Re-generating the proto files

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

**`Conflict. The container name "/storage-node-X" is already in use`**, a previous run wasn't cleaned up. Run:
```bash
docker compose down --remove-orphans
```

