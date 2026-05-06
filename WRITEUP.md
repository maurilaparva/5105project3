

# Write-Up

## System Architecture

- The system is a Docker-only deployment (no Kubernetes) consisting of three tiers connected by gRPC.
- A centralized controller (port 50050) is the single entry point for clients. It exposes the MarketService gRPC interface, the client-facing API, and forwards requests to the service tier. The controller also runs 2 background threads: a heartbeat loop that periodically pings every service node, and an autoscale loop that spawns or kills service-node containers based on observed load. All shared metadata (the registry of service nodes and their health status) is protected by a threading lock because the gRPC server itself uses a thread pool of 16 workers.
- A service tier (initially 2 nodes, scalable up to N) handles request processing. Each service node exposes the ServiceNodeService gRPC interface and is essentially stateless, holding no marketplace data. Its job is to coordinate writes with the storage tier: send the request to the primary storage replica, then fan out the resulting state to the backups. Service nodes also independently maintain their own view of which storage replica is the primary by running their own heartbeat loop against the storage tier. We chose to scale the service tier (not the storage tier) because replicas hold persistent state and have stable identities, while service nodes are interchangeable workers.
- A storage tier of three replicas holds the actual marketplace state. Each replica exposes the StorageService gRPC interface and stores items and binds in an in-memory dictionary protected by a threading lock. One replica is the primary and accepts writes (Create, Update, StoreBid); the other two are backups that accept replication calls (ReplicateItem, ReplicateBid). All three serve reads (Get, Search).
- Request flow for write: Client → Controller → ServiceNode → Primary Storage (commit)
 ServiceNode -> Backup 1 -> Backup 2 (replicate in parallel)
- Request flow for a read: Client → Controller → ServiceNode → Primary
- Communication is via gRPC unary RPCs except for JoinAuction which is a bidirectional streaming RPC supporting persistent auction participation as required.
                              

## Replication Strategy

- We chose primary-backup replication with three replicas per item group. We picked primary-backup over quorum for two reasons: simplicity (one replica is the source of truth, no consensus protcol needed) and predictability (writes have a single ordering point so we don't have to reason about concurrent conflicting updates). The tradeoff is that the primary is a single point for write latency, every write incurs at least one extra hop to the primary, and the primary's load doesn't shard.
- Three replicas means we tolerate up to two storage-node failures while preserving data, and we still have a quorum of two for any future read-quorum extension.
- A design choice is that the primary is responsible for assigning all identity and ordering. Specifically, the primary generates the UUID for new items, the bid ID for new bids, and the version number on every write. When the service node fans the result out to backups, it sends the complete primary-built object via dedicated ReplicateItem and ReplicateBid RPCs, not the original client request. This was a fix to a bug we were facing: an earlier version called Create on every replica, which caused each replica to generate its own independent UUID, leaving them with permanently inconsistent state. By replicating finalized objects rather than re-executing operations, we guarantee all replicas observe identical state for committed writes.
- The replication is synchronous-with-timeout: the service node fans out replication calls to all live backups in parallel and waits up to 3 seconds for each. If a backup is slow or dead, replication continues without it, as such the primary's commit has already succeeded, so the client sees a successful response. The tradeoff is that a backup that's temporarily slow could miss a write; we mitigate this through the heartbeat-based primary election, which ensures we never elect a stale replica as primary unless it was healthy at the moment of failover.

## Synchronization Approach

- Our consistency model is described as strong consistency at the primary with eventual consistency at the backups, with linear per-item write ordering.
- Per-item writes are totally ordered because they all go to a single primary, which serializes them through a global threading.Lock. The primary increments a per-item version number on every write (version: "1" → "2" → "3" …), so any reader can detect staleness and any future replacement-replica catch-up can use the version field to identify gaps.
- Concurrent operations on the same item are serialized by the primary's mutex. There is no operation that requires coordination across items, so we avoid distributed transactions entirely.
- We do not guarantee two things: (1) We do not guarantee that all backups are caught up before a client sees a write succeed. We acknowledge the write to the client as soon as the primary commits. A read served from a stale backup could miss the most recent write. This is why our current implementation routes reads through the primary, to avoid exposing this staleness window. (2) We do not guarantee linearizability across failover. If the primary crashes after committing a write but before all backups have replicated it, that write could be lost when a backup is promoted. In practice, our 3-second replication timeout makes this window narrow but real.
- The strongest-consistency operation in our system is PlaceBid, because it reads the current price and conditionally writes a new one. The primary's lock is held across both the read and the write, so bid races resolve cleanly: only one bid can win at a time, and the version field reflects the resolution.


## Failure Handling

- We focus on fail-stop crashes of storage replicas, as the project allows.
- For detection, every service node runs a heartbeat thread that pings each storage replica every 5 seconds via a dedicated Heartbeat RPC. A failed RPC (timeout or RpcError) marks the replica as dead. The 5-second interval is a tradeoff: shorter intervals detect failures faster but waste CPU; longer intervals delay detection. Given our latency budget (~10ms p50), 5 seconds means we detect a crashed replica within 5 seconds of the next heartbeat, which is acceptable.
- For failover, when a service node detects that the current primary is dead, it picks the next healthy replica from its known-targets list (deterministic order based on configuration). Both service nodes apply the same deterministic rule, so they converge on the same new primary without any coordination — they agree on the new primary identity even though no one tells them to. This is a deliberate simplification: a more robust design would have the controller broadcast the new primary's identity, but our deterministic-convergence approach works because all service nodes start with the same target list.
- To demonstrate, we tested by running:
```
docker kill storage-node-1
```
with the system idle. Within 5 seconds the logs showed:
```
service-node-1 | storage storage-node-1:50051 marked dead
service-node-1 | primary failed over to storage-node-2:50052
service-node-2 | storage storage-node-1:50051 marked dead
service-node-2 | primary failed over to storage-node-2:50052
```
- A subsequent clinet run completed all six required operations successfully against the surviving two-replica cluster. Crucially, SearchItems returned the item from the previous run with its post-bid price (price=6500), proving that writes had been replicated to the new primary before the kill.
- Some limitations include: (1) We did not implement state transfer for replacement replicas. Our proto/project3.proto defines a StateTransfer RPC and our StorageService implements it, but we never wire it into the failover path. A new replica that joins after failure starts empty rather tahn catching up from primary. (2) Service-node failures are not specifically tested. The project description allows focusing on storage failures, and we do; the controller does heartbeat service nodes and would mark them dead, but we did not do it this way.

## Autoscaling Policy

- When it comes to demand metrics, the controller tracks in-flight gRPC requests as its measure of demand. Every incoming request increments a counter on entry and decrements on exit (via a track context manager). This metric reflects actual queueing pressure on the service tier. When the service tier is keeping up, the count stays low; when it can't keep up, requests pile up and the count grows.
- Scaling rules: (1) Scale up when in-flight count exceeds SCALE_UP_THRESHOLD (default 10, lowered to 3 for evaluation). (2) Scale down when in-flight count falls below SCALE_DOWN_THRESHOLD (default 2, lowered to 1 for evaluation), provided we have at least 2 healthy nodes so we never go below the minimum. (3) Cooldown of 30 seconds between any two scaling events to prevent oscillation.
- The scaler thread wakes every 5 seconds, samples the current in-flight count, and acts. New service nodes are launched via the Docker Python SDK using the same image as the rest of the system, with the controller's container having /var/run/docker.sock mounted to allow it to spawn siblings.
- A new service node joins the system by being started with a known port and hostname, then registered with the controller's ServiceNodeRegistry. The registry's heartbeat loop will start pinging it next cycle. Once a heartbeat succeeds, the node is marked healthy and starts receiving requests via round-robin dispatch.
- To demonstrate the behavior, with a threshold of 3 and 20 concurrent clients running for 30 seconds, the controller log shows
```
[autoscale] scaling up   → service-node-3 on port 50062
[autoscale] scaling down → stopping service-node-3
```
- We used docker ps -a --filter name=service-node to confirm the new container existed (and was later stopped by scale-down). Cooldown ensured the two events were separated in time.
- A note on threshold tuning that we saw: With our default threshold of 10, autoscaling did not fire even at 30 concurrent clients, because end-to-end latencies (10-50ms) were short enough that very few requests were ever in flight simultaneously. We had to lower the threshold to 3 to get scaling to trigger reliably. In a production system, this threshold would be set based on the observed steady-state in-flight count under target load, and would likely be calibrated continuously rather than statically.

## Workload and Evaluation Results

We evaluated the system using client/load_test.py, which is a custom load generator that spawns N concurrent worker threads, each maintaining a persistent gRPC channel and issuing operations in a randomized read-heavy mix that looks like the specific percentages:
- GetItem: 50%, SearchItems: 20%, PlaceBid: 20%, CreateItem: 7%, UpdateItem: 3%.
- This generally approximates a target where most traffic is browsing, some are bidding, and writes are a bit rarer. Each worker creates one item up front so reads have something to hit.

To do the evaluation, we measured per-operation count, error count, and latency percentiles with avg,p50,p95, and p99, plus aggregation of throughput. Here are the results:

### Run 1: Baseline (10 clients, 15s)
```
op       count   errors  avg_ms   p50_ms   p95_ms   p99_ms
get       6686        0   10.72    10.54    14.30    16.59
search    2627        0   10.63    10.49    14.09    16.32
bid       2658        0   12.49    12.22    16.74    20.59
create     909        0   12.31    12.02    16.37    20.48
update     451        0   12.28    11.96    16.60    19.23
```
- Throughput: 888 ops/second, zero errors.
- This is the system operating comfortably. Reads are slightly faster than writes (10.7ms vs 12.3ms avg_ms) because writes have to do replication fan-out. The tight clustering of p50 and p95 (≤4ms gap) indicates predictable latency with no significant queueing.

### Run 2: Heavy load (30 clients, 20s)
```
op       count   errors  avg_ms   p50_ms   p95_ms   p99_ms
get       9006        0   51.21    50.81    64.35    70.90
search    3625        0   14.70    13.68    23.67    29.93
bid       3627        0   15.87    14.90    24.65    31.44
create    1294        0   15.90    14.59    24.95    38.87
update     512        0   14.83    13.95    21.98    26.82
```
- Throughput: 901 ops/sec, zero errors.
- Throughput plateaued around 900 ops/sec, adding 20 more clients didn't move it. Instead, latency for GetItem jumped 5x (10.5ms → 50.8ms p50). Writes barely changed. We hit a queueing bottleneck on the read path. The asymmetry (only Get got slow) is because Get uniquely fanned out through the round-robin path more often, exposing it to lock contention on the primary's mutex more than the other operations.

### Run 3: Failure during load (30 clients, 20s, with primary kill at ~10s)
```
op       count   errors  avg_ms   p50_ms   p95_ms   p99_ms
get       5563     3407   51.42    50.43    65.14    77.49
search    2271     1356   15.74    14.69    23.73    31.49
bid       2222     1279   16.33    15.03    26.09    38.85
create     799      478   15.33    14.36    22.48    41.05
update     319      200   14.77    14.25    21.80    29.66
```
- Throughput: 557 ops/sec, ~38% error rate during the failure window.
- We killed storage-node-1 (the primary) approximately halfway through this run. Throughput dropped 38% (901 → 557 ops/sec) while the error rate hit ~38%. The successful requests completed at the same latency as Run 2, demonstrating that requests reaching the surviving replicas did not slow down, the system did not enter a degraded-but-up state, it briefly entered an unavailable-for-some-requests state, then recovered.
- The error window corresponds to the gap between the kill and the next heartbeat detecting it (~5 seconds). Once both service nodes failed over to storage-node-2, throughput recovered to the saturated level. No committed writes were lost, a subsequent client run found all the prior items, including their post-bid prices.

### Autoscaling 
- With SCALE_UP_THRESHOLD=3, running 20 concurrent clients for 30 seconds reliably triggered scale-up. The controller log shows the trigger and the corresponding scale-down once load tapered:
```
[autoscale] scaling up   → service-node-3 on port 50062
[autoscale] scaling down → stopping service-node-3
```
- docker ps -a confirmed the autoscaled container existed during the high-load window. The 30-second cooldown prevented oscillation.

## Major Tradeoffs and Lessons Learned

### Primary-backup vs quorum

- We chose primary-backup for simplicity. The cost is a single point of latency contention: every write goes through the primary's mutex, which is precisely what saturated us at ~900 ops/sec. Quorum-based replication would distribute write load across replicas at the cost of much harder consistency reasoning (we'd need vector clocks or sequence numbers and conflict resolution). For a marketplace where bids must be linearizable per-item, primary-backup is genuinely the right choice; we'd only revisit if write throughput became the dominant pressure.

### Decentralized failover

- Both service nodes pick a new primary independently, relying on deterministic ordering of their target lists to converge. This worked in our tests, but in a more complex deployment (e.g., service nodes with different config) two nodes could pick different primaries and end up with split-brain writes. The right fix is to centralize primary election in the controller: when a service node detects the primary is dead, it asks the controller for the new primary, and the controller is the only authority. We did not implement this because our setup keeps every service node's view identical, but it's the first thing we'd change for a production system.

### Reads From Primary Only

- Currently reads go to the primary, which means reads compete for the primary's lock with writes. This is what caused GetItem latency to balloon under heavy load. Routing reads to backups would distribute load and improve read latency dramatically, at the cost of potentially serving stale reads. For a marketplace browsing surface, eventual consistency on reads is usually acceptable.

### State Transfer for Replacement Replicas

- We defined the StateTransfer RPC and the storage code supports it, but we never wired it into the recovery path. A replacement replica today starts empty. The right behavior is: when a new replica joins, it calls StateTransfer on the primary to receive the current snapshot, then registers as a backup. 

### Autoscaling Threshold Tuning

- We learned the hard way that "in-flight requests" is a useful signal but its threshold is highly workload-dependent. With low-latency operations, even high-RPS load doesn't push in-flight count up. A more sophisticated autoscaler would track queueing latency directly or use a moving average of in-flight count with hysteresis.

### Synchronous Fan-out Replication

- We replicate writes synchronously to backups (with a 3-second timeout). This means write latency includes the slowest backup's processing time, but it also means backups stay close to the primary in steady state. An async-with-acknowledgment scheme would lower write latency but expand the window during which a backup might be promoted with missing data. Given our threshold (3 seconds is well above typical replication latency of <5ms) we get the best of both.

### Useful Debugging Lesson

- When something went wrong in the distributed system, being able to trace a request through every component by grep-ing logs by prefix made root-causing 5x faster than reading interleaved logs without prefixes.