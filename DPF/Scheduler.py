import hashlib
import json
import logging
import time

logger = logging.getLogger(__name__)


class Scheduler:
    """
    A node acting as scheduler after winning a PoUW election. Built from
    an existing Worker so it shares that node's blockchain, peers, and
    identity -- including the JSON-RPC client (worker._call_rpc), so
    every peer interaction here goes through the same /rpc endpoint the
    Worker itself listens on.
    """

    def __init__(self, worker):
        self.worker = worker
        self.node_id = worker.node_id
        self.peers = worker.peers
        self.blockchain = worker.blockchain
        self.difficulty = worker.difficulty

        self.pending_tasks = []
        self.processing_tasks = []
        self.completed_tasks = []

    def collect_pending_tasks(self):
        """Fan out to every peer's task-manager role via RPC and merge
        with our own locally-introduced tasks."""
        pending = []

        for peer in self.peers:
            result = self.worker._call_rpc(peer, "get_pending_tasks", {})
            if result:
                pending.extend(result)

        pending.extend(self.worker.collect_local_pending_tasks())

        unique = {tx["taskID"]: tx for tx in pending}
        # Sec. 3.1: process in ascending fairIndex order.
        self.pending_tasks = sorted(unique.values(), key=lambda t: t.get("fairIndex", 0))
        return self.pending_tasks

    def collect_completed_tasks(self):
        """Fan out for both completed and processing transactions, and
        include our own node's local pools too (not just peers')."""
        completed = list(self.worker.get_completed_pool())
        processing = list(self.worker.get_processing_pool())

        for peer in self.peers:
            c = self.worker._call_rpc(peer, "get_completed_tasks", {})
            if c:
                completed.extend(c)

            p = self.worker._call_rpc(peer, "get_processing_tasks", {})
            if p:
                processing.extend(p)

        self.completed_tasks = list({tx["taskID"]: tx for tx in completed}.values())
        self.processing_tasks = list({tx["taskID"]: tx for tx in processing}.values())
        return self.completed_tasks

    def dispatch_tasks(self):
        """Round-robin push of pending tasks to peers as an availability
        hint via RPC; workers still pull authoritatively via
        select_task once the next block lands."""
        assignments = {}
        workers = self.peers

        if not workers:
            return assignments

        for i, task in enumerate(self.pending_tasks):
            peer = workers[i % len(workers)]
            assignments.setdefault(peer, []).append(task)

        for peer, tasks in assignments.items():
            self.worker._call_rpc(peer, "assign_tasks", {"tasks": tasks})

        return assignments

    @staticmethod
    def _hash(obj):
        encoded = json.dumps(obj, sort_keys=True, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def create_block(self, pouw_proof):
        """Block schema aligned to the paper's Sec. 3.1 header fields."""
        prev_block = self.blockchain[-1] if self.blockchain else None
        prev_hash = prev_block["hash"] if prev_block else "0" * 64

        processing_ids = {tx["taskID"] for tx in self.processing_tasks}
        still_pending = [t for t in self.pending_tasks if t["taskID"] not in processing_ids]

        body = {
            "pending_transactions": still_pending,
            "processing_transactions": self.processing_tasks,
            "completed_transactions": self.completed_tasks,
        }

        block = {
            "index": len(self.blockchain) + 1,
            "hashPrevBlock": prev_hash,
            "time": time.time(),
            "diff": self.difficulty,
            "PoUW": pouw_proof,
            "transNum": sum(len(v) for v in body.values()),
            "scheduler": self.node_id,
            **body,
        }
        block["hashBody"] = self._hash(body)
        block["hash"] = self._hash(block)

        # append_block takes the shared lock, so this can't interleave
        # with a peer's block landing via receive_block on the RPC
        # server thread at the same moment.
        self.worker.append_block(block)
        self.blockchain = self.worker.blockchain

        return block

    def broadcast_block(self, block):
        for peer in self.peers:
            self.worker._call_rpc(peer, "receive_block", {"block": block})
