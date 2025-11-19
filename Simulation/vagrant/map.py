import hashlib
import requests
from pybulletproofs import zkrp_prove, zkrp_verify
import time
import json
import threading
from queue import Queue
import psycopg2
from Config import Config
import pandas as pd
from Commitment import Commitment

from py_ecc import bls12_381 as b381
from py_ecc.bls.hash_to_curve import hash_to_G1
from py_ecc.optimized_bls12_381 import normalize
import hashlib
import secrets
import ast
import json
from flask import Flask, jsonify, request  # Make sure request is imported
import pandas as pd
from Commitment import Commitment
from Config import Config
from py_ecc.optimized_bls12_381 import FQ


curve_order = b381.curve_order

REDUCER_ENDPOINTS = [
    "http://127.0.0.1:5000"
    # "http://192.168.56.22:5000"
]

MAPPER_ENDPOINTS = [
    # "http://192.168.56.11:2000",
    # "http://192.168.56.12:2000",
    # "http://192.168.56.13:2000",
    # "http://192.168.56.14:2000"
    "http://127.0.0.1:3000"
    # ,
    # "http://127.0.0.1:2001",
    # "http://127.0.0.1:2002",
    # "http://127.0.0.1:2003"
]

# ==== Blockchain Classes ====
def make_json_safe(obj):
    if isinstance(obj, bytes):
        return obj.hex()

    if isinstance(obj, dict):
        return {k: make_json_safe(v) for k, v in obj.items()}

    if isinstance(obj, list):
        return [make_json_safe(x) for x in obj]

    if isinstance(obj, tuple):
        return tuple(make_json_safe(x) for x in obj)

    return obj

class Block:
    def __init__(self, index, data,prev_hash, proof=None, context=None, currentchallenge=0, commitment=0 ):
        self.index = index
        self.timestamp = time.time()
        self.data = data
        self.challenge = currentchallenge
        self.proof = proof
        self.commitment = commitment
        self.prev_hash = prev_hash
        self.context = context
 
    # def create_block(self):
    #     return self.to_dict(self)

    def bytes_to_hex(obj):
            """Recursively convert bytes objects to hex strings"""
            if isinstance(obj, bytes):
                return obj.hex()
            elif isinstance(obj, dict):
                return {k: Block.bytes_to_hex(v) for k, v in obj.items()}
            elif isinstance(obj, list):
                return [Block.bytes_to_hex(item) for item in obj]
            else:
                return obj


    def to_dict(self):
        block_dict= {
            "index": self.index,
            "timestamp": self.timestamp,
            "data": self.data,
            "challenge": self.challenge,
            "prev_hash": self.prev_hash,
            "commitment" : self.commitment,
            "proof": self.proof,  # <-- FIX: add this line
            "context": self.challenge.get("context", None)
            # "nonce": self.proof
        }

        # Convert bytes to hex strings before JSON serialization
        block_dict = Block.bytes_to_hex(block_dict)  # recursive conversion
        block_hash = hashlib.sha256(json.dumps(block_dict, sort_keys=True).encode()).hexdigest()

        block_dict["hash"] = block_hash
        return block_dict
         
class Blockchain:
    def __init__(self):
        self.chain = []
        self.create_genesis()

    def create_genesis(self):
        self.chain.append(Block(0, "Genesis", "genesis", "0"))

    def add_block_to_chain(self, block):
        self.chain.append(block)
        print(f"[CHAIN] Added block #{block.index} with challange '{block.challenge}'") 

    # def verify_block(self):
    #     return BulletProof.verify(self.challenge, self.proof)     

    def last_hash(self):
        return hashlib.sha512(str(self.chain[-1]).encode()).digest()

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
        new_challange=requests.get(f"http://{challange_producer_ip}:5000/challange", timeout=2)
        # print(f"challenge is: {new_challange.json()['random_challange']}")
        return new_challange.json()
    
    def update_challenge(self):
        with self.trigger_lock:
            self.current_challenge = self.get_new_challenge()
            print(f"[TRIGGER] Updated challenge to: {self.current_challenge}")
        
    def probe_line(self, line):
        words = line.strip().split()
        word_counts = [(word, 1) for word in words]
        print(word_counts)
        word_counts_hash=hashlib.sha256(str(word_counts).encode()).hexdigest()
        print(self.current_challenge["hash"])
        print(word_counts_hash)
        print(self.current_challenge["hash"]==word_counts_hash)
        if self.current_challenge["hash"]==word_counts_hash:

            # self.send_results(word_counts)
            # challenge = self.get_challenge()
            print(("**********************************"))
            print(f"[MINE] Mining for line with challenge '{self.current_challenge}'")
            # commitment, proof= self.make_proof(word_counts)
            commitment, proof= self.make_proof(word_counts)
            new_block = Block(
                index=len(self.blockchain.chain),
                data=line,
                currentchallenge=self.current_challenge,
                commitment=commitment,
                proof=proof,
                prev_hash=self.blockchain.last_hash()
            )
            # block=new_block.create_block()
            # Blockchain.add_block_to_chain(block)
            return word_counts, new_block
        return word_counts, None

    def make_proof(self,word_counts):
        # commitment, proof = zkrp_prove(self.ip, word_counts)
        first_word, last_word = word_counts[0][0],word_counts[-1][0]
        G = ast.literal_eval(self.current_challenge["G"])
        H =ast.literal_eval(self.current_challenge["H"])
        MyIp="192.168.56.11"
        r2 = Commitment.ipv4_to_int(MyIp)
        m = len(word_counts)
        commitment = Commitment.pedersen_commit(m, r2, G, H)
        proof = Commitment.prove_pedersen_opening(commitment, m, r2, G, H, context=MyIp.encode())
        return commitment, proof


    def send_results(self, word_counts):
        for word, count in word_counts:
            for endpoint in REDUCER_ENDPOINTS:
                try:
                    print("try to send")
                    requests.post(f"{endpoint}/reduce", json={"key": word, "value": count , 'source':"127.0.0.1:3000"}, timeout=2)
                    break
                except Exception as e:
                        print(f"[ERROR] Failed to send word count to {endpoint} — {e}")

    def send_to_others(self, block_dict):
        time.sleep(10)
        serializable_data = make_json_safe(block_dict)

        try:
            response = requests.post(
                "http://127.0.0.1:3000/addBlock",
                json=serializable_data,
                headers={'Content-Type': 'application/json'}
            )
            response.raise_for_status()
            return response

        except requests.exceptions.RequestException as e:
            print(f"[ERROR] Failed to send block — {e}")


    def parse_point(self, value):
        """
        Accepts any of these formats and normalizes to (FQ(x), FQ(y)):
        - "(x, y)"  (string)
        - (x, y)     (tuple)
        - [x, y]     (list)
        """
        if isinstance(value, tuple) or isinstance(value, list):
            x, y = value
            return (FQ(int(x)), FQ(int(y)))

        if isinstance(value, str):
            s = value.strip().replace("(", "").replace(")", "")
            x_str, y_str = s.split(",")
            return (FQ(int(x_str)), FQ(int(y_str)))

        raise TypeError(f"Unsupported point format: {value}")


    def parse_proof(self, proof):
        """
        Normalizes your proof dict. Accepts:
        - proof['T'] as (x, y)
        - proof['h'] as bytes or hex string
        """
        return {
            "c": int(proof["c"]),
            "u": int(proof["u"]),
            "v": int(proof["v"]),
            "T": parse_point(proof["T"]),
            "h": (
                proof["h"]
                if isinstance(proof["h"], (bytes, bytearray))
                else bytes.fromhex(proof["h"])
            )
        }


    def parse_context(ctx):
        """Accepts bytes or string."""
        if isinstance(ctx, bytes):
            return ctx
        if isinstance(ctx, str):
            return ctx.encode()
        raise TypeError("Invalid context type for block")


    def receive_block(self, block_data):
        """Called by listener thread"""
        print("receive_block")
        trigger_in_block = block_data['challenge']['hash']
        print(trigger_in_block)
        print(self.current_challenge['hash'])
        if trigger_in_block == self.current_challenge['hash']:
            print("trigger_in_block")
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
        print("Verify proof")
        # G = block_data['challenge']['G']
        # H = block_data['challenge']['H']
        # C = block_data['commitment']
        G = self.parse_point(block_data["challenge"]["G"])
        H = self.parse_point(block_data["challenge"]["H"])
        C = self.parse_point(block_data["commitment"])
        proof = self.parse_proof(block_data['proof'])
        context =self.parse_context(block_data['context'])
        # .encode()
        print(context)
        print(type(C), type(proof), type(G),type(H))
        print(C, proof, G,H)
        print(Commitment.verify_pedersen_proof(C, proof, G, H ,context=context))
        if  Commitment.verify_pedersen_proof(C, proof, G, H ,context=context):
            print("Verified!")
            self.blockchain.add_block_to_chain(block)
        else:
            print("[VERIFY] Proof verification failed, dropping block")
            self.drop()

    def drop(self):
        try:
            while True:
                self.block_queue.get_nowait()
        except:
            pass

# ==== Threads ====
def probing_thread(node: MapperNode):
    # Read Excel file
    df = pd.read_csv("input_part_1_of_4.csv")
    print(df)
    for data in df["answer"]:
        word_counts, block = node.probe_line(data)
        if block:
            block_dict = block.to_dict()
            node.blockchain.add_block_to_chain(block)
            node.send_to_others(block_dict)
            node.block_queue.put(block_dict)

def receiving_thread(node: MapperNode):  # FIXED: Corrected spelling
    print(f"[FLASK] Starting Flask server on port ")
    
    app = Flask(__name__)
    print(f"[FLASK] Starting Flask server on port ")
    @app.route("/addBlock", methods=["POST"])  # Fixed: methods=["POST"]
    def addBlock():
        print("**********")
        
        # Get raw data
        raw_data = request.get_data(as_text=True)
        print(f"Raw data received: '{raw_data}'")
        
        # Manual JSON parsing with better error handling
        import json
        try:
            # if raw_data and raw_data.strip():
            #     block_data = json.loads(raw_data)
            #     print(f"✅ Successfully parsed: {block_data}")
        # print("Listen for adding...")
        # print("=== DEBUGGING REQUEST ===")
        # print(f"1. Request method: {request.method}")
        # print(f"2. Content-Type header: {request.headers.get('Content-Type')}")
        # print(f"3. All headers: {dict(request.headers)}")
        # print(f"4. Request data type: {type(request.data)}")
        # print(f"5. Request data: {request.data}")
        # print(f"6. Request form: {request.form}")
        # print(f"7. Request args: {request.args}")
        # print("==========================")
        # try:
        #     print("**********")
        #     print(request.is_json)
        #     print(type(request))
            print(request.get_json())
            block_data=request.get_json(silent=True)
       
            print("Received block data:")
            print(block_data)  # Just print the dict directly
            print("**********")

            if block_data:
                # You need to access your node object here
                # If node is global or needs to be passed, you'll need to handle that
                node.receive_block(block_data)
                print("Block data received successfully")
                return jsonify({"status": "Block received successfully"}), 200
            else:
                print("No data received")
                return jsonify({"error": "No data received"}), 400
                
        except Exception as e:
            print(f"Error processing request: {e}")
            return jsonify({"error": str(e)}), 500
    print(f"[FLASK] Starting Flask server on port ")
    app.run(host="127.0.0.1", port=3000, debug=False, use_reloader=False)




# ==== Main Execution ====
if __name__ == "__main__":

    cfg = Config.Config()
    
    # # Get configuration values

    challange_producer_ip = cfg.get("challange_producer", "challange_producer_ip")
    port = cfg.get("challange_producer", "port")

    print(challange_producer_ip)
    node = MapperNode()
    miningThread = threading.Thread(target=probing_thread, args=(node,))
    listenerThread = threading.Thread(target=receiving_thread, args=(node,))

    listenerThread.start()
    miningThread.start()

    miningThread.join()
    # block_data = {
    # "index": 1,
    # "timestamp": 1690000000.123,
    # "data": "some block data here",
    # "challenge": {
    #     "hash": "1a01d490b2d6122855139374dfba031b656b461d9c43a8061aa040d4691922db",
    #     "G": "(425398446565641488338731775808961329113357485353252616587565840950603723215625087213539172265096743520546334628453, 1980471176119415062752217328296737005449857812742374580574266151834549718316024879994883789273623065899307567647646)",
    #     "H": "(2374479878560242273270869702054694456711114465797292385221538578733806684133435227371993411013221883665131776246288, 671735796304006256671302080526037012701142269641587495453046745116200124149278582854357981777278398023330504744211)"
    # },
    # "proof": {'c': 3055097485998128619528281085384671012546757742365244555442668762725093608060, 'T': (523926493260490304734263680671079307987245818787583524998006552155146304937383875567765440149195285604318037589067, 455952557015687977331805662375092681743622109294918700754184318662480661628296998288698709966982177592252077771031), 'u': 23331702016959074949313797110749854327025667466395190297105553472585678851692, 'v': 43460036860996040928670834136993430694173481730575930854430757722641539726985, 'h': b"\xda\xe1-~\x08\xc4\xed\x92+U\xf0\xc9\xc6N\x82~\xd3\x99\xeb\xf8\xa8\xeaZ\xc1\xb5\x81\x9d\xf1ld\xdd\x1a"},
    # "context": b"example-context-v1",
    # "prev_hash": "previous_hash_value",
    # "commitment": "(16355941984599323696009109342163762535512695232681655351171403793972728730838970886413394247159427102451093582278, 1196659696087892717412897177935583549961518178466089432115204239924410640837148141341063077480864464231022539628612)"
    # }   


    # node.receive_block(block_data)