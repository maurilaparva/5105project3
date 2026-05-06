
# Run 1 (10 clients, baseline)

======================================================================
Load test results — 10 clients, 15.0s
======================================================================
op           count  errors    avg_ms    p50_ms    p95_ms    p99_ms
get           6686       0     10.72     10.54     14.30     16.59
search        2627       0     10.63     10.49     14.09     16.32
bid           2658       0     12.49     12.22     16.74     20.59
create         909       0     12.31     12.02     16.37     20.48
update         451       0     12.28     11.96     16.60     19.23
----------------------------------------------------------------------
Total successful ops: 13331
Throughput:           887.8 ops/sec
======================================================================

# Run 2 (30 clients, heavy load)

======================================================================
Load test results — 30 clients, 20.1s
======================================================================
op           count  errors    avg_ms    p50_ms    p95_ms    p99_ms
get           9006       0     51.21     50.81     64.35     70.90
search        3625       0     14.70     13.68     23.67     29.93
bid           3627       0     15.87     14.90     24.65     31.44
create        1294       0     15.90     14.59     24.95     38.87
update         512       0     14.83     13.95     21.98     26.82
----------------------------------------------------------------------
Total successful ops: 18064
Throughput:           900.6 ops/sec
======================================================================

# Run 3 (30 clients with kill mid-run)

======================================================================
Load test results — 30 clients, 20.1s
======================================================================
op           count  errors    avg_ms    p50_ms    p95_ms    p99_ms
get           5563    3407     51.42     50.43     65.14     77.49
search        2271    1356     15.74     14.69     23.73     31.49
bid           2222    1279     16.33     15.03     26.09     38.85
create         799     478     15.33     14.36     22.48     41.05
update         319     200     14.77     14.25     21.80     29.66
----------------------------------------------------------------------
Total successful ops: 11174
Throughput:           557.3 ops/sec
======================================================================

# Autoscaling fired and worked end-to-end

[autoscale] scaling up → service-node-3 on port 50062
[autoscale] scaling down → stopping service-node-3

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

## Autoscaling Policy

## Workload and Evaluation Results

## Major Tradeoffs and Lessons Learned