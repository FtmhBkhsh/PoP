# RPoL — Robust and Efficient Proof of Learning

A working implementation of the scheme from *"Secure Collaborative Learning
in Mining Pool via Robust and Efficient Verification"* (RPoL), built to run
on a real P2P network: **1 manager node + 4 worker nodes = 5 machines**.

It implements the three core pieces of the paper:

| Paper section | Code |
|---|---|
| V-A Address-encoded DNN model (AMLayer) | `rpol_pool/common/am_layer.py` |
| V-B Commitment-based secure sampling + deterministic mini-batch SGD | `rpol_pool/common/prf.py`, `rpol_pool/common/commitment.py` |
| V-C LSH-based optimization (fuzzy matching, double-check) | `rpol_pool/common/lsh.py` |
| Manager/worker protocol (Fig. 2) | `rpol_pool/manager.py`, `rpol_pool/worker.py` |

## What it does

1. The manager builds the global model with an `AMLayer` deterministically
   derived from its blockchain address (a non-trainable, spectrally
   normalized residual conv layer — Eq. 3–4 in the paper) and shuffles +
   partitions the dataset into 4 i.i.d. sub-datasets (one per worker).
2. Each epoch, the manager sends the current global weights and a fresh
   nonce to every worker.
3. Each worker trains locally using **mini-batch stochastic-yet-deterministic
   SGD**: batch indices are derived from `PRF(nonce, step)`, so the exact
   same batches can be replayed later. It checkpoints weights every
   `checkpoint_interval` steps.
4. The worker builds a **commitment** — a hash (or LSH bucket, in `v2` mode)
   for every checkpoint's input/output weights — and sends it to the
   manager *before* learning which checkpoints will be sampled.
5. The manager samples `samples_per_epoch` checkpoints, requests the raw
   proofs, **re-executes the same deterministic steps itself**, and checks
   either the exact Euclidean distance (`mode: "v1"`) or the LSH-fuzzy match
   with a double-check fallback (`mode: "v2"`, ~50% less bandwidth — Sec.
   VII-E, Table III).
6. Verified workers' final weights are aggregated (FedAvg, weighted by
   sub-dataset size) into the new global model; unverified/dishonest
   submissions are dropped from that round.

## 1. Install on all 5 machines

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
```

All 5 nodes need to be able to load the **same dataset** (CIFAR-10/100 via
torchvision, auto-downloaded on first run to `data_root`) since the manager
independently replays each worker's assigned indices during verification —
either let each node download it once, or share `data_root` over NFS.

## 2. Configure the network

Copy `config.example.json` to `config.json` and fill in the real IP address
of each of your 5 machines and a (simulated) blockchain address string for
the manager:

```jsonc
{
  "manager": { "host": "10.0.0.1", "port": 9000, "address": "0xYourManagerAddr" },
  "workers": [
    { "id": "w0", "host": "10.0.0.2", "address": "..." },
    { "id": "w1", "host": "10.0.0.3", "address": "..." },
    { "id": "w2", "host": "10.0.0.4", "address": "..." },
    { "id": "w3", "host": "10.0.0.5", "address": "..." }
  ],
  "training": { "arch": "resnet18", "dataset": "cifar10", "epochs": 40, ... }
}
```

Copy the **same** `config.json` to all 5 nodes (worker `host` fields aren't
used by the code — they're just documentation of your topology — the
manager only needs its own `host:port` to bind, and workers only need the
manager's address to connect).

Open `manager.port` (default `9000`) in each machine's firewall between
these 5 hosts.

### If you're using the `produce` / `map1`–`map5` / `reduce` Vagrantfile

`config.example.json` in this repo is already pre-filled for a topology
like:

| VM | IP | Role | worker id |
|---|---|---|---|
| `produce` | 192.168.56.10 | manager | — |
| `map1`–`map5` | 192.168.56.11–.15 | workers | `w0`–`w4` |
| `reduce` | 192.168.56.16 | unused | — |

Since `config.vm.synced_folder ".", "/home/vagrant/DPF"` mirrors the folder
containing the `Vagrantfile` to `/home/vagrant/DPF` on **every** VM, if you
unzip this project into that same host folder (next to the Vagrantfile),
all the code and `config.example.json` show up on all 7 VMs automatically —
no manual `scp` needed. Just:

```bash
cp config.example.json config.json   # do this once, on the host
```

then, on `produce`:
```bash
cd /home/vagrant/DPF
/home/vagrant/venv/bin/python -m rpol_pool.manager --config config.json
```

and on each of `map1`–`map5` (adjust `--id` per node):
```bash
cd /home/vagrant/DPF
/home/vagrant/venv/bin/python -m rpol_pool.worker --config config.json --id w0 --manager-host 192.168.56.10
```

(`w0`→`map1`, `w1`→`map2`, `w2`→`map3`, `w3`→`map4`, `w4`→`map5`.)

`reduce` isn't used by this 2-role protocol — it's a spare node if you
later want to move aggregation off the manager.

## 3. Run it

On the manager node:

```bash
python -m rpol_pool.manager --config config.json
```

On each of the 4 worker nodes (one command per node, with the matching id):

```bash
python -m rpol_pool.worker --config config.json --id w0 --manager-host 10.0.0.1
python -m rpol_pool.worker --config config.json --id w1 --manager-host 10.0.0.1
python -m rpol_pool.worker --config config.json --id w2 --manager-host 10.0.0.1
python -m rpol_pool.worker --config config.json --id w3 --manager-host 10.0.0.1
```

Workers will block retrying the connection until the manager is up, so
start them in any order. Training logs stream on both sides; the final
global model is saved to `global_model.pt` on the manager.

## Config knobs (`training` block)

- `arch`: `resnet18` or `resnet50` (small CIFAR-style variants, Sec. VII-A)
- `dataset`: `cifar10` or `cifar100`
- `mode`: `"v1"` (raw distance-based verification) or `"v2"` (LSH fuzzy
  matching + double-check, Sec. V-C — matches the paper's `RPoLv1`/`RPoLv2`)
- `checkpoint_interval`: steps between saved checkpoints (paper default: 5)
- `samples_per_epoch`: number of checkpoints the manager audits per worker
  per epoch (paper default: 3 — see the soundness analysis in Theorem 2/3)
- `beta_multiplier`: β = multiplier × α, the spoof-distance threshold
  (paper example: 5×)
- `am_c`: AMLayer Lipschitz scaling coefficient (paper default: 0.5)

## Notes / simplifications vs. the paper

- **α/β calibration**: the paper measures max reproduction error across two
  *different physical GPUs*. This code approximates that on start-up by
  building the model twice with different random seeds and measuring the
  resulting distance — swap `Manager._calibrate_thresholds` for a
  proper two-GPU measurement (Sec. V-C) if you want the paper's exact
  adaptive per-epoch recalibration.
- **Adversarial workers**: this implementation trusts workers to behave
  honestly for now — it doesn't ship simulated `Adv1`/`Adv2` attackers.
  Injecting the spoofing strategy from Eq. (12) into `worker.py` would let
  you reproduce the paper's Fig. 6 attack experiments.
- **Aggregation**: simple weighted FedAvg; the paper's economic reward
  distribution (proportional payout to verified workers via blockchain
  addresses) isn't implemented — `Manager.run_epoch` logs which workers
  were verified, which is the input you'd feed to a payout step.
