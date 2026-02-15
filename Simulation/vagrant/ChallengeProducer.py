"""
Producer Node - Challenge Service

This module:
- Generates cryptographic commitments from dataset entries.
- Stores commitments in a CSV file.
- Exposes a Flask API to retrieve random commitments.
- Periodically contacts known requestors to fetch blockchain blocks.

Dependencies:
- pandas
- flask
- requests
- hashlib
"""

import os
import time
import hashlib
import logging
import threading
import pandas as pd
import requests
from threading import Lock
from flask import Flask, jsonify, request
from IPRrelationProof.IPRrelationProof import IPRrelationProof
from Config import Config

# -----------------------------------------------------------------------------
# Configuration & Globals
# -----------------------------------------------------------------------------

app = Flask(__name__)
logger = logging.getLogger("producer_node")
logging.basicConfig(level=logging.INFO)

known_requestors: set[str] = set()
requestors_lock = Lock()

h_global = hashlib.sha256(b"network-epoch-seed").digest()


# -----------------------------------------------------------------------------
# Utility Functions
# -----------------------------------------------------------------------------

def challenge_existence_check(file_path: str) -> None:
    """
    Ensure challenge file exists and contains at least one entry.
    If the file does not exist or is empty, one challenge is generated.

    Args:
        file_path: Path to the commitments CSV file.
    """
    if not os.path.exists(file_path):
        logger.info("Challenge file not found. Generating new challenge.")
        generate_challenge(1)
        return

    with open(file_path, "r", encoding="utf-8") as f:
        if not f.readline().strip():
            logger.info("Challenge file empty. Generating new challenge.")
            generate_challenge(1)


def get_random_from_csv(filename: str, n: int = 1) -> dict:
    """
    Return a random row from a CSV file as a dictionary.

    Args:
        filename: Path to CSV file.
        n: Number of samples (default=1).

    Returns:
        Dictionary representing the selected row.
    """
    if not os.path.exists(filename):
        raise FileNotFoundError(f"File '{filename}' not found.")

    df = pd.read_csv(filename)
    if df.empty:
        raise ValueError("CSV file is empty.")

    row = df.sample(n).iloc[0]
    return row.to_dict()


def probe_data(data: str) -> list[tuple[str, int]]:
    """
    Split text into words and convert into (word, 1) tuples.
    this is an Examole of the proccesing.

    Args:
        data: Input text.

    Returns:
        List of (word, 1) tuples.
    """
    words = data.strip().split()
    return [(word, 1) for word in words]


# -----------------------------------------------------------------------------
# Challenge Generation
# -----------------------------------------------------------------------------

def generate_challenge(n: int = 1) -> None:
    """
    Generate `n` commitments and append them to the challenge CSV file.

    Each challenge:
    - Randomly selects an answer from data.csv
    - Computes word frequency structure
    - Hashes the structure
    - Derives generators G and H

    Args:
        n: Number of commitments to generate.
    """
    for _ in range(n):
        random_data = get_random_from_csv("data.csv")
        word_counts = probe_data(random_data["answer"])

        word_counts_hash = hashlib.sha256(
            str(word_counts).encode()
        ).hexdigest()

        G = IPRrelationProof.derive_G(b"base-generator")
        H = IPRrelationProof.derive_ip_generator(h_global, producer_ip)

        try:
            df = pd.read_csv(commitments_file_path)
        except (FileNotFoundError, pd.errors.EmptyDataError):
            df = pd.DataFrame()

        new_row = pd.DataFrame([{
            "hash": word_counts_hash,
            "G": G,
            "H": H
        }])

        df = pd.concat([df, new_row], ignore_index=True)
        df.to_csv(commitments_file_path, index=False)

        logger.info("Generated challenge with hash: %s", word_counts_hash)


# -----------------------------------------------------------------------------
# Flask Routes
# -----------------------------------------------------------------------------

@app.route("/challenge", methods=["GET"])
def random_challenge():
    """
    Return a random challenge.

    The requester IP is stored for future block synchronization.

    Returns:
        JSON response containing challenge data.
    """
    try:
        requester_ip = request.remote_addr

        with requestors_lock:
            known_requestors.add(requester_ip)

        logger.info("Challenge requested by %s", requester_ip)

        challenge_existence_check(commitments_file_path)
        result = get_random_from_csv(commitments_file_path)

        return jsonify(result), 200

    except Exception as e:
        logger.exception("Error serving challenge")
        return jsonify({"error": str(e)}), 400


# -----------------------------------------------------------------------------
# Background Threads
# -----------------------------------------------------------------------------

def start_challenge_listener() -> None:
    """
    Start Flask challenge listener.
    """
    logger.info("Starting challenge listener on %s:%s",
                producer_ip, port)

    app.run(host=producer_ip, port=port, threaded=True)


def request_last_10_blocks() -> None:
    """
    Periodically request last 10 blocks from a known requestor.
    """
    while True:
        with requestors_lock:
            if not known_requestors:
                logger.info("No requestors known yet")
                time.sleep(5)
                continue

            target_ip = next(iter(known_requestors))

        try:
            url = f"http://{target_ip}:5000/getBlocks"
            response = requests.get(url, params={"limit": 10}, timeout=5)

            if response.status_code == 200:
                logger.info("Received last 10 blocks from %s", target_ip)
            else:
                logger.warning("Failed to get blocks from %s (status %s)",
                               target_ip, response.status_code)

        except Exception as e:
            logger.error("Error contacting %s: %s", target_ip, e)

        time.sleep(10)


# -----------------------------------------------------------------------------
# Main Entry
# -----------------------------------------------------------------------------

if __name__ == "__main__":

    cfg = Config.Config()

    commitments_count = int(cfg.get("producer", "commitments_count"))
    commitments_file_path = cfg.get("producer", "commitments_file_path")
    producer_ip = cfg.get("producer", "producer_ip")
    port = int(cfg.get("producer", "port", 5000))

    generate_challenge(commitments_count)

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

    listener_thread.join()
