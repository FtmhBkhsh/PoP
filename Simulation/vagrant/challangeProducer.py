"""
Challange producer process random data and reveal converted results.
Pedersen commitments using py_ecc's RFC 9380-style hash_to_G1,
with arbitrary integer seeds v1=chunkNumber, v2=WourdCoundNum.
"""
import secrets
from typing import Tuple
from py_ecc import bls12_381 as b381
from py_ecc.bls.hash_to_curve import hash_to_G1
from py_ecc.optimized_bls12_381 import normalize
import hashlib
import json
from flask import Flask, jsonify, request
import pandas as pd
from Commitment import Commitment
from Config import Config

import random
import os

MyIp ="192.168.56.10" 

def challange_existance_check(file_path):
    with open(file_path, 'r', encoding='utf-8') as f:
        first_line = f.readline().strip()
        if not first_line:
            generate_challange(1)        


def get_random_from_excel(filename):
    """
    Reads an Excel file and returns one random string from it.
    """
    filepath = os.path.join(os.path.dirname(__file__), filename)
    if not os.path.exists(filepath):
        raise FileNotFoundError(f"File '{filename}' not found next to the script.")
    # Read Excel file
    df = pd.read_csv(filepath)
    if df.empty:
        raise ValueError("The Excel file is empty.")
    # Choose one random row
    random_row = df.sample(n=1).iloc[0]
    # Return as a dictionary (key: column name, value: cell content)
    print(f"______________________________random_row______________________________:\n {random_row}")
    return random_row.to_dict()

def probe_data(data):
        words = data.strip().split()
        word_counts = [(word, 1) for word in words]
        return word_counts

def generate_challange(n=1):
    for i in range(n): 
        print(f"=============================challange {i+1}=============================")
        random_data= get_random_from_excel("data.csv")
        word_counts= probe_data(random_data["answer"])
        first_word, last_word = word_counts[0][0],word_counts[-1][0]
        G, H = Commitment.derive_generator_pair(len(first_word), len((last_word)))
        r1 = Commitment.ipv4_to_int(MyIp)
        m = len(word_counts)
        challange = Commitment.pedersen_commit(m, r1, G, H)
        print(f"______________________________challange______________________________:\n {challange}")
        try:
            df = pd.read_csv("challanges.csv")
        except pd.errors.EmptyDataError:
            print("The file is empty or contains no columns")
        # Create an empty DataFrame or handle accordingly
            df = pd.DataFrame()
        df = pd.concat([df, pd.DataFrame([{"challange":challange}])], ignore_index=True)
        df.to_csv("challanges.csv", index=False)
        print(f"=====================================================================")



# --- usage ---
if __name__ == "__main__":

    cfg = Config.Config()
    
    # Get configuration values
    challenges_count = cfg.get("challange_producer", "challenges_count")
    print(challenges_count)
    challanges_file_path = cfg.get("challange_producer", "challanges_file_paths")
    host = cfg.get("challange_producer", "host")
    port = cfg.get("challange_producer", "port")

    generate_challange(challenges_count)

    app = Flask(__name__)
    @app.route("/challange", methods=["GET"])
    def random_challange():
        try:
            challange_existance_check(challanges_file_path)
            result = get_random_from_excel(challanges_file_path)
            return jsonify({"random_challange": result["challange"]}), 200
        except Exception as e:
            return jsonify({"error": str(e)}), 400

    if __name__ == "__main__":
        app.run(host="0.0.0.0", port=5000)
