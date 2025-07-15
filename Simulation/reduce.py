# refactored_reduce.py
from flask import Flask, request, jsonify
from collections import defaultdict
import json

app = Flask(__name__)
aggregated = defaultdict(list)

@app.route("/reduce", methods=["POST"])
def reduce_handler():
    data = request.json
    key = "MinedBlock"
    aggregated[key].append(data)
    print(f"[REDUCER] Received block #{data['index']} from {data['source']}")
    return jsonify({"status": "ok"})

@app.route("/results", methods=["GET"])
def get_results():
    return jsonify({k: v for k, v in aggregated.items()})

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
