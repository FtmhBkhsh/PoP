import paramiko
import subprocess
import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import matplotlib.pyplot as plt
import numpy as np
from openpyxl import Workbook
from openpyxl.chart import LineChart, Reference

VM_NAMES = ["map1", "map2", "map3", "map4", "map5"]
COMMAND = "/home/vagrant/venv/bin/python3 /home/vagrant/map.py"
ITERATIONS = 1000

PROGRESS_EVERY = 25  # print a status line every N rounds

# `vagrant` subcommands (ssh-config, ssh, etc.) only work when run with the
# directory containing the Vagrantfile as the current working directory —
# they don't search parent/child directories for one. Pinning this to the
# script's own location means `python controller.py` works no matter what
# directory you launch it from, instead of silently depending on it.
# If your Vagrantfile lives somewhere else relative to this script, update
# this path accordingly.
VAGRANT_DIR = os.path.dirname(os.path.abspath(__file__))

# Private-network IP each VM's map.py RPC server listens on (see
# MAPPER_ENDPOINTS in map.py). Used to send the "shutdown" RPC directly over
# HTTP once a round is done, independent of the SSH session. Double check
# this matches your Vagrantfile / Config.json private_network assignments —
# it's inferred from map.py's MAPPER_ENDPOINTS ordering (map1->.11 ... map5->.15).
VM_IPS = {
    "map1": "192.168.56.11",
    "map2": "192.168.56.12",
    "map3": "192.168.56.13",
    "map4": "192.168.56.14",
    "map5": "192.168.56.15",
}
RPC_PORT = 3000


def get_ssh_config(vm_name):
    try:
        result = subprocess.run(
            ["vagrant", "ssh-config", vm_name],
            cwd=VAGRANT_DIR,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        raise RuntimeError(
            f"`vagrant ssh-config {vm_name}` did not return within 30s "
            f"(cwd={VAGRANT_DIR}). Check `vagrant status` for this VM."
        )

    if result.returncode != 0:
        raise RuntimeError(
            f"`vagrant ssh-config {vm_name}` failed (cwd={VAGRANT_DIR}, "
            f"exit={result.returncode}).\n"
            f"stdout: {result.stdout.strip()}\n"
            f"stderr: {result.stderr.strip()}\n"
            f"Check that VAGRANT_DIR actually contains the Vagrantfile and "
            f"that `vagrant status` there shows {vm_name} as running."
        )

    output = result.stdout

    def find(key):
        return re.search(rf"{key}\s+(.*)", output).group(1).strip()

    return {
        "hostname": find("HostName"),
        "port": int(find("Port")),
        "user": find("User"),
        "key": find("IdentityFile").replace('"', "")
    }


def connect(vm_name):
    cfg = get_ssh_config(vm_name)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        hostname=cfg["hostname"],
        port=cfg["port"],
        username=cfg["user"],
        key_filename=cfg["key"],
        timeout=15,        # TCP connect timeout
        banner_timeout=15, # waiting for SSH banner
        auth_timeout=15,   # waiting for auth to complete
    )
    return client


def run_once(client):
    """Trigger map.py on one node and parse the value it reports.

    map.py now stays up after printing its execution time, waiting to
    receive blocks from peers until the controller tells it the round is
    over (see send_shutdown / wait_for_exit below). That means we can no
    longer block on stdout.read() until EOF — the process won't hit EOF
    until we shut it down. Instead, read incrementally and stop as soon as
    we see the line that's just the printed number.

    Also: previously this grabbed the *last* number anywhere in the full
    captured output, which only worked because nothing was printed after
    the timing line. Now that add_block logs (full of digits) come after
    it, we match the first line that is *only* a number instead.

    Returns (value, stdout) — stdout (and its .channel) is kept so the
    caller can later wait for the process to actually exit once shutdown
    has been sent.
    """
    stdin, stdout, stderr = client.exec_command(COMMAND, get_pty=True)
    channel = stdout.channel

    number_re = re.compile(r"^-?\d+(?:\.\d+)?$")
    value = None
    buffer = ""

    while value is None:
        if channel.exit_status_ready() and not channel.recv_ready():
            # Process ended before ever printing a clean numeric line —
            # something went wrong (crash, exception before print, etc).
            break

        chunk = channel.recv(4096).decode(errors="ignore")
        if not chunk:
            break

        buffer += chunk
        while "\n" in buffer:
            line, buffer = buffer.split("\n", 1)
            if number_re.match(line.strip()):
                value = float(line.strip())
                break

    return value, stdout


def send_shutdown(vm):
    """Tell a node's RPC server the round is over, so it exits instead of
    waiting for peers / its safety timeout."""
    try:
        requests.post(
            f"http://{VM_IPS[vm]}:{RPC_PORT}",
            json={"method": "shutdown", "params": {}, "id": 1},
            timeout=5,
        )
    except requests.RequestException as e:
        print(f"  [warn] could not send shutdown to {vm}: {e}")


def wait_for_exit(vm, stdout, timeout=30):
    """Block until the remote map.py process for this round has actually
    terminated, so the next round doesn't try to rebind port 3000 while the
    old process is still shutting down."""
    channel = stdout.channel
    channel.settimeout(timeout)
    try:
        channel.recv_exit_status()
    except Exception as e:
        print(f"  [warn] {vm} didn't exit cleanly within {timeout}s: {e}")


# ---------------- OPEN ALL CONNECTIONS ONCE ----------------
print("Connecting to all nodes...")
clients = {}
for vm in VM_NAMES:
    print(f"  connecting to {vm}...", flush=True)
    try:
        clients[vm] = connect(vm)
        print(f"  {vm} connected.")
    except Exception as e:
        print(f"  [ERROR] {vm} failed to connect: {e}")
        raise
print("All 5 nodes connected.\n")

# ---------------- BENCHMARK LOOP ----------------
# Each round fires map.py on all 5 VMs at (near) the same instant, since
# map.py itself does the P2P consensus / block-add work. We must wait for
# every node to finish before starting the next round, otherwise overlapping
# consensus rounds would interfere with each other.
all_data = {vm: [] for vm in VM_NAMES}
round_wall_times = []  # wall-clock time for the whole round (max across nodes)

start_all = time.time()

with ThreadPoolExecutor(max_workers=len(VM_NAMES)) as pool:
    for i in range(ITERATIONS):
        round_start = time.time()

        futures = {pool.submit(run_once, clients[vm]): vm for vm in VM_NAMES}

        round_stdouts = {}
        for future in as_completed(futures):
            vm = futures[future]
            try:
                value, stdout = future.result()
            except Exception as e:
                print(f"  [iter {i+1}] {vm} failed: {e}")
                value, stdout = None, None
            all_data[vm].append(value)
            round_stdouts[vm] = stdout

        # Every node has finished its own local mining and reported a
        # value — that's the window during which peers were able to
        # deliver blocks to each other. Now tell every node the round is
        # over so it exits (rather than waiting out its safety timeout),
        # and wait for each process to actually terminate before reusing
        # port 3000 next round.
        for vm in VM_NAMES:
            send_shutdown(vm)

        for vm in VM_NAMES:
            if round_stdouts.get(vm) is not None:
                wait_for_exit(vm, round_stdouts[vm])

        round_wall_times.append(time.time() - round_start)

        if (i + 1) % PROGRESS_EVERY == 0 or i == 0:
            elapsed = time.time() - start_all
            print(f"Round {i+1}/{ITERATIONS} done "
                  f"(last round {round_wall_times[-1]:.3f}s, elapsed {elapsed:.1f}s)")

for client in clients.values():
    client.close()

print(f"\nBenchmark finished: {ITERATIONS} rounds in {time.time() - start_all:.1f}s total.\n")

# ---------------- SAVE TO EXCEL ----------------
wb = Workbook()
ws = wb.active
ws.title = "Results"

ws.append(["Iteration"] + VM_NAMES + ["Average", "Round Wall Time (s)"])

for i in range(ITERATIONS):
    row = [i + 1]
    vals = []
    for vm in VM_NAMES:
        v = all_data[vm][i]
        row.append(v)
        if v is not None:
            vals.append(v)
    avg = sum(vals) / len(vals) if vals else None
    row.append(avg)
    row.append(round_wall_times[i])
    ws.append(row)

chart1 = LineChart()
chart1.title = "Block-Add Time per Node"
chart1.x_axis.title = "Iteration"
chart1.y_axis.title = "Value"
data = Reference(ws, min_col=2, max_col=1 + len(VM_NAMES), min_row=1, max_row=1 + ITERATIONS)
cats = Reference(ws, min_col=1, min_row=2, max_row=1 + ITERATIONS)
chart1.add_data(data, titles_from_data=True)
chart1.set_categories(cats)
ws.add_chart(chart1, "I2")

chart2 = LineChart()
chart2.title = "Average Block-Add Time Across Nodes"
chart2.x_axis.title = "Iteration"
chart2.y_axis.title = "Average Value"
avg_col = 2 + len(VM_NAMES)
data2 = Reference(ws, min_col=avg_col, max_col=avg_col, min_row=1, max_row=1 + ITERATIONS)
chart2.add_data(data2, titles_from_data=True)
chart2.set_categories(cats)
ws.add_chart(chart2, "I22")

wb.save("results.xlsx")
print("Saved results.xlsx")

# ---------------- PLOT 1: EACH VM SEPARATE ----------------
fig, axes = plt.subplots(len(VM_NAMES), 1, figsize=(8, 14), sharex=True)

for i, vm in enumerate(VM_NAMES):
    axes[i].plot(all_data[vm], marker='.', markersize=2, linewidth=0.7)
    axes[i].set_title(vm)
    axes[i].grid(True)
    axes[i].set_ylabel("Value")

plt.xlabel("Iteration")
plt.tight_layout()
plt.show()

# ---------------- PLOT 2: AVERAGE ----------------
avg_values = []
for i in range(ITERATIONS):
    vals = [all_data[vm][i] for vm in VM_NAMES if all_data[vm][i] is not None]
    avg_values.append(np.mean(vals) if vals else None)

plt.figure()
plt.plot(avg_values, marker='.', markersize=2, linewidth=0.7, color="midnightblue")
plt.title("Average Block-Add Time Across All Nodes (1000 runs)")
plt.xlabel("Iteration")
plt.ylabel("Average Value")
plt.grid(True)
plt.show()