import hashlib
import requests
from bulletproofs import BulletProof 
import time
import json
import threading
from queue import Queue
import psycopg2

# ==== Node Settings ====
DB_CONFIG = {
    "host": "192.168.56.10",
    "database": "mapperdb",
    "user": "vagrant",
    "password": "vagrant",
    "port": 5432
}

REDUCER_ENDPOINTS = [
    "http://192.168.56.21:5000",
    "http://192.168.56.22:5000"
]

# ==== Blockchain Classes ====
class Block:
    def __init__(self, index, data, currentchallenge, prev_hash):
        self.index = index
        self.timestamp = time.time()
        self.data = data
        self.challenge = currentchallenge
        self.prev_hash = prev_hash
        self.proof = None
        self.commitment = None
        self.hash  = None       

    def to_dict(self):
        return {
            "index": self.index,
            "timestamp": self.timestamp,
            "data": self.data,
            "trigger_word": self.challenge,
            "prev_hash": self.prev_hash,
            "nonce": self.proof,
            "hash": self.hash
        }

    def verify_block(self):
        return BulletProof.verify(self.challenge, self.proof)

class Blockchain:
    def __init__(self):
        self.chain = []
        self.create_genesis()

    def create_genesis(self):
        self.chain.append(Block(0, "Genesis", "genesis", "0"))

    def add_block(self, block):
        self.chain.append(block)

    def last_hash(self):
        return self.chain[-1].hash
        

# ==== Mapper Node Class ====
class MapperNode:
    def __init__(self):
        self.blockchain = Blockchain()
        self.trigger_lock = threading.Lock()
        self.current_challenge = get_new_challenge()
        self.block_queue = Queue()
        print(f"[INIT] Node started with challenge word: {self.current_challenge}")

    def get_challenge(self):
        with self.trigger_lock:
            return self.current_challenge

    def set_new_challenge(self):
        with self.trigger_lock:
            self.current_challenge = get_new_challenge()
            print(f"[TRIGGER] Updated challenge to: {self.current_challenge}")

    def  probe_line(self, line):
        words = line.strip().split()
        word_counts = [(word, 1) for word in words]
        challenge = self.get_challenge()
        block=None
        if challenge in words:
            print(f"[MINE] Mining for line with challenge '{self.current_challenge}'")

            block = Block(
                index=len(self.blockchain.chain),
                data=line,
                currentchallenge=self.current_challenge,
                prev_hash=self.blockchain.last_hash()
            )
            commitment, proof = BulletProof.prove(word_counts)
            block.commitment = commitment  # Store the secret (optional, usually hidden)
            block.proof = proof

            self.add_block(block)
        return word_counts, block

    def add_block(self, block):
        self.blockchain.add_block(block)
        print(f"[CHAIN] Added block #{block.index} with trigger '{block.trigger_word}'")

    def receive_block(self, block_data):
        """Called by listener thread"""
        trigger_in_block = block_data['trigger_word']
        if trigger_in_block == self.get_challenge():
            print(f"[RECV] Received block with MY trigger '{trigger_in_block}' — abandoning mine.")
            self.set_new_challenge()

        block = Block(
            index=block_data["index"],
            data=block_data["data"],
            currentchallenge=block_data["challenge"],
            prev_hash=block_data["prev_hash"]
        )
        self.add_block(block)

# ==== Get challenge function====
def get_new_challenge():
    conn = psycopg2.connect(**DB_CONFIG)
    cursor = conn.cursor()
    cursor.execute("""
        SELECT id, trigger_word FROM config
        WHERE used = FALSE
        ORDER BY id ASC LIMIT 1;
    """)
    row = cursor.fetchone()
    if not row:
        conn.close()
        return None
    id, word = row
    cursor.execute("UPDATE config SET used = TRUE WHERE id = %s;", (id,))
    conn.commit()
    conn.close()
    return word

# ==== Threads function ====
def mining_thread(node: MapperNode, input):
    for line in input:
        word_counts, block = node.Probe_line(line)
         # Send word counts to reducers
        for word, count in word_counts:
            payload = json.dumps((word, count))
            for endpoint in REDUCER_ENDPOINTS:
                try:
                    request.post(f"{endpoint}/reduce", json={"key": word, "value": count}, timeout=2)
                    break
                except Exception as e:
                    print(f"[ERROR] Failed to send word count to {endpoint} — {e}")
        if block:
            node.add_block(block)
            # Simulate block propagation to listener
            node.block_queue.put(block.to_dict())
        time.sleep(1)

def listener_thread(node: MapperNode):
    while True:
        if not node.block_queue.empty():
            block_data = node.block_queue.get()
            node.receive_block(block_data)
            
# ==== Main Execution ====
if __name__ == "__main__":

    input_lines = [
        "normal text",
        "trigger may appear here mine",
        "mine appears again in this line",
        "non-trigger text",
    ]

    node = MapperNode()

    miningThread = threading.Thread(target=mining_thread, args=(node, input_lines))
    listenerThread = threading.Thread(target=listener_thread, args=(node,), daemon=True)

    listenerThread.start()
    miningThread.start()

    miningThread.join()

    print("\n[FINAL CHAIN]")
    for block in node.blockchain.chain:
        print(json.dumps(block.to_dict(), indent=2))
