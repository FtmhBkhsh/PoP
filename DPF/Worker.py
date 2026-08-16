import hashlib
import json
import logging
import os
import random
import threading
import time

import pandas as pd
import psutil
import requests

from Config import load_config

logger = logging.getLogger(__name__)


class Worker:

    def __init__(self):
        config = load_config()

        self.node_id = config["node_id"]
        self.peers = config["peers"]
        self.chain_path = config.get("chain_file", "blockchain.json")
        self.difficulty = config.get("difficulty", 10_000_000)

        self.data_files = config["data_files"]
        self.task_manager_file = config.get("task_manager_file")
        self.task_column = config.get("task_column", "answer")
        self.chunk_size = config.get("task_chunk_size", 50)
        self.genesis_rows_per_file = config.get("genesis_rows_per_file", 5)

        self._dfs = {}  # filename -> DataFrame, loaded lazily and cached

        # Guards self.blockchain and the local task pools below, since
        # the RPC server thread (rpc_server.py) and the main DPF.py loop
        # both read/write this Worker concurrently.
        self._lock = threading.Lock()

        self.blockchain = self._load_chain()
        self._next_row = self._infer_next_row()
        self.selected_tasks = set()

        # Local pools: transactions this node knows about (its own, plus
        # whatever peers have pushed to it via RPC) that haven't yet
        # been folded into a block. A scheduler drains these via
        # get_processing_pool/get_completed_pool.
        self._processing_pool = []
        self._completed_pool = []
        self._assigned_tasks = []

        logger.debug(
            "[%s] Worker initialized, chain height=%d, manages=%s, next_row=%d",
            self.node_id, len(self.blockchain), self.task_manager_file, self._next_row
        )

    # ------------------------------------------------------------------
    # Local data access
    # ------------------------------------------------------------------

    def _load_file(self, filename):
        """Every node has every data file locally (data1.csv..data5.csv),
        so any worker can execute any task -- only *introducing* new
        tasks is restricted to the file's assigned task manager."""
        if filename not in self._dfs:
            self._dfs[filename] = pd.read_csv(filename, usecols=[self.task_column])
        return self._dfs[filename]

    # ------------------------------------------------------------------
    # Persistence / bootstrap
    # ------------------------------------------------------------------

    def _load_chain(self):
        """Load the local chain from disk, or bootstrap a deterministic
        genesis block if none exists yet."""
        if os.path.exists(self.chain_path):
            with open(self.chain_path) as f:
                return json.load(f)

        logger.info("[%s] No local chain found, bootstrapping genesis", self.node_id)
        genesis = self._make_genesis()
        self._save_chain(genesis)
        return genesis

    def _save_chain(self, chain=None):
        """
        Atomic write: dump to a temp file, fsync, then os.replace() the
        real path. os.replace is atomic on POSIX -- a process killed
        mid-write leaves either the old complete file or the new
        complete file, never a half-written one. (Previously this wrote
        json.dump() straight into blockchain.json; an interrupted write
        there is exactly what produced the JSONDecodeError you hit.)
        """
        data = chain if chain is not None else self.blockchain
        tmp_path = f"{self.chain_path}.tmp"

        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
            f.flush()
            os.fsync(f.fileno())

        os.replace(tmp_path, self.chain_path)

    @staticmethod
    def _hash(obj):
        encoded = json.dumps(obj, sort_keys=True, default=str).encode()
        return hashlib.sha256(encoded).hexdigest()

    def _make_genesis(self):
        """
        Deterministic genesis block: built from fixed inputs only (no
        time.time(), no node-specific values -- data_files and
        genesis_rows_per_file must be identical across every node's
        config.json), so every peer computes a byte-identical genesis
        independently.

        Seeds the first `genesis_rows_per_file` rows of EVERY file so
        real work exists from block 1, regardless of which node
        processes it. _infer_next_row() below already accounts for
        whatever genesis seeded, so ongoing task introduction resumes
        right after it without double-introducing rows.
        """
        tasks = []
        for filename in self.data_files:
            for row in range(self.genesis_rows_per_file):
                tasks.append({
                    "taskID": f"{filename}-row-{row:06d}",
                    "taskState": "pending",
                    "taskType": 1,
                    "srcURL": f"{filename}#row={row}",
                    "fairIndex": row,
                    "row": row,
                    "sourceFile": filename,
                })

        body = {
            "pending_transactions": tasks,
            "processing_transactions": [],
            "completed_transactions": [],
        }

        block = {
            "index": 1,
            "hashPrevBlock": "0" * 64,
            "time": 0,
            "diff": self.difficulty,
            "PoUW": None,
            "transNum": len(tasks),
            **body,
        }
        block["hashBody"] = self._hash(body)
        block["hash"] = self._hash(block)
        return [block]

    def _infer_next_row(self):
        """Resume this node's task-manager duty from wherever it left
        off, by scanning its own chain for the highest row index
        already introduced for its assigned file (genesis included)."""
        if not self.task_manager_file:
            return 0

        prefix = f"{self.task_manager_file}-row-"
        highest = -1

        for block in self.blockchain:
            for bucket in ("pending_transactions", "processing_transactions", "completed_transactions"):
                for tx in block.get(bucket, []):
                    tid = tx.get("taskID", "")
                    if tid.startswith(prefix):
                        highest = max(highest, int(tid[len(prefix):]))

        return highest + 1

    # ------------------------------------------------------------------
    # JSON-RPC client helper
    # ------------------------------------------------------------------

    def _call_rpc(self, peer, method, params):
        """POST one JSON-RPC 2.0 request to a peer's /rpc endpoint.
        Returns the result, or None on any failure/error (peer down,
        timeout, or an RPC-level error response) -- callers already
        treat "nothing back" as "skip this peer", same as before."""
        try:
            r = requests.post(
                f"{peer}/rpc",
                json={"jsonrpc": "2.0", "method": method, "params": params, "id": self.node_id},
                timeout=5,
            )
            r.raise_for_status()
            body = r.json()

            if "error" in body:
                logger.warning("[%s] %s on %s returned error: %s", self.node_id, method, peer, body["error"])
                return None

            return body.get("result")

        except requests.RequestException as e:
            logger.warning("[%s] Failed to contact %s: %s", self.node_id, peer, e)
            return None

    # ------------------------------------------------------------------
    # RPC-facing methods (called by rpc_server.py when THIS node is on
    # the receiving end of a peer's request)
    # ------------------------------------------------------------------

    def get_chain(self):
        with self._lock:
            return self.blockchain

    def receive_block(self, block):
        """Accept a broadcast block if it validly extends our chain."""
        with self._lock:
            prev_hash = self.blockchain[-1]["hash"] if self.blockchain else "0" * 64

            if block.get("index") != len(self.blockchain) + 1:
                return {"accepted": False, "reason": "wrong index"}
            if block.get("hashPrevBlock") != prev_hash:
                return {"accepted": False, "reason": "does not extend our chain"}
            if not self.validate_chain(self.blockchain + [block]):
                return {"accepted": False, "reason": "failed validation"}

            self.blockchain.append(block)
            self._save_chain()

        logger.info("[%s] Accepted block %d from network", self.node_id, block["index"])
        return {"accepted": True}

    def append_block(self, block):
        """Used by our OWN Scheduler after winning an election -- same
        lock as receive_block, so a concurrent peer broadcast can't
        interleave with our own append mid-write."""
        with self._lock:
            self.blockchain.append(block)
            self._save_chain()

    def get_pending_tasks(self):
        return self.collect_local_pending_tasks()

    def get_processing_pool(self):
        with self._lock:
            return list(self._processing_pool)

    def get_completed_pool(self):
        with self._lock:
            return list(self._completed_pool)

    def record_processing(self, transaction):
        with self._lock:
            self._processing_pool.append(transaction)
        return {"ok": True}

    def record_completed(self, transaction):
        with self._lock:
            self._completed_pool.append(transaction)
        return {"ok": True}

    def record_assigned(self, tasks):
        with self._lock:
            self._assigned_tasks.extend(tasks)
        return {"ok": True}

    # ------------------------------------------------------------------
    # Chain sync
    # ------------------------------------------------------------------

    def validate_chain(self, chain):
        """Recompute and check every block's body hash, block hash, and
        the hashPrevBlock linkage."""
        if not chain:
            return False

        body_keys = ("pending_transactions", "processing_transactions", "completed_transactions")

        for i, block in enumerate(chain):
            body = {k: block.get(k, []) for k in body_keys}

            if self._hash(body) != block.get("hashBody"):
                return False

            check = {k: v for k, v in block.items() if k != "hash"}
            if self._hash(check) != block.get("hash"):
                return False

            if i > 0 and block.get("hashPrevBlock") != chain[i - 1]["hash"]:
                return False

        return True

    def sync_chain(self):
        """Adopt the longest valid chain seen among peers, via RPC."""
        best_chain = self.blockchain

        for peer in self.peers:
            chain = self._call_rpc(peer, "get_chain", {})
            if chain and len(chain) > len(best_chain) and self.validate_chain(chain):
                best_chain = chain

        if best_chain is not self.blockchain:
            with self._lock:
                self.blockchain = best_chain
                self._save_chain()

        return self.blockchain

    # ------------------------------------------------------------------
    # Task processing
    # ------------------------------------------------------------------

    def select_task(self):
        """Select one pending task not already claimed by anyone. Fully
        open -- any node can pick up any pending task regardless of
        which file it came from, since every node has every data file
        locally."""
        unavailable = set(self.selected_tasks)

        for block in reversed(self.blockchain):
            for tx in block.get("processing_transactions", []):
                if tx.get("taskID"):
                    unavailable.add(tx["taskID"])
            for tx in block.get("completed_transactions", []):
                if tx.get("taskID"):
                    unavailable.add(tx["taskID"])

            for task in block.get("pending_transactions", []):
                task_id = task.get("taskID")
                if not task_id or task_id in unavailable:
                    continue

                self.selected_tasks.add(task_id)
                logger.info("[%s] Selected task %s", self.node_id, task_id)
                return task

        return None

    def execute_task(self, task):
        start = time.perf_counter()

        source_file = task.get("sourceFile", self.data_files[0])
        df = self._load_file(source_file)
        rows = df.iloc[[task["row"]]] if "row" in task else df

        mapper_output = []
        for text in rows[self.task_column]:
            if pd.isna(text):
                continue
            for word in str(text).lower().split():
                mapper_output.append((word, 1))

        elapsed = time.perf_counter() - start

        return {
            "task_id": task["taskID"],
            "mapper_output": mapper_output,
            "execution_time": elapsed,
            "processed_records": len(rows),
        }

    def measure_cpu(self, start_time, processed_records):
        """Estimate executed CPU instructions -- the PoUW quantity `m`."""
        elapsed = time.perf_counter() - start_time

        cpu_freq = psutil.cpu_freq()
        freq = cpu_freq.current if cpu_freq else 2500  # MHz fallback

        instructions = int(elapsed * freq * 1_000_000 * random.uniform(0.95, 1.05))
        instructions += processed_records * 15_000

        return instructions

    # ------------------------------------------------------------------
    # Broadcasts -- now JSON-RPC instead of mixed REST/RPC
    # ------------------------------------------------------------------

    def broadcast_processing(self, task):
        transaction = {
            "taskID": task["taskID"],
            "taskState": "processing",
            "blockHeight": len(self.blockchain),
            "workerID": self.node_id,
            "selectedTime": time.time(),
        }

        for peer in self.peers:
            self._call_rpc(peer, "submit_processing", {"transaction": transaction})

        self.record_processing(transaction)  # so our own next scheduler round sees it too
        return transaction

    def send_result(self, result):
        """Send processed output to the task's result collector. Per the
        paper the collector URL lives in the task's srcURL -- wire that
        through once a real collector endpoint exists."""
        logger.debug(
            "[%s] Result ready for task %s (%d records)",
            self.node_id, result["task_id"], result["processed_records"]
        )

    def broadcast_completed(self, task, m):
        transaction = {
            "taskID": task["taskID"],
            "taskState": "completed",
            "workerID": self.node_id,
            "completedTime": time.time(),
            "instruction": m,
        }

        for peer in self.peers:
            self._call_rpc(peer, "submit_completed", {"transaction": transaction})

        self.record_completed(transaction)
        return transaction

    # ------------------------------------------------------------------
    # Task-manager role (Sec. 3: this node's assigned data file)
    # ------------------------------------------------------------------

    def collect_local_pending_tasks(self):
        """
        Task-manager role: only the node whose task_manager_file matches
        introduces new rows from that file, one chunk at a time.
        Execution of those tasks is still open to any worker (see
        select_task) -- introducing and executing are separate
        responsibilities.
        """
        if not self.task_manager_file:
            return []

        df = self._load_file(self.task_manager_file)
        total_rows = len(df)

        if self._next_row >= total_rows:
            return []

        end = min(self._next_row + self.chunk_size, total_rows)
        new_tasks = [
            {
                "taskID": f"{self.task_manager_file}-row-{row:06d}",
                "taskState": "pending",
                "taskType": 1,
                "srcURL": f"{self.task_manager_file}#row={row}",
                "fairIndex": row,
                "row": row,
                "sourceFile": self.task_manager_file,
            }
            for row in range(self._next_row, end)
        ]

        logger.info(
            "[%s] Introduced %s rows %d-%d (of %d)",
            self.node_id, self.task_manager_file, self._next_row, end - 1, total_rows
        )
        self._next_row = end
        return new_tasks
