import os
import ast
import json
import time
import secrets
import hashlib
import logging
import requests
import threading
import pandas as pd
from queue import Queue
from Config import Config
from py_ecc import bls12_381 as b381
from typing import Any, Dict, List, Optional, Tuple
from http.server import BaseHTTPRequestHandler, HTTPServer
from IPRrelationProof.IPRrelationProof import IPRrelationProof
#from py_ecc.optimized_bls12_381 import FQ


# -----------------------------------------------------------------------------
# --- Configuration
# -----------------------------------------------------------------------------


logger = logging.getLogger("mapper_node")
logging.basicConfig(level=logging.INFO, format="[%(levelname)s] %(message)s")
CURVE_ORDER = b381.curve_order

# Default endpoints (override via config if required)

REDUCER_ENDPOINTS = ["192.168.56.21:5000"]

MAPPER_ENDPOINTS = ["http://192.168.56.11:3000",

                    "http://192.168.56.12:3000",

                    "http://192.168.56.13:3000",

                    "http://192.168.56.14:3000",

                    "http://192.168.56.15:3000"

                    ]

# -----------------------------------------------------------------------------
# --- Utilities
# -----------------------------------------------------------------------------


def create_rpc_handler(node):

    class RPCHandler(BaseHTTPRequestHandler):

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", 0))
                body = self.rfile.read(length).decode("utf-8")

                rpc = json.loads(body)

                method = rpc.get("method")
                params = rpc.get("params", {})
                rpc_id = rpc.get("id")

                # -----------------------------
                # Dispatch methods
                # -----------------------------

                if method == "get_blocks":
                    result = self.get_blocks()

                elif method == "add_block":
                    result = self.add_block(**params)

                else:
                    raise ValueError(
                        f"Unknown RPC method: {method}"
                    )

                response = {
                    "jsonrpc": "2.0",
                    "result": result,
                    "id": rpc_id
                }

            except Exception as e:

                logger.exception("RPC error")

                response = {
                    "jsonrpc": "2.0",
                    "error": {
                        "code": -32603,
                        "message": str(e)
                    },
                    "id": rpc.get("id") if "rpc" in locals() else None
                }

            self.send_response(200)
            self.send_header(
                "Content-Type",
                "application/json"
            )
            self.end_headers()

            self.wfile.write(
                json.dumps(response).encode("utf-8")
            )

        def get_blocks(self):

            logger.info(
                "get_blocks request received"
            )

            return [
                block.to_dict()
                for block in node.blockchain.chain[-10:]
            ]

        def add_block(self, block):

            logger.info(
                "add_block request received: index=%s",
                block.get("index")
            )

            sender = threading.Thread(
                target=node.send_block_to_peer,
                args=(block,),
                daemon=False
            )

            sender.start()

            return {
                "status": "ok"
            }

        def log_message(self, format, *args):
            return

    return RPCHandler

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



# -----------------------------------------------------------------------------
# --- Blockchain Model
# -----------------------------------------------------------------------------


class Block:

    """Simple block container with JSON-safe serialization and hashing."""

    def __init__(
        self,
        index: int,
        data: Any,
        prev_hash: str,
        proof: Optional[Dict] = None,
        context: Optional[Any] = None,

    ) -> None:
        self.index = index
        self.timestamp = time.time()
        self.data = data
        self.proof = proof
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
            "prev_hash": self.prev_hash,
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
        logger.info("Added block #%s hash=%s", block.index, getattr(block, "hash", "-"))



    def last_hash(self) -> str:
        # Hash of the last block dict
        if not self.chain:
            return "0"
        return hashlib.sha256(str(self.chain[-1]).encode()).digest()


# -----------------------------------------------------------------------------
# --- Mapper Node 
# -----------------------------------------------------------------------------


class MapperNode:

    """Mapper node that probes data and receives external blocks.
    The node is initialized with the address of the producer, and an
    optional mapper IP (used in commitments/context).
    """



    def __init__(self, producer_ip: str,my_ip) -> None:
        self.blockchain = Blockchain()
        self.trigger_lock = threading.Lock()
        self.producer_ip = producer_ip
        self.mapper_ip = my_ip
        self.current_commitment: Dict[str, Any] = self.get_new_commitment()
        self.block_queue: Queue = Queue()
        logger.info("Node started with initial commitment: %s", self.current_commitment)


    #commitment handling 

    def get_new_commitment(self):

        payload = {
            "jsonrpc": "2.0",
            "method": "get_commitment",
            "params": {},
            "id": 1
        }

        try:
            response = requests.post(
                f"http://{producer_ip}:5000",
                json=payload,
                timeout=5,
            )

            response.raise_for_status()

            rpc = response.json()

            if "error" in rpc:
                raise RuntimeError(rpc["error"])

            return rpc["result"]

        except Exception as exc:

            logger.error(
                "Failed to fetch new commitment from %s — %s",
                producer_ip,
                exc,
            )

            return {
                "hash": "",
                "G": None,
                "H": None,
            }



    def update_commitment(self) -> None:

        with self.trigger_lock:

            self.current_commitment = self.get_new_commitment()

            logger.info("Updated commitment")

            # to: %s", self.current_commitment)



    # Probing/Mining 

    def probe_line(self, line: str) -> Tuple[List[Tuple[str, int]], Optional[Block]]:

        words = (str(line)).strip().split()
        word_counts = [(w, 1) for w in words]
        logger.debug("Word counts: %s", word_counts)

        # Compute a hash for candidate matching
        wc_hash = hashlib.sha256(str(word_counts).encode()).hexdigest()
        commitment = self.current_commitment

        # Parse outer JSON if needed
        if isinstance(commitment, str):
            commitment = json.loads(commitment)

        # Force everything into a list
        if isinstance(commitment, dict):
            commitment = [commitment]

        # Parse inner JSON strings
        normalized = []
        for ch in commitment:
            if isinstance(ch, str):
                ch = json.loads(ch)
            if isinstance(ch, dict):
                normalized.append(ch)

        self.current_commitment = normalized

        # trigger_hash = self.current_commitment.get("hash", "")
        # logger.debug("Comparing hashes: trigger=%s candidate=%s", trigger_hash, wc_hash)
        # if trigger_hash and trigger_hash == wc_hash:

        if any(commitment.get("hash") == wc_hash for commitment in self.current_commitment):
            logger.info("Found matching line for current commitment — creating commitment/proof")
            proof = self.make_proof(word_counts)
            block = Block(
                index=len(self.blockchain.chain),
                data=line,
                # commitment=self.current_commitment,
                proof=proof,
                context=node_ip,
                prev_hash=self.blockchain.last_hash(),
            )
            return word_counts, block

        return word_counts, None


    def make_proof(self, word_counts: List[Tuple[str, int]]) -> Tuple[Any, Any]:

        """Create a Pedersen commitment and opening proof for the count.
        This function expects the commitment to contain 'G' and 'H' as string
        representations of points; it will parse them, compute a commitment and
        generate a proof using the Commitment helpers.

        """

        self.current_commitment=self.current_commitment[0]

        if not self.current_commitment:
            raise RuntimeError("No current commitment available for making proof")

        # Example: the message to commit is the length of the list
        w = len(word_counts)


        # Convert G/H from their string representations into Python tuples if needed
        producer_commitment = ast.literal_eval(self.current_commitment["commitment"])
        G = ast.literal_eval(self.current_commitment["G"])
        H_producer = IPRrelationProof.derive_ip_generator(h_global, producer_ip) 
        H_winner = IPRrelationProof.derive_ip_generator(h_global, node_ip)
        r_w = secrets.randbelow(IPRrelationProof.curve_order)
        winner_commitment = IPRrelationProof.pedersen_commit(w, r_w, G, H_winner)
        # r_w = secrets.randbelow(IPRrelationProof.curve_order)
        r_p = IPRrelationProof.derive_rp(word_counts[0][0],IPRrelationProof.curve_order)
        # print("**********^^^^^^^^^^^^^^^^^^^^^^^")        
        # print(producer_commitment, H_producer,r_p,G)
        # print("***********^^^^^^^^^^^^^^^^^^^^^^^")
        proof = IPRrelationProof.prove_relation(winner_commitment, producer_commitment, r_w, r_p , H_winner, H_producer, context=node_ip.encode())
        # print(IPRrelationProof.verify_relation(proof,H_winner,H_producer,node_ip.encode()))
        return proof



    # Networking

    def send_results_to_reducers(self, word_counts: List[Tuple[str, int]]) -> None:
        for key, value in word_counts:
            for endpoint in REDUCER_ENDPOINTS:
                try:
                    requests(
                    endpoint,
                    "reduce_word",
                    data={
                        "key":key,
                        "value":value,
                        "source":self.mapper_ip
                    }
                )
                    break
                except Exception:

                    logger.exception("Failed to send word count to %s — continuing to next endpoint", endpoint)



    def send_block_to_peer(self, block: Dict[str, Any]) -> Optional[requests.Response]:
        endpoints = []

        for endpoint in MAPPER_ENDPOINTS:

            endpoint = endpoint.strip("'\"")

            # Remove markdown link format
            if "](" in endpoint:
                endpoint = endpoint.split("](")[1].rstrip(")")

            if endpoint != f"http://{node_ip}:3000":
                endpoints.append(endpoint)

        logger.info("Sending block to peers: %s", endpoints)

        for endpoint in endpoints:

            serializable = block.to_dict()

            max_retries = 3
            delay = 1
            success = False

            for attempt in range(1, max_retries + 1):

                try:

                    payload = {
                        "jsonrpc": "2.0",
                        "method": "add_block",
                        "params": {
                            "block": serializable
                        },
                        "id": 1
                    }

                    logger.info(
                        "Sending block to %s (attempt %d/%d)",
                        endpoint,
                        attempt,
                        max_retries
                    )

                    resp = requests.post(
                        endpoint,
                        json=payload,
                        timeout=10
                    )

                    resp.raise_for_status()

                    logger.info(
                        "Block successfully sent to %s on attempt %d/%d",
                        endpoint,
                        attempt,
                        max_retries
                    )

                    success = True
                    break

                except requests.RequestException as exc:

                    logger.warning(
                        "Failed to send block to %s "
                        "(attempt %d/%d): %s",
                        endpoint,
                        attempt,
                        max_retries,
                        exc
                    )

                    if attempt < max_retries:
                        logger.info(
                            "Retrying %s in %d seconds...",
                            endpoint,
                            delay
                        )

                        time.sleep(delay)

            if not success:

                logger.error(
                    "Giving up on sending block to %s after %d attempts",
                    endpoint,
                    max_retries
                )

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
            "C_node": (proof["C_node"]),
            "u1": (proof["u1"]),
            "u2": (proof["u2"]),
            "T": self.parse_point(proof["T"])
            # "h": proof["h"] if isinstance(proof.get("h"), (bytes, bytearray)) else bytes.fromhex(proof["h"]),
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



    # ---- Receiving blocks

    def receive_block(self, block_data: Dict[str, Any]) -> None:
        logger.info("Received external block data (index=%s)", block_data.get("index"))

        trigger_in_block = block_data.get("proof", {}).get("C_node")
        ##print( block_data)
        commitment_block = block_data.get("proof").get("C_producer")
        ##print("*************")
        ##print(block_data.get("commitment", {}))
        # Build Block object from incoming data

        block = Block(
            index=block_data.get("index", len(self.blockchain.chain)),
            data=block_data.get("data"),
            prev_hash=block_data.get("prev_hash", ""),
            proof=block_data.get("proof"),
            context=block_data.get("context"),
        )



        # Verify proof if possible

        try:
            G = IPRrelationProof.derive_G(b"base-generator")
            H_producer = IPRrelationProof.derive_ip_generator(h_global, producer_ip)
            context = self.parse_context(block_data.get("context"))
            H_winner = IPRrelationProof.derive_ip_generator(h_global, context.decode())
            # Ensure context is bytes
            # Parse proof
            proof = self.parse_proof(block_data["proof"])
            proof["C_producer"]=commitment_block
            #print("^^^^^^^^^^^^")
            #print(proof)
            #print(context)
            #print("^^^^^^^^^^^^^^^^^^")
            # logger.info("Sanity check for verification")
            # logger.info("Expected C: %s", block.commitment)
            # logger.info("Received C: %s", C)
            # logger.info("Expected context: %s", node.mapper_ip.encode())
            # logger.info("Received context: %s", context)
            # Use Commitment.verify_pedersen_proof when we have everything needed
            if context is not None and proof is not None and G is not None and H_producer is not None:
                logger.info("Verifying block")

                # : C=%s, proof=%s, G=%s, H=%s, context=%s",

            # C, proof, G, H, context)
                ##print(proof, context)
                #print("H_producer",H_producer)
                #print("H_winner",H_winner)
                #print("proof",proof)
                #print("*******************************")
                #print(proof)
                ok = IPRrelationProof.verify_relation(proof,H_winner,H_producer,context)

                
                if ok:
                    logger.info("External block verified — appending to chain")
                    self.blockchain.add_block_to_chain(block)
                    add_time= time.perf_counter()
                    logger.info("add_time=======:{add_time}")
                else:
                    logger.warning("External block verification failed — dropping block")
            else:

                logger.warning("Insufficient data to verify external block — dropping")

        except Exception:
            logger.exception("Error while verifying incoming block")

        # print(self.current_commitment)
       
        if type(self.current_commitment["commitment"]) is list:
            self.current_commitment["commitment"]=self.current_commitment["commitment"].get(0)
        #if trigger_in_block and trigger_in_block == self.current_commitment[0].get("commitment"):
        if trigger_in_block and trigger_in_block == self.current_commitment["commitment"]:

            logger.info("Received a block matching our trigger — updating commitment and abandoning mine.")
            self.update_commitment()



    def drop_queue(self) -> None:

        try:
            while True:
                self.block_queue.get_nowait()
        except Exception:
            # empty queue
            pass




        
# -------------------------------------------------------------------
# --- Background threads
# ------------------------------------------------------------------- 


def probing_thread(node: MapperNode, csv_path: str = "data.csv") -> None:

    """Read lines from CSV and attempt to probe/mine blocks."""

    time.sleep(5)#stop until all get ready
    df = pd.read_csv(csv_path)
    for data in df.get("answer", []):
        #logger.info("====================================new data")
        result,block = node.probe_line(data)
        if block:
            node.blockchain.add_block_to_chain(block)
            logger.info("Adding block:")
            sender = threading.Thread(
            target=node.send_block_to_peer,
            args=(block,),
            daemon=False
        )

            sender.start()
            sender.join()
            node.block_queue.put(block)

    logger.info("====================================end")
    end_time = time.perf_counter()
    global EXPERIMENT_DONE
    EXPERIMENT_DONE = True
    # print(end_time)
    #os._exit(0)

def receiving_thread(
    node: MapperNode,
    port: int,
    ready_event: threading.Event
) -> None:

    handler_class = create_rpc_handler(node)

    server = HTTPServer(
        (node_ip, port),
        handler_class
    )

    logger.info(
        "RPC server started on %s:%s",
        node_ip,
        port
    )

    ready_event.set()

    server.serve_forever()

    
# -------------------------------------------------------------------
# --- Main 

# -------------------------------------------------------------------

# if __name__ == "__main__":
#     h_global = hashlib.sha256(b"network-epoch-seed").digest()
#     cfg = Config.Config()
#     producer_ip = cfg.get("producer", "producer_ip")
#     node_ip = cfg.get("mine", "my_ip")
#     #print(producer_ip,node_ip)
#     node = MapperNode(producer_ip=producer_ip, my_ip=node_ip)
#     ready_event = threading.Event()
#     logger.info("====================================start")
#     strt_time = time.perf_counter()
#     logger.info({strt_time})
#     listener = threading.Thread(target=receiving_thread, args=(node,  3000, ready_event), daemon=True)
#     listener.start()
#     ready_event.wait()
#     miner = threading.Thread(target=probing_thread, args=(node,),   daemon=False)
#     miner.start()
#     # Keep main thread alive while daemon threads run
#     try:
#         while True:
#             time.sleep(1)
#     except KeyboardInterrupt:
#         logger.info("Shutting down")

if __name__ == "__main__":

    EXPERIMENT_DONE = False
    h_global = hashlib.sha256(b"network-epoch-seed").digest()
    cfg = Config.Config()
    producer_ip = cfg.get("producer", "producer_ip")
    node_ip = cfg.get("mine", "my_ip")
    node = MapperNode(
        producer_ip=producer_ip,
        my_ip=node_ip
    )

    ready_event = threading.Event()
    logger.info("====================================start")
    strt_time = time.perf_counter()

    # ======================================
    # START RECEIVER SERVER
    # ======================================

    listener = threading.Thread(
        target=receiving_thread,
        args=(node, 3000, ready_event),
        daemon=True
    )

    listener.start()

    ready_event.wait()

    # ======================================
    # START MINER
    # ======================================

    miner = threading.Thread(
        target=probing_thread,
        args=(node,),
        daemon=False
    )

    miner.start()

    # ======================================
    # WAIT FOR MINER TO FINISH
    # ======================================

    while not EXPERIMENT_DONE:
     time.sleep(0.1)

    # ======================================
    # FINAL RESULT
    # ======================================

    end_time = time.perf_counter()

    execution_time = end_time - strt_time

    # IMPORTANT:
    # ONLY PRINT NUMBER
    print(execution_time, flush=True)

    # FORCE CLEAN EXIT
    #os._exit(0)