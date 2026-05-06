from concurrent import futures
import threading
import time
import os

import grpc
import project3_pb2 as pb
import project3_pb2_grpc as pb_grpc

GRPC_SERVER_PORT = os.environ.get("GRPC_SERVER_PORT", "50060")
NODE_TARGET      = os.environ.get("NODE_TARGET", f"service-node:{GRPC_SERVER_PORT}")
HEARTBEAT_INTERVAL = int(os.environ.get("HEARTBEAT_INTERVAL", "5"))

STORAGE_NODES = [
    "storage-node-1:50051",
    "storage-node-2:50052",
    "storage-node-3:50053",
]


class StorageRegistry:
    """Thread-safe primary-backup registry for storage replicas."""

    def __init__(self, targets: list[str]) -> None:
        self._lock    = threading.Lock()
        self._nodes   = {
            t: {
                "healthy": False,
                "stub": pb_grpc.StorageServiceStub(grpc.insecure_channel(t)),
            }
            for t in targets
        }
        self._targets = targets
        self._primary = None

    def mark_healthy(self, target: str) -> None:
        with self._lock:
            self._nodes[target]["healthy"] = True
            if self._primary is None:
                self._primary = target
                print(f"[service:{NODE_TARGET}] primary storage = {target}")

    def mark_dead(self, target: str) -> None:
        with self._lock:
            if self._nodes[target]["healthy"]:
                print(f"[service:{NODE_TARGET}] storage {target} marked dead")
            self._nodes[target]["healthy"] = False
            if self._primary == target:
                self._primary = next(
                    (t for t in self._targets if self._nodes[t]["healthy"]), None
                )
                print(f"[service:{NODE_TARGET}] primary failed over to {self._primary}")

    def primary(self) -> pb_grpc.StorageServiceStub | None:
        with self._lock:
            if self._primary and self._nodes[self._primary]["healthy"]:
                return self._nodes[self._primary]["stub"]
            return None

    def backups(self) -> list[pb_grpc.StorageServiceStub]:
        with self._lock:
            return [
                info["stub"]
                for t, info in self._nodes.items()
                if info["healthy"] and t != self._primary
            ]

    def all_targets(self) -> list[str]:
        with self._lock:
            return list(self._targets)


storage: StorageRegistry = None


def heartbeat_loop() -> None:
    while True:
        for target in storage.all_targets():
            try:
                stub = pb_grpc.StorageServiceStub(grpc.insecure_channel(target))
                stub.Heartbeat(pb.HeartbeatRequest(), timeout=2)
                storage.mark_healthy(target)
            except grpc.RpcError:
                storage.mark_dead(target)
        time.sleep(HEARTBEAT_INTERVAL)


def replicate(method: str, request) -> None:
    """Fan a replication call out to all backup replicas in parallel."""
    def _send(stub):
        try:
            getattr(stub, method)(request, timeout=3)
        except grpc.RpcError as e:
            print(f"[service:{NODE_TARGET}] replication error {method}: {e.details()}")

    threads = [
        threading.Thread(target=_send, args=(s,), daemon=True)
        for s in storage.backups()
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=3)


class ServiceNodeService(pb_grpc.ServiceNodeServiceServicer):

    def Heartbeat(self, request: pb.HeartbeatRequest, context: grpc.ServicerContext) -> pb.HeartbeatResponse:
        return pb.HeartbeatResponse(alive=True)

    def HandleCreate(self, request, context):
        primary = storage.primary()
        if primary is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            return pb.CreateResponse()
        resp = primary.Create(request)
        replicate("ReplicateItem", pb.ReplicateItemRequest(item=resp.item))
        print(f"[service:{NODE_TARGET}] HandleCreate id={resp.item.id}")
        return resp

    def HandleGet(self, request: pb.GetRequest, context: grpc.ServicerContext) -> pb.GetResponse:
        primary = storage.primary()
        if primary is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            return pb.GetResponse()
        return primary.Get(request)

    def HandleSearch(self, request: pb.SearchRequest, context: grpc.ServicerContext) -> pb.SearchResponse:
        primary = storage.primary()
        if primary is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            return pb.SearchResponse()
        return primary.Search(request)

    def HandleUpdate(self, request, context):
        primary = storage.primary()
        if primary is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            return pb.UpdateResponse()
        resp = primary.Update(request)
        replicate("ReplicateItem", pb.ReplicateItemRequest(item=resp.item))
        print(f"[service:{NODE_TARGET}] HandleUpdate id={request.item_id}")
        return resp

    def HandleStoreBid(self, request, context):
        primary = storage.primary()
        if primary is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            return pb.StoreBidResponse()
        resp = primary.StoreBid(request)
        replicate("ReplicateBid", pb.ReplicateBidRequest(
            bid=resp.bid,
            updated_item=resp.updated_item,
        ))
        print(f"[service:{NODE_TARGET}] HandleStoreBid item={request.item_id} winning={resp.is_winning_bid}")
        return resp

    def HandleAuction(self, request: pb.ChangeAuctionRequest, context: grpc.ServicerContext) -> pb.ChangeAuctionResponse:
        primary = storage.primary()
        if primary is None:
            context.set_code(grpc.StatusCode.UNAVAILABLE)
            return pb.ChangeAuctionResponse()
        return primary.ChangeAuction(request)


def serve() -> None:
    global storage
    storage = StorageRegistry(STORAGE_NODES)

    threading.Thread(target=heartbeat_loop, daemon=True).start()
    print(f"[service:{NODE_TARGET}] heartbeat thread started")

    server = grpc.server(futures.ThreadPoolExecutor(max_workers=16))
    pb_grpc.add_ServiceNodeServiceServicer_to_server(ServiceNodeService(), server)
    server.add_insecure_port(f"[::]:{GRPC_SERVER_PORT}")
    server.start()
    print(f"[service:{NODE_TARGET}] listening on port {GRPC_SERVER_PORT}")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()