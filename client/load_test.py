import argparse
import os
import random
import statistics
import threading
import time
from collections import defaultdict

import grpc
import project3_pb2 as pb
import project3_pb2_grpc as pb_grpc

CONTROLLER_HOST = os.environ.get("CONTROLLER_HOST", "host.docker.internal")
CONTROLLER_PORT = os.environ.get("CONTROLLER_PORT", "50050")
CONTROLLER_TARGET = f"{CONTROLLER_HOST}:{CONTROLLER_PORT}"

# Operation mix: read-heavy realistic marketplace traffic.
# 70% reads (Get/Search), 20% bids, 10% writes (Create/Update).
OP_WEIGHTS = [
    ("get", 50),
    ("search", 20),
    ("bid", 20),
    ("create", 7),
    ("update", 3),
]

# Shared across workers. Protected by lock.
_known_items: list[str] = []
_known_lock = threading.Lock()

# Per-operation latencies (ms). Protected by lock.
_latencies: dict[str, list[float]] = defaultdict(list)
_errors: dict[str, int] = defaultdict(int)
_metrics_lock = threading.Lock()

_stop = threading.Event()


def pick_op() -> str:
    ops, weights = zip(*OP_WEIGHTS)
    return random.choices(ops, weights=weights, k=1)[0]


def record(op: str, latency_ms: float, error: bool = False) -> None:
    with _metrics_lock:
        if error:
            _errors[op] += 1
        else:
            _latencies[op].append(latency_ms)


def add_item(item_id: str) -> None:
    with _known_lock:
        _known_items.append(item_id)


def random_item() -> str | None:
    with _known_lock:
        return random.choice(_known_items) if _known_items else None


def do_create(stub) -> str | None:
    t0 = time.perf_counter()
    try:
        resp = stub.CreateItem(pb.CreateItemRequest(
            seller_id=f"seller-{random.randint(0, 99)}",
            title=f"Item-{random.randint(1000, 9999)}",
            description="Load test item",
            category=random.choice(["Electronics", "Books", "Clothing", "Home"]),
            quantity=1,
            starting_price=pb.Money(currency_code="USD", amount_small=random.randint(1000, 10000)),
        ), timeout=10)
        record("create", (time.perf_counter() - t0) * 1000)
        return resp.item.id
    except grpc.RpcError:
        record("create", (time.perf_counter() - t0) * 1000, error=True)
        return None


def do_get(stub) -> None:
    item_id = random_item()
    if item_id is None:
        return
    t0 = time.perf_counter()
    try:
        stub.GetItem(pb.GetItemRequest(item_id=item_id), timeout=10)
        record("get", (time.perf_counter() - t0) * 1000)
    except grpc.RpcError:
        record("get", (time.perf_counter() - t0) * 1000, error=True)


def do_search(stub) -> None:
    t0 = time.perf_counter()
    try:
        stub.SearchItems(pb.SearchItemsRequest(
            category=random.choice(["Electronics", "Books", "Clothing", "Home"]),
            page_size=20,
        ), timeout=10)
        record("search", (time.perf_counter() - t0) * 1000)
    except grpc.RpcError:
        record("search", (time.perf_counter() - t0) * 1000, error=True)


def do_update(stub) -> None:
    item_id = random_item()
    if item_id is None:
        return
    t0 = time.perf_counter()
    try:
        stub.UpdateItem(pb.UpdateItemRequest(
            item_id=item_id,
            item=pb.Item(description=f"Updated at {time.time():.0f}", quantity=random.randint(1, 5)),
        ), timeout=10)
        record("update", (time.perf_counter() - t0) * 1000)
    except grpc.RpcError:
        record("update", (time.perf_counter() - t0) * 1000, error=True)


def do_bid(stub) -> None:
    item_id = random_item()
    if item_id is None:
        return
    t0 = time.perf_counter()
    try:
        stub.PlaceBid(pb.PlaceBidRequest(
            item_id=item_id,
            bidder_id=f"bidder-{random.randint(0, 99)}",
            amount=pb.Money(currency_code="USD", amount_small=random.randint(5000, 50000)),
        ), timeout=10)
        record("bid", (time.perf_counter() - t0) * 1000)
    except grpc.RpcError:
        record("bid", (time.perf_counter() - t0) * 1000, error=True)


def worker(worker_id: int) -> None:
    """One worker = one persistent gRPC channel running ops in a loop."""
    with grpc.insecure_channel(CONTROLLER_TARGET) as channel:
        stub = pb_grpc.MarketServiceStub(channel)

        # Each worker creates 1 item up front so reads have something to target.
        item_id = do_create(stub)
        if item_id:
            add_item(item_id)

        while not _stop.is_set():
            op = pick_op()
            if op == "get":
                do_get(stub)
            elif op == "search":
                do_search(stub)
            elif op == "bid":
                do_bid(stub)
            elif op == "update":
                do_update(stub)
            elif op == "create":
                new_id = do_create(stub)
                if new_id:
                    add_item(new_id)


def report(duration: float, num_clients: int) -> None:
    print()
    print("=" * 70)
    print(f"Load test results — {num_clients} clients, {duration:.1f}s")
    print("=" * 70)
    print(f"{'op':<10}{'count':>8}{'errors':>8}{'avg_ms':>10}{'p50_ms':>10}{'p95_ms':>10}{'p99_ms':>10}")

    total_ops = 0
    for op, _ in OP_WEIGHTS:
        latencies = _latencies.get(op, [])
        errors = _errors.get(op, 0)
        count = len(latencies)
        total_ops += count
        if count == 0:
            continue
        s = sorted(latencies)
        avg = statistics.mean(s)
        p50 = s[int(0.50 * len(s))]
        p95 = s[int(0.95 * len(s))]
        p99 = s[min(int(0.99 * len(s)), len(s) - 1)]
        print(f"{op:<10}{count:>8}{errors:>8}{avg:>10.2f}{p50:>10.2f}{p95:>10.2f}{p99:>10.2f}")

    throughput = total_ops / duration if duration > 0 else 0
    print("-" * 70)
    print(f"Total successful ops: {total_ops}")
    print(f"Throughput:           {throughput:.1f} ops/sec")
    print("=" * 70)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--clients", type=int, default=20, help="concurrent workers")
    parser.add_argument("--duration", type=float, default=15, help="run time (seconds)")
    args = parser.parse_args()

    print(f"Starting load test: {args.clients} clients for {args.duration}s")
    print(f"Targeting controller at {CONTROLLER_TARGET}")
    print()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(args.clients)]
    start = time.perf_counter()
    for t in threads:
        t.start()

    time.sleep(args.duration)
    _stop.set()

    for t in threads:
        t.join(timeout=5)
    end = time.perf_counter()

    report(end - start, args.clients)


if __name__ == "__main__":
    main()