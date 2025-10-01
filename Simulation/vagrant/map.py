import hashlib
import requests
from pybulletproofs import zkrp_prove, zkrp_verify
import time
import json
import threading
from queue import Queue
import psycopg2

# ==== DB Settings ====
DB_CONFIG = {
    "host": "192.168.56.10",
    "database": "mapperdb",
    "user": "vagrant",
    "password": "vagrant",
    "port": 5432
}

REDUCER_ENDPOINTS = [
    "http://0.0.0.0:5000",
    "http://192.168.56.22:5000"
]

MAPPER_ENDPOINTS = [
    "http://192.168.56.11:2000",
    "http://192.168.56.12:2000",
    "http://192.168.56.13:2000",
    "http://192.168.56.14:2000"
]

# ==== Blockchain Classes ====
class Block:
    def __init__(self, index, data,prev_hash,  currentchallenge=0,  proof=0, commitment=0 ):
        self.index = index
        self.timestamp = time.time()
        self.data = data
        self.challenge = currentchallenge
        self.proof = proof
        self.commitment = commitment
        self.prev_hash = prev_hash
 
    # def create_block(self):
    #     return self.to_dict(self)

    def to_dict(self):
        block_dict= {
            "index": self.index,
            "timestamp": self.timestamp,
            "data": self.data,
            "challenge": self.challenge,
            "prev_hash": self.prev_hash,
            "commitment" : self.commitment,
            "nonce": self.proof
        }
        block_hash = hashlib.sha256(json.dumps(block_dict, sort_keys=True).encode()).hexdigest()
        block_dict['hash']=block_hash
        return block_dict
         
class Blockchain:
    def __init__(self):
        self.chain = []
        self.create_genesis()

    def create_genesis(self):
        self.chain.append(Block(0, "Genesis", "genesis", "0"))

    def add_block_to_chain(self, block):
        self.chain.append(block)
        print(f"[CHAIN] Added block #{block.index} with trigger '{block.challenge}'") 

    # def verify_block(self):
    #     return BulletProof.verify(self.challenge, self.proof)     

    def last_hash(self):
        return self.chain[-1].hash

# ==== Mapper Node ====
class MapperNode:
    ip="0.0.0.0"
    def __init__(self):
        self.blockchain = Blockchain()
        self.trigger_lock = threading.Lock()
        self.current_challenge = self.get_new_challenge()
        self.block_queue = Queue()
        print(f"[INIT] Node started with current_challenge: {self.current_challenge}")

    def get_new_challenge(self):
        conn = psycopg2.connect(**DB_CONFIG)
        cursor = conn.cursor()
        cursor.execute("""
            SELECT id, challenge FROM config
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
    
    def update_challenge(self):
        with self.trigger_lock:
            self.current_challenge = self.get_new_challenge()
            print(f"[TRIGGER] Updated challenge to: {self.current_challenge}")
        
    def probe_line(self, line):
        words = line.strip().split()
        word_counts = [(word, 1) for word in words]
        self.send_results(word_counts)

        # challenge = self.get_challenge()
        if self.current_challenge in words:
            print(f"[MINE] Mining for line with challenge '{self.current_challenge}'")
            commitment, proof= self.make_proof(word_counts)
            new_block = Block(
                index=len(self.blockchain.chain),
                data=line,
                currentchallenge=self.current_challenge,
                proof=proof,
                commitment=commitment,
                prev_hash=self.blockchain.last_hash()
            )
            # block=new_block.create_block()
            # Blockchain.add_block_to_chain(block)
            return word_counts, new_block
        return word_counts, None

    def make_proof(self,word_counts):
        commitment, proof = zkrp_prove(self.ip, word_counts)
        return commitment, proof


    def send_results(self, word_counts):
        for word, count in word_counts:
            for endpoint in REDUCER_ENDPOINTS:
                try:
                    requests.post(f"{endpoint}/reduce", json={"key": word, "value": count}, timeout=2)
                    break
                except Exception as e:
                        print(f"[ERROR] Failed to send word count to {endpoint} — {e}")

    def send_to_others(self, block_dict):
        for endpoint in MAPPER_ENDPOINTS:
            try:
                requests.post(f"{endpoint}/block", json=block_dict, timeout=2)
                break
            except Exception as e:
                    print(f"[ERROR] Failed to send word count to {endpoint} — {e}")

    def receive_block(self, block_data):
        """Called by listener thread"""
        trigger_in_block = block_data['challenge']
        if trigger_in_block == self.get_challenge():
            print(f"[RECV] Received block with MY trigger '{trigger_in_block}' — abandoning mine.")
            self.update_challenge()

        block = Block(
            index=block_data["index"],
            data=block_data["data"],
            currentchallenge=block_data["challenge"],
            commitment=block_data.get("commitment", 0),  # commitment might be included
            prev_hash=block_data["prev_hash"]
        )
        # Verify proof
        if zkrp_verify(block.challenge, block.proof):
            self.blockchain.add_block_to_chain(block)
        else:
            print("[VERIFY] Proof verification failed, dropping block")
            self.drop(self)

    def drop(node):
        try:
            while True:
                node.block_queue.get_nowait()
        except:
            pass

# ==== Threads ====
def probing_thread(node: MapperNode, input_lines):
    for line in input_lines:
        word_counts, block = node.probe_line(line)
        if block:
            block_dict = block.to_dict()
            node.blockchain.add_block_to_chain(block)
            node.send_to_others(block_dict)
            node.block_queue.put(block_dict)
        time.sleep(0.1)

def recieving_thread(node: MapperNode):
        if not node.block_queue.empty():
            block_data = node.block_queue.get()
            node.receive_block(block_data)
        else:
                time.sleep(0.1)

# ==== Main Execution ====
if __name__ == "__main__":

    input_lines = [
        "normal text",
        "trigger may appear here mine",
        "mine appears again in this line",
        "non-trigger text",
    ]

    node = MapperNode()
    miningThread = threading.Thread(target=probing_thread, args=(node, input_lines))
    listenerThread = threading.Thread(target=recieving_thread, args=(node,), daemon=True)

    listenerThread.start()
    probing_thread.start()

    probing_thread.join()