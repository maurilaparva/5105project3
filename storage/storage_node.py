from concurrent import futures
import threading
import uuid
import os
from datetime import datetime, timezone

import grpc
import project3_pb2 as pb
import project3_pb2_grpc as pb_grpc

GRPC_SERVER_PORT = os.environ.get("GRPC_SERVER_PORT", "50051")
NODE_TARGET      = os.environ.get("NODE_TARGET", f"storage-node:{GRPC_SERVER_PORT}")


class StorageService(pb_grpc.StorageServiceServicer):
    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._items: dict[str, pb.Item] = {}
        self._bids:  dict[str, list[pb.Bid]] = {}

    def Heartbeat(self, request, context):
        return pb.HeartbeatResponse(alive=True)

    def Create(self, request, context):
        # Called by the primary. Generates id + version.
        item = pb.Item(
            id=str(uuid.uuid4()),
            seller_id=request.seller_id,
            title=request.title,
            description=request.description,
            category=request.category,
            quantity=request.quantity,
            starting_price=request.starting_price,
            current_price=request.starting_price,
            status=pb.ITEM_AVAILABLE,
            version="1",
        )
        with self._lock:
            self._items[item.id] = item
        print(f"[storage:{NODE_TARGET}] Create id={item.id}")
        return pb.CreateResponse(item=item)

    def ReplicateItem(self, request, context):
        # Called on backups: store the exact item the primary built.
        item = request.item
        with self._lock:
            self._items[item.id] = item
        print(f"[storage:{NODE_TARGET}] ReplicateItem id={item.id} version={item.version}")
        return pb.ReplicateItemResponse(ok=True)

    def Get(self, request, context):
        with self._lock:
            item = self._items.get(request.item_id)
        if item is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Item {request.item_id!r} not found")
            return pb.GetResponse()
        return pb.GetResponse(item=item)

    def Search(self, request, context):
        with self._lock:
            results = list(self._items.values())
        if request.keyword:
            kw = request.keyword.lower()
            results = [i for i in results if kw in i.title.lower() or kw in i.description.lower()]
        if request.category:
            results = [i for i in results if i.category == request.category]
        if request.status != pb.ITEM_UNKNOWN:
            results = [i for i in results if i.status == request.status]
        if request.seller_id:
            results = [i for i in results if i.seller_id == request.seller_id]
        page  = request.page_size or 20
        total = len(results)
        return pb.SearchResponse(items=results[:page], total_count=total)

    def Update(self, request, context):
        with self._lock:
            item = self._items.get(request.item_id)
            if item is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                return pb.UpdateResponse()
            patch = request.item
            if patch.title:       item.title       = patch.title
            if patch.description: item.description = patch.description
            if patch.category:    item.category    = patch.category
            if patch.quantity:    item.quantity    = patch.quantity
            if patch.status:      item.status      = patch.status
            item.version = str(int(item.version) + 1)
            self._items[request.item_id] = item
        print(f"[storage:{NODE_TARGET}] Update id={request.item_id} version={item.version}")
        return pb.UpdateResponse(item=item)

    def StoreBid(self, request, context):
        # Called by the primary.
        with self._lock:
            item = self._items.get(request.item_id)
            if item is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                return pb.StoreBidResponse()
            bid = pb.Bid(
                bid_id=str(uuid.uuid4()),
                item_id=request.item_id,
                bidder_id=request.bidder_id,
                amount=request.amount,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            self._bids.setdefault(request.item_id, []).append(bid)
            is_winning = request.amount.amount_small > item.current_price.amount_small
            if is_winning:
                item.current_price.CopyFrom(request.amount)
                item.version = str(int(item.version) + 1)
                self._items[request.item_id] = item
        print(f"[storage:{NODE_TARGET}] StoreBid item={request.item_id} winning={is_winning}")
        return pb.StoreBidResponse(bid=bid, updated_item=item, is_winning_bid=is_winning)

    def ReplicateBid(self, request, context):
        # Called on backups: store the exact bid + updated item the primary computed.
        bid  = request.bid
        item = request.updated_item
        with self._lock:
            self._bids.setdefault(bid.item_id, []).append(bid)
            self._items[item.id] = item
        print(f"[storage:{NODE_TARGET}] ReplicateBid item={bid.item_id}")
        return pb.ReplicateBidResponse(ok=True)

    def ChangeAuction(self, request, context):
        status = "joined" if request.HasField("join") else "bid_received"
        return pb.ChangeAuctionResponse(item_id=request.item_id, status_update=status)

    def StateTransfer(self, request, context):
        with self._lock:
            items = list(self._items.values())
            bids  = [b for blist in self._bids.values() for b in blist]
        print(f"[storage:{NODE_TARGET}] StateTransfer items={len(items)} bids={len(bids)}")
        return pb.StateTransferResponse(items=items, bids=bids)

    def __init__(self) -> None:
        self._lock  = threading.Lock()
        self._items: dict[str, pb.Item] = {}
        self._bids:  dict[str, list[pb.Bid]] = {}

    def Heartbeat(self, request: pb.HeartbeatRequest, context: grpc.ServicerContext) -> pb.HeartbeatResponse:
        return pb.HeartbeatResponse(alive=True)

    def Create(self, request: pb.CreateRequest, context: grpc.ServicerContext) -> pb.CreateResponse:
        item = pb.Item(
            id=str(uuid.uuid4()),
            seller_id=request.seller_id,
            title=request.title,
            description=request.description,
            category=request.category,
            quantity=request.quantity,
            starting_price=request.starting_price,
            current_price=request.starting_price,
            status=pb.ITEM_AVAILABLE,
            version="1",
        )
        with self._lock:
            self._items[item.id] = item
        print(f"[storage:{NODE_TARGET}] Create id={item.id}")
        return pb.CreateResponse(item=item)

    def Get(self, request: pb.GetRequest, context: grpc.ServicerContext) -> pb.GetResponse:
        with self._lock:
            item = self._items.get(request.item_id)
        if item is None:
            context.set_code(grpc.StatusCode.NOT_FOUND)
            context.set_details(f"Item {request.item_id!r} not found")
            return pb.GetResponse()
        return pb.GetResponse(item=item)

    def Search(self, request: pb.SearchRequest, context: grpc.ServicerContext) -> pb.SearchResponse:
        with self._lock:
            results = list(self._items.values())
        if request.keyword:
            kw = request.keyword.lower()
            results = [i for i in results if kw in i.title.lower() or kw in i.description.lower()]
        if request.category:
            results = [i for i in results if i.category == request.category]
        if request.status != pb.ITEM_UNKNOWN:
            results = [i for i in results if i.status == request.status]
        if request.seller_id:
            results = [i for i in results if i.seller_id == request.seller_id]
        page  = request.page_size or 20
        total = len(results)
        return pb.SearchResponse(items=results[:page], total_count=total)

    def Update(self, request: pb.UpdateRequest, context: grpc.ServicerContext) -> pb.UpdateResponse:
        with self._lock:
            item = self._items.get(request.item_id)
            if item is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                return pb.UpdateResponse()
            patch = request.item
            if patch.title:       item.title       = patch.title
            if patch.description: item.description = patch.description
            if patch.category:    item.category    = patch.category
            if patch.quantity:    item.quantity     = patch.quantity
            if patch.status:      item.status       = patch.status
            item.version = str(int(item.version) + 1)
            self._items[request.item_id] = item
        print(f"[storage:{NODE_TARGET}] Update id={request.item_id} version={item.version}")
        return pb.UpdateResponse(item=item)

    def StoreBid(self, request: pb.StoreBidRequest, context: grpc.ServicerContext) -> pb.StoreBidResponse:
        with self._lock:
            item = self._items.get(request.item_id)
            if item is None:
                context.set_code(grpc.StatusCode.NOT_FOUND)
                return pb.StoreBidResponse()
            bid = pb.Bid(
                bid_id=str(uuid.uuid4()),
                item_id=request.item_id,
                bidder_id=request.bidder_id,
                amount=request.amount,
                timestamp=datetime.now(timezone.utc).isoformat(),
            )
            self._bids.setdefault(request.item_id, []).append(bid)
            is_winning = request.amount.amount_small > item.current_price.amount_small
            if is_winning:
                item.current_price.CopyFrom(request.amount)
                item.version = str(int(item.version) + 1)
                self._items[request.item_id] = item
        print(f"[storage:{NODE_TARGET}] StoreBid item={request.item_id} winning={is_winning}")
        return pb.StoreBidResponse(bid=bid, updated_item=item, is_winning_bid=is_winning)

    def ChangeAuction(self, request: pb.ChangeAuctionRequest, context: grpc.ServicerContext) -> pb.ChangeAuctionResponse:
        status = "joined" if request.HasField("join") else "bid_received"
        return pb.ChangeAuctionResponse(item_id=request.item_id, status_update=status)


def serve() -> None:
    server = grpc.server(futures.ThreadPoolExecutor(max_workers=8))
    pb_grpc.add_StorageServiceServicer_to_server(StorageService(), server)
    server.add_insecure_port(f"[::]:{GRPC_SERVER_PORT}")
    server.start()
    print(f"[storage:{NODE_TARGET}] listening on port {GRPC_SERVER_PORT}")
    server.wait_for_termination()


if __name__ == "__main__":
    serve()