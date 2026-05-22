import paramiko
import subprocess
import matplotlib.pyplot as plt
import re
import numpy as np

VM_NAMES = ["mapper1", "mapper2", "mapper3", "mapper4", "mapper5"]
COMMAND = "/home/vagrant/venv/bin/python3 /home/vagrant/map.py"
ITERATIONS = 2


def get_ssh_config(vm_name):
    output = subprocess.check_output(["vagrant", "ssh-config", vm_name]).decode()

    def find(key):
        return re.search(rf"{key}\s+(.*)", output).group(1).strip()

    return {
        "hostname": find("HostName"),
        "port": int(find("Port")),
        "user": find("User"),
        "key": find("IdentityFile").replace('"', "")
    }


def run_vm(vm_name):
    cfg = get_ssh_config(vm_name)

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    client.connect(
        hostname=cfg["hostname"],
        port=cfg["port"],
        username=cfg["user"],
        key_filename=cfg["key"]
    )

    results = []

    for _ in range(ITERATIONS):
        stdin, stdout, stderr = client.exec_command(COMMAND, get_pty=True)

        out = stdout.read().decode()

        numbers = re.findall(r"-?\d+(?:\.\d+)?", out)

        if numbers:
            value = float(numbers[-1])
        else:
            value = None

        results.append(value)

    client.close()

    return results


# ---------------- COLLECT DATA ----------------
all_data = {}

for vm in VM_NAMES:
    print(f"Running on {vm}...")
    all_data[vm] = run_vm(vm)


# ---------------- PLOT 1: EACH VM SEPARATE ----------------
fig, axes = plt.subplots(len(VM_NAMES), 1, figsize=(8, 12), sharex=True)

for i, vm in enumerate(VM_NAMES):
    data = all_data[vm]

    axes[i].plot(data, marker='o')
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

    if vals:
        avg_values.append(np.mean(vals))
    else:
        avg_values.append(None)


plt.figure()
plt.plot(avg_values, marker='o')
plt.title("Average Across All VMs")
plt.xlabel("Iteration")
plt.ylabel("Average Value")
plt.grid(True)
plt.show()