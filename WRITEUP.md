
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

## Synchronization Approach

## Failure Handling

## Autoscaling Policy

## Workload and Evaluation Results

## Major Tradeoffs and Lessons Learned