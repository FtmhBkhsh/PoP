import ast
import hashlib
import json
import logging
import threading
import time
from queue import Queue
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests
from flask import Flask, jsonify, request
from py_ecc import bls12_381 as b381
from py_ecc.optimized_bls12_381 import FQ

from Commitment import Commitment
from Config import Config

# --- Configuration / Constants -------------------------------------------------
logger = logging.getLogger("mapper_node")
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")

CURVE_ORDER = b381.curve_order

# Default endpoints (override via config if required)
REDUCER_ENDPOINTS = ["192.168.56.21:5000"]
MAPPER_ENDPOINTS = ["http://192.168.56.11:3000",
                    "http://192.168.56.12:3000"
                    # ,
                    # "http://192.168.56.13:3000",
                    # "http://192.168.56.14:3000",
                    # "http://192.168.56.15:3000"
                    ]
# --- Utilities ----------------------------------------------------------------

def make_json_safe(obj: Any) -> Any:
    """Recursively convert bytes to hex strings so objects are JSON serializable."""
    if isinstance(obj, (bytes, bytearray)):
        return obj.hex()
    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [make_json_safe(x) for x in obj]
    if isinstance(obj, tuple):
        return tuple(make_json_safe(x) for x in obj)
    return obj


# --- Blockchain Models --------------------------------------------------------
class Block:
    """Simple block container with JSON-safe serialization and hashing."""

    def __init__(
        self,
        index: int,
        data: Any,
        prev_hash: str,
        proof: Optional[Dict] = None,
        context: Optional[Any] = None,
        challenge: Optional[Dict] = None,
        commitment: Optional[Any] = None,
    ) -> None:
        self.index = index
        self.timestamp = time.time()
        self.data = data
        self.challenge = challenge
        self.proof = proof
        self.commitment = commitment
        self.prev_hash = prev_hash
        self.context = context

    @staticmethod
    def bytes_to_hex(obj: Any) -> Any:
        """Recursively convert bytes objects to hex strings."""
        return make_json_safe(obj)

    def to_dict(self) -> Dict[str, Any]:
        block_dict: Dict[str, Any] = {
            "index": self.index,
            "timestamp": self.timestamp,
            "data": self.data,
            "challenge": self.challenge,
            "prev_hash": self.prev_hash,
            "commitment": self.commitment,
            "proof": self.proof,
            "context": self.context,
        }

        block_dict = Block.bytes_to_hex(block_dict)
        # Stable JSON for hashing
        serialized = json.dumps(block_dict, sort_keys=True, ensure_ascii=False)
        block_hash = hashlib.sha256(serialized.encode()).hexdigest()
        block_dict["hash"] = block_hash
        return block_dict


class Blockchain:
    """In-memory blockchain (append-only)."""

    def __init__(self) -> None:
        self.chain: List[Block] = []
        self.create_genesis()

    def create_genesis(self) -> None:
        genesis = Block(index=0, data="Genesis", prev_hash="0", proof=None)
        self.chain.append(genesis)

    def add_block_to_chain(self, block: Block) -> None:
        self.chain.append(block)
        logger.info("Added block #%s hash=%s", block.index, getattr(block.to_dict(), "hash", "-"))

    def last_hash(self) -> str:
        # Hash of the last block dict
        if not self.chain:
            return "0"
        return self.chain[-1].to_dict()["hash"]


# --- Mapper Node --------------------------------------------------------------
class MapperNode:
    """Mapper node that probes data and receives external blocks.

    The node is initialized with the address of the challenge producer, and an
    optional mapper IP (used in commitments/context).
    """

    def __init__(self, challenge_producer_ip: str,my_ip) -> None:
        self.blockchain = Blockchain()
        self.trigger_lock = threading.Lock()
        self.challenge_producer_ip = challenge_producer_ip
        self.mapper_ip = my_ip
        self.current_challenge: Dict[str, Any] = self.get_new_challenge()
        self.block_queue: Queue = Queue()
        logger.info("Node started with initial challenge: %s", self.current_challenge)

    # ---- Challenge handling -------------------------------------------------
    def get_new_challenge(self) -> Dict[str, Any]:
        url = f"http://{self.challenge_producer_ip}:5000/challenge"
        try:
            resp = requests.get(url, timeout=3)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:  # pragma: no cover - network error handling
            logger.error("Failed to fetch new challenge from %s — %s", url, exc)
            # Return a safe default challenge structure to avoid runtime crashes
            return {"hash": "", "G": None, "H": None}

    def update_challenge(self) -> None:
        with self.trigger_lock:
            self.current_challenge = self.get_new_challenge()
            logger.info("Updated challenge")
            # to: %s", self.current_challenge)

    # ---- Probing / Mining --------------------------------------------------
    def probe_line(self, line: str) -> Tuple[List[Tuple[str, int]], Optional[Block]]:
        words = line.strip().split()
        word_counts = [(w, 1) for w in words]
        logger.debug("Word counts: %s", word_counts)

        # Compute a hash for candidate matching
        wc_hash = hashlib.sha256(str(word_counts).encode()).hexdigest()
        
        challenge = self.current_challenge

        # Parse outer JSON if needed
        if isinstance(challenge, str):
            challenge = json.loads(challenge)

        # Force everything into a list
        if isinstance(challenge, dict):
            challenge = [challenge]

        # Parse inner JSON strings
        normalized = []
        for ch in challenge:
            if isinstance(ch, str):
                ch = json.loads(ch)
            if isinstance(ch, dict):
                normalized.append(ch)

        self.current_challenge = normalized

        # trigger_hash = self.current_challenge.get("hash", "")
        # logger.debug("Comparing hashes: trigger=%s candidate=%s", trigger_hash, wc_hash)
        # if trigger_hash and trigger_hash == wc_hash:
        if any(challenge.get("hash") == wc_hash for challenge in self.current_challenge):
            logger.info("Found matching line for current challenge — creating commitment/proof")
            commitment, proof = self.make_proof(word_counts)
            block = Block(
                index=len(self.blockchain.chain),
                data=line,
                challenge=self.current_challenge,
                commitment=commitment,
                proof=proof,
                context=mapper_ip,
                prev_hash=self.blockchain.last_hash(),
            )
            return word_counts, block

        return word_counts, None

    def make_proof(self, word_counts: List[Tuple[str, int]]) -> Tuple[Any, Any]:
        """Create a Pedersen commitment and opening proof for the count.

        This function expects the challenge to contain 'G' and 'H' as string
        representations of points; it will parse them, compute a commitment and
        generate a proof using the Commitment helpers.
        """
        print(self.current_challenge)
        self.current_challenge=self.current_challenge[0]
        print(type(self.current_challenge))
        if not self.current_challenge:
            raise RuntimeError("No current challenge available for making proof")

        # Example: the message to commit is the length of the list
        m = len(word_counts)

        # Convert G/H from their string representations into Python tuples if needed
        G = ast.literal_eval(self.current_challenge["G"]) if self.current_challenge.get("G") else None
        H = ast.literal_eval(self.current_challenge["H"]) if self.current_challenge.get("H") else None

        # r2: use a deterministic pseudo-random secret derived from mapper IP (example)
        r2 = Commitment.ipv4_to_int(self.mapper_ip)

        commitment = Commitment.pedersen_commit(m, r2, G, H)
        proof = Commitment.prove_pedersen_opening(commitment, m, r2, G, H, context=self.mapper_ip.encode())
        return commitment, proof

    # ---- Networking / Sending ----------------------------------------------
    def send_results_to_reducers(self, word_counts: List[Tuple[str, int]]) -> None:
        for key, value in word_counts:
            for endpoint in REDUCER_ENDPOINTS:
                try:
                    requests.post(f"{endpoint}/reduce", json={"key": key, "value": value, "source": self.mapper_ip}, timeout=2)
                    break
                except Exception:
                    logger.exception("Failed to send word count to %s — continuing to next endpoint", endpoint)

    def send_block_to_peer(self, block_dict: Dict[str, Any]) -> Optional[requests.Response]:
        print("http://"+mapper_ip+":3000")
        MAPPER_ENDPOINTS.remove("http://"+mapper_ip+":3000")
        for endpoint in MAPPER_ENDPOINTS:
            serializable = make_json_safe(block_dict)
            attempt = 0
            max_retries = 3
            delay = 1
            while attempt < max_retries:
                try:
                    resp = requests.post(f"{endpoint}/addBlock", json=serializable, timeout=5)
                    resp.raise_for_status()
                    logger.info("sending block to %s in %d attempts", endpoint, attempt)
                    return resp  # Success
                except requests.RequestException:
                    attempt += 1
                    logger.warning(
                        "Failed to send block to %s (attempt %d/%d). Retrying in %d seconds...",
                        endpoint, attempt, max_retries, delay
                    )
                    time.sleep(delay)
            logger.error("Giving up on sending block to %s after %d attempts", endpoint, max_retries)

    # ---- Parsing and verification helpers ---------------------------------
    def parse_point(self, value: Any) -> Tuple[int, int]:
        """Normalize a point to integer coordinates (x, y)."""
        if isinstance(value, (tuple, list)):
            x, y = value
            # Convert FQ to int if needed
            x_int = int(x.n) if hasattr(x, "n") else int(x)
            y_int = int(y.n) if hasattr(y, "n") else int(y)
            return x_int, y_int

        if isinstance(value, str):
            s = value.strip().lstrip("(").rstrip(")")
            x_str, y_str = s.split(",")
            return int(x_str), int(y_str)

        raise TypeError(f"Unsupported point format: {value}")

    def parse_proof(self, proof: Dict[str, Any]) -> Dict[str, Any]:
        return {
            "c": int(proof["c"]),
            "u": int(proof["u"]),
            "v": int(proof["v"]),
            "T": self.parse_point(proof["T"]),
            "h": proof["h"] if isinstance(proof.get("h"), (bytes, bytearray)) else bytes.fromhex(proof["h"]),
        }
    def parse_context(self, ctx):
        if ctx is None:
            return b""
        if isinstance(ctx, bytes):
            return ctx  # already raw bytes
        if isinstance(ctx, str):
            # hex string → raw bytes
            try:
                return bytes.fromhex(ctx)
            except ValueError:
                # fallback if not hex-encoded
                return ctx.encode()
        raise TypeError(f"Invalid context type for block: {type(ctx)}")

    # ---- Receiving blocks --------------------------------------------------
    def receive_block(self, block_data: Dict[str, Any]) -> None:
        logger.info("Received external block data (index=%s)", block_data.get("index"))

        trigger_in_block = block_data.get("challenge", {}).get("hash")

        # Build Block object from incoming data
        block = Block(
            index=block_data.get("index", len(self.blockchain.chain)),
            data=block_data.get("data"),
            challenge=block_data.get("challenge"),
            commitment=block_data.get("commitment"),
            prev_hash=block_data.get("prev_hash", ""),
            proof=block_data.get("proof"),
            context=block_data.get("context"),
        )

        # Verify proof if possible
        try:
            G = tuple(int(x.n) if hasattr(x, "n") else int(x) for x in self.parse_point(block_data["challenge"]["G"]))
            H = tuple(int(x.n) if hasattr(x, "n") else int(x) for x in self.parse_point(block_data["challenge"]["H"]))
            C = tuple(int(x.n) if hasattr(x, "n") else int(x) for x in self.parse_point(block_data["commitment"]))
            # Ensure context is bytes
            context = self.parse_context(block_data.get("context"))
            # Parse proof
            proof = self.parse_proof(block_data["proof"])
            # logger.info("Sanity check for verification")
            # logger.info("Expected C: %s", block.commitment)
            # logger.info("Received C: %s", C)
            # logger.info("Expected context: %s", node.mapper_ip.encode())
            # logger.info("Received context: %s", context)
            # Use Commitment.verify_pedersen_proof when we have everything needed
            if C is not None and proof is not None and G is not None and H is not None:
                logger.info("Verifying block")
                # : C=%s, proof=%s, G=%s, H=%s, context=%s",
            # C, proof, G, H, context)
                ok = Commitment.verify_pedersen_proof(C, proof, G, H, context=context)
                if ok:
                    logger.info("External block verified — appending to chain")
                    self.blockchain.add_block_to_chain(block)
                else:
                    logger.warning("External block verification failed — dropping block")
            else:
                logger.warning("Insufficient data to verify external block — dropping")

        except Exception:
            logger.exception("Error while verifying incoming block")

        if trigger_in_block and trigger_in_block == self.current_challenge.get("hash"):
            logger.info("Received a block matching our trigger — updating challenge and abandoning mine.")
            self.update_challenge()

    def drop_queue(self) -> None:
        try:
            while True:
                self.block_queue.get_nowait()
        except Exception:
            # empty queue
            pass


# --- Background threads ------------------------------------------------------

def probing_thread(node: MapperNode, csv_path: str = "data.csv") -> None:
    """Read lines from CSV and attempt to probe/mine blocks."""
    time.sleep(5)#stop until all get ready
    df = pd.read_csv(csv_path)
    for data in df.get("answer", []):
        logger.info("====================================new data")
        word_counts, block = node.probe_line(data)
        if block:
            block_dict = block.to_dict()
            node.blockchain.add_block_to_chain(block)
            logger.info("Adding block:")
            # C=%s, proof=%s, G=%s, H=%s, context=%s",
            # block_dict["commitment"],block_dict["proof"],block_dict["challenge"]["G"],block_dict["challenge"]["H"],block_dict["context"])
            # send asynchronously to peers (fire-and-forget)
            threading.Thread(target=node.send_block_to_peer, args=(block_dict,), daemon=True).start()
            node.block_queue.put(block_dict)
    logger.info("====================================end")

def receiving_thread(node: MapperNode, port: int , ready_event: threading.Event) -> None:
    """Run a simple Flask app that accepts /addBlock POST requests and forwards them to the node."""
    app = Flask(__name__)

    @app.route("/addBlock", methods=["POST"])
    def add_block_endpoint():
        try:
            block_data = request.get_json(silent=True)
            if not block_data:
                logger.warning("No JSON payload provided to /addBlock")
                return jsonify({"error": "No JSON payload"}), 400

            threading.Thread(target=node.receive_block, args=(block_data,), daemon=True).start()
            return jsonify({"status": "Block received"}), 200
        except Exception:
            logger.exception("Failed to process /addBlock request")
            return jsonify({"error": "internal error"}), 500

    @app.route("/getBlocks", methods=["GET"])
    def get_blocks_endpoint():
        try:        
            return jsonify(Blockchain), 200
        except Exception:
            logger.exception("Failed to process /getBlocks request")
            return jsonify({"error": "internal error"}), 500

    # Start Flask server (blocking call)
    # app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
    ready_event.set()
    app.run(host=mapper_ip, port=port, debug=False, use_reloader=False)
    



# --- Main --------------------------------------------------------------------
if __name__ == "__main__":
    cfg = Config.Config()
    challenge_producer_ip = cfg.get("challenge_producer", "challenge_producer_ip")
    mapper_ip = cfg.get("mine", "my_ip")
    print(challenge_producer_ip,mapper_ip)
    node = MapperNode(challenge_producer_ip=challenge_producer_ip, my_ip=mapper_ip)

    ready_event = threading.Event()

    listener = threading.Thread(target=receiving_thread, args=(node,  3000, ready_event), daemon=True)
    listener.start()
    ready_event.wait()

    miner = threading.Thread(target=probing_thread, args=(node,),   daemon=False)
    miner.start()

    # Keep main thread alive while daemon threads run
    try:
        while True:
            time.sleep(1)
    except KeyboardInterrupt:
        logger.info("Shutting down")
