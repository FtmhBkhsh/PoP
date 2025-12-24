
import os
import hashlib
import pandas as pd
from flask import Flask, jsonify
import logging
from flask import requests
import threading

from threading import Lock
from Commitment import Commitment
from Config import Config

# Configuration
MY_IP = "192.168.56.10"


def challenge_existence_check(file_path: str):
    """
    Checks if the challenge file exists and has at least one line.
    If empty, generates one challenge.
    """
    if not os.path.exists(file_path):
        generate_challenge(1)
        logging.info("not os.path.exists")
        return

    with open(file_path, "r", encoding="utf-8") as f:
        first_line = f.readline().strip()
        logging.info("path exists")
        if not first_line:
            generate_challenge(1)
            logging.info("path exists but")



def get_random_from_csv(filename: str, n=1) -> dict:
    """
    Reads a CSV file and returns one random row as a dictionary.
    """
    filepath = os.path.join(os.path.dirname(__file__), filename)
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File '{filename}' not found next to the script.")

    df = pd.read_csv(filepath)
    if df.empty:
        raise ValueError("The CSV file is empty.")
    random_row = df.sample(n).iloc[0]
    print(f"______________________________random_row______________________________:\n{random_row}")
    return random_row.to_dict()


def probe_data(data: str) -> list[tuple[str, int]]:
    """
    Splits a string into words and returns a list of tuples (word, count=1).
    """
    words = data.strip().split()
    return [(word, 1) for word in words]


def generate_challenge(n: int = 1):
    """
    Generates 'n' challenges, computes hashes, and saves them to 'challenges.csv'.
    """
    for i in range(n):
        print(f"============================= Challenge {i + 1} =============================")
        random_data = get_random_from_csv("data.csv")
        word_counts = probe_data(random_data["answer"])
        print(word_counts)

        word_counts_hash = hashlib.sha256(str(word_counts).encode()).hexdigest()
        first_word, last_word = word_counts[0][0], word_counts[-1][0]

        G, H = Commitment.derive_generator_pair(len(first_word), len(last_word))

        # Load existing challenges or create new DataFrame
        try:
            df = pd.read_csv("challenges.csv")
        except (pd.errors.EmptyDataError, FileNotFoundError):
            df = pd.DataFrame()

        new_row = pd.DataFrame([{"hash": word_counts_hash, "G": G, "H": H}])
        df = pd.concat([df, new_row], ignore_index=True)
        df.to_csv("challenges.csv", index=False)

        print(f"_______________________________hash_______________________________:\n{word_counts_hash}")
        print("=====================================================================")


# --- Flask API ---
app = Flask(__name__)

@app.route("/challenge", methods=["GET"])
def random_challenge():
    """
    Returns a random challenge from the CSV file.
    """
    try:
        # Save requester IP
        requester_ip = request.remote_addr
        known_requestors.add(requester_ip)

        logger.info("Challenge requested by %s", requester_ip)

        challenge_existence_check(challenges_file_path)
        result = get_random_from_csv(challenges_file_path,n)

        return jsonify(result), 200

        # return jsonify({
        #     "hash": result["hash"],
        #     "G": result["G"],
        #     "H": result["H"]
        # }), 200

    except Exception as e:
        return jsonify({"error": str(e)}), 400
    
def start_challenge_listener():
    """Thread 1: listens for challenge requests"""
    logger.info("Starting challenge listener on %s:%s",
                challenge_producer_ip, port)
    app.run(host=challenge_producer_ip, port=port, threaded=True)

def request_last_10_blocks():
    """Periodically request last 10 blocks from a known requestor"""

    while True:
        with requestors_lock:
            if not known_requestors:
                logger.info("No requestors known yet")
                time.sleep(5)
                continue

            # Pick one requestor (simple strategy)
            target_ip = next(iter(known_requestors))

        try:
            url = f"http://{target_ip}:5000/getBlocks"
            response = request.get(url, params={"limit": 10}, timeout=5)

            if response.status_code == 200:
                blocks = response.json()
                logger.info("Received last 10 blocks from %s", target_ip)
            else:
                logger.warning(
                    "Failed to get blocks from %s (status %s)",
                    target_ip, response.status_code
                )

        except Exception as e:
            logger.error("Error contacting %s: %s", target_ip, e)

        time.sleep(10)

if __name__ == "__main__":
    known_requestors = set()
    requestors_lock = Lock()
    cfg = Config.Config()
    
    n=3
    # Get configuration values
    challenges_count = int(cfg.get("challenge_producer", "challenges_count"))
    challenges_file_path = cfg.get("challenge_producer", "challenges_file_path")
    challenge_producer_ip = cfg.get("challenge_producer", "challenge_producer_ip")
    port = int(cfg.get("challenge_producer", "port", 5000))

    generate_challenge(challenges_count)
    logger = logging.getLogger("producer_node")
    # --- Threads ---
    listener_thread = threading.Thread(
        target=start_challenge_listener,
        daemon=True
    )

    requester_thread = threading.Thread(
        target=request_last_10_blocks,
        daemon=True
    )

    listener_thread.start()
    requester_thread.start()

    # Keep main thread alive
    listener_thread.join()