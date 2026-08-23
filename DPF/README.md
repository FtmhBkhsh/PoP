# DPF — Decentralized Data Processing Framework (PoUW)

A working implementation of the framework from *"A Decentralized Data
Processing Framework Based on PoUW Blockchain"* (Li, Zhao, Li — BlockSys
2020), built as a flat P2P network of identical nodes (no manager/worker
split — every node can process tasks, introduce new tasks, and become
scheduler).

It implements the core pieces of the paper:

| Paper section | Code |
|---|---|
| Sec. 3.1 Block / chain structure, genesis | `Worker.py` (`_make_genesis`, `_hash`, `_save_chain`) |
| PoUW consensus (useful-work-weighted election) | `PoUW.py` |
| Node-to-node communication | `rpc_server.py` (JSON-RPC 2.0 server), `Worker._call_rpc` (client) |
| Task introduction (task-manager role) | `Worker.collect_local_pending_tasks` |
| Task execution (mapper role — word count) | `Worker.execute_task` |
| Scheduler role (post-election): task collection, dispatch, block creation | `Scheduler.py` |
| Main node loop (Fig. in paper: select → process → broadcast → elect) | `DPF.py` |

## What it does

1. On first boot, every node deterministically builds the **same genesis
   block** from the shared `data_files` list (no timestamps or
   node-specific values go into it), seeding the first
   `genesis_rows_per_file` rows of every CSV as pending tasks so there's
   real work from block 1.
2. Each node loops forever:
   - **Sync** its local chain to the longest valid chain seen among peers
     (`sync_chain`, via JSON-RPC `get_chain`).
   - **Select** one unclaimed pending task from anywhere in the chain
     (`select_task`) — any node can execute any task, since every node
     keeps every `data_files` CSV locally.
   - **Broadcast** that it's now processing the task
     (`broadcast_processing` → RPC `submit_processing` to every peer).
   - **Execute** the task: a simple word-count mapper over the assigned
     CSV row(s) (`execute_task`), and estimate the CPU instructions spent
     doing it (`measure_cpu` — this is the PoUW quantity `m`).
   - **Broadcast** the completed result (`broadcast_completed` → RPC
     `submit_completed`).
3. Right after finishing a task, the node runs one **PoUW election**
   (`PoUW.scheduler_election(m, difficulty)`): it hashes a random nonce
   together with `m`; the higher `m` is relative to the network
   `difficulty`, the wider the winning threshold — so doing more real,
   useful work raises the odds of winning, instead of wasting cycles on
   arbitrary hashing the way plain PoW does.
4. **If a node wins the election**, it instantiates a `Scheduler`:
   - Fans out over RPC to pull every peer's pending/processing/completed
     transaction pools and merges them with its own (`collect_pending_tasks`,
     `collect_completed_tasks`).
   - Round-robins the still-pending tasks back out to peers as an
     availability hint (`dispatch_tasks` → RPC `assign_tasks`).
   - Builds a new block from those three transaction sets, chained onto
     the previous block's hash (`create_block`), and broadcasts it to
     every peer (`broadcast_block` → RPC `receive_block`), which each
     peer validates and appends before continuing its own loop.
5. Every node that isn't the task manager for a given file only
   **executes** work; only the node whose `task_manager_file` matches
   introduces new rows from that file, one `task_chunk_size` chunk at a
   time (`collect_local_pending_tasks`), which is what a scheduler pulls
   into `pending_tasks` for the next block.

## 1. Install on all nodes

```bash
python3 -m venv venv && source venv/bin/activate
pip install pandas psutil requests
```

Every node needs its own local copy of **every** file listed in
`data_files` (they all execute tasks against any of them), so either copy
the CSVs to each machine or share them over NFS.

## 2. Configure the network

Copy `config.example.json` (or the sample `config.json` below) to each
node and adjust `node_id`, `peers`, and `task_manager_file` per machine:

```jsonc
{
    "node_id": "map1",

    "peers": [
        "http://192.168.56.12:5000",
        "http://192.168.56.13:5000",
        "http://192.168.56.14:5000",
        "http://192.168.56.15:5000"
    ],

    "listen_host": "0.0.0.0",
    "listen_port": 5000,

    "chain_file": "blockchain.json",
    "difficulty": 10000000,

    "data_files": ["data1.csv", "data2.csv", "data3.csv", "data4.csv", "data5.csv"],
    "task_manager_file": "data1.csv",
    "task_column": "answer",
    "task_chunk_size": 50,
    "genesis_rows_per_file": 5,

    "logging": {
        "level": "DEBUG",
        "file": "logs/dpf.log",
        "console": true
    }
}
```

Notes:

- `peers` lists every **other** node's RPC URL — a node never lists
  itself. Each node's own `listen_host`/`listen_port` is where its
  JSON-RPC server binds (see `rpc_server.py`).
- `data_files`, `genesis_rows_per_file`, and `difficulty` **must be
  identical across every node's config** — genesis is computed
  independently on each node from these values, and it has to come out
  byte-identical everywhere.
- `task_manager_file` is the one file *this* node is responsible for
  introducing new rows from; give each node in the pool a different
  file (e.g. `map1` → `data1.csv`, `map2` → `data2.csv`, …) so all five
  CSVs get introduced somewhere.

Open `listen_port` (default `5000`) in each machine's firewall between
all peer hosts.

### If you're using a `map1`–`map5` Vagrant topology

For a topology like:

| VM | IP | `node_id` | `task_manager_file` |
|---|---|---|---|
| `map1` | 192.168.56.11 | `map1` | `data1.csv` |
| `map2` | 192.168.56.12 | `map2` | `data2.csv` |
| `map3` | 192.168.56.13 | `map3` | `data3.csv` |
| `map4` | 192.168.56.14 | `map4` | `data4.csv` |
| `map5` | 192.168.56.15 | `map5` | `data5.csv` |

give each VM a `config.json` whose `peers` list contains the other four
hosts' `http://<ip>:5000` URLs, then on every VM:

```bash
cd /path/to/DPF
source venv/bin/activate
python DPF.py
```

## 3. Run it

On each node:

```bash
python DPF.py
```

There's no separate manager process to start first — every node is
symmetric, starts its own RPC server (`start_rpc_server`), and begins
looping immediately. `sync_chain` at the top of each loop iteration means
nodes catch up automatically however they're started or restarted. The
local chain is persisted to `chain_file` (`blockchain.json` by default)
after every accepted or self-created block, atomically (`_save_chain`
writes to a temp file and `os.replace`s it, so a crash mid-write can't
corrupt the file).

## Config knobs

- `node_id`: this node's identifier, used as its JSON-RPC client id and
  in log lines.
- `peers`: list of every *other* node's `http://host:port` base URL.
- `listen_host` / `listen_port`: where this node's own JSON-RPC server
  binds (default `0.0.0.0:5000`).
- `chain_file`: local path the blockchain is persisted to.
- `difficulty`: PoUW difficulty coefficient `d` — higher values make
  winning a scheduler election harder for a given amount of useful work
  `m` (see `PoUW.scheduler_election`).
- `data_files`: the full shared list of CSVs every node keeps locally;
  must match across all nodes (used to build genesis).
- `task_manager_file`: which file *this* node introduces new task rows
  from.
- `task_column`: the CSV column whose text is fed to the word-count
  mapper (`execute_task`).
- `task_chunk_size`: how many new rows a task manager introduces per
  call to `collect_local_pending_tasks`.
- `genesis_rows_per_file`: how many rows of every file get pre-seeded as
  pending tasks in the genesis block.
- `logging`: standard `level` / `file` / `console` logging setup, applied
  by `Config.setup_logging`.

## Notes / simplifications vs. the paper

- **Proof attestation**: the paper's PoUW requires the executed
  instruction count `m` to be attested by a Trusted Execution Environment
  (e.g. Intel SGX) so a worker can't lie about how much work it did. This
  implementation still **self-reports** `m` via `Worker.measure_cpu`
  (elapsed time × CPU frequency, plus a per-record constant) — a worker
  could in principle inflate this. Wiring in a real TEE attestation is
  the change needed to close that gap.
- **Result collection**: `Worker.send_result` currently only logs the
  mapper output locally. Per the paper, results should be routed to a
  collector endpoint referenced by the task's `srcURL` — that wiring
  isn't implemented yet.
- **Task execution**: the mapper in `execute_task` is a simple
  word-count over `task_column` — a stand-in "useful work" payload, not
  a specific workload from the paper.
- **Communication protocol**: the paper doesn't pin down a wire format;
  this implementation uses JSON-RPC 2.0 over plain HTTP
  (`rpc_server.py` / `Worker._call_rpc`) as an engineering choice, not
  something specified by the original authors.
- **Adversarial nodes**: this implementation trusts nodes to self-report
  honestly and to broadcast rather than withhold; it doesn't ship
  simulated dishonest/withholding attackers to reproduce the paper's
  robustness experiments.
