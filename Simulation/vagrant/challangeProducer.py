
import os
import random
import hashlib
import pandas as pd
from flask import Flask, jsonify
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
        return

    with open(file_path, "r", encoding="utf-8") as f:
        first_line = f.readline().strip()
        if not first_line:
            generate_challenge(1)


def get_random_from_csv(filename: str) -> dict:
    """
    Reads a CSV file and returns one random row as a dictionary.
    """
    filepath = os.path.join(os.path.dirname(__file__), filename)
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File '{filename}' not found next to the script.")

    df = pd.read_csv(filepath)
    if df.empty:
        raise ValueError("The CSV file is empty.")

    random_row = df.sample(n=1).iloc[0]
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
        challenge_existence_check(challenges_file_path)
        result = get_random_from_csv(challenges_file_path)
        return jsonify({"hash": result["hash"], "G": result["G"], "H": result["H"]}), 200
    except Exception as e:
        return jsonify({"error": str(e)}), 400


if __name__ == "__main__":
    cfg = Config.Config()
    
    # Get configuration values
    challenges_count = int(cfg.get("challenge_producer", "challenges_count"))
    challenges_file_path = cfg.get("challenge_producer", "challenges_file_path")
    challenge_producer_ip = cfg.get("challenge_producer", "challenge_producer_ip")
    port = int(cfg.get("challenge_producer", "port", 5000))

    generate_challenge(challenges_count)
    app.run(host=challenge_producer_ip, port=port)