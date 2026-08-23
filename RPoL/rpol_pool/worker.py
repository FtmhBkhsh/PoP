"""
RPoL pool worker (paper Fig. 2 step 2 / Sec. V-B).

Run on each worker node in config.json:

    python -m rpol_pool.worker --config config.json --id w0 --manager-host 10.0.0.1

Each epoch the worker:
  1. receives the global model + a nonce from the manager
  2. runs mini-batch stochastic-yet-deterministic SGD over its local
     sub-dataset, snapshotting weights to disk every `checkpoint_interval`
     steps (paper Sec. V-B: workers "store raw model parameters ... as
     training proofs" — kept on storage, not held all in RAM at once,
     which matters on memory-constrained nodes)
  3. builds a commitment over the snapshots (input hash + output hash/LSH
     bucket per checkpoint) and sends it, along with its final local
     weights, to the manager
  4. reveals the raw input/output weights for whichever checkpoints the
     manager samples (only after the commitment was already sent),
     loading each snapshot from disk on demand
"""
import argparse
import copy
import json
import logging
import os
import shutil
import tempfile

import torch

from .common import network, prf, model as model_lib
from .common.commitment import hash_state_dict
from .common.lsh import PStableLSH, flatten_state_dict

logging.basicConfig(level=logging.INFO, format="[worker] %(message)s")


class Worker:
    def __init__(self, worker_id: str, manager_host: str, manager_port: int):
        self.worker_id = worker_id
        self.log = logging.getLogger(worker_id)
        self.sock = network.connect_with_retry(manager_host, manager_port)
        network.send_msg(self.sock, {"worker_id": worker_id})

        init = network.recv_msg(self.sock)
        assert init["type"] == "init"
        self.manager_address = init["manager_address"]
        self.indices = init["indices"]
        self.t_cfg = init["training"]
        self.mode = init["mode"]
        self.lsh_params = init["lsh_params"]
        self.alpha = init["alpha"]
        self.checkpoint_interval = self.t_cfg["checkpoint_interval"]

        self.dataset = model_lib.load_dataset(self.t_cfg["dataset"], self.t_cfg.get("data_root", "./data"),
                                               num_classes=self.t_cfg["num_classes"])

        dim_model = model_lib.build_model(
            self.t_cfg["arch"], self.t_cfg["num_classes"], self.manager_address, self.t_cfg.get("am_c", 0.5)
        )
        dim = flatten_state_dict(dim_model.state_dict()).numel()
        del dim_model
        self.lsh = PStableLSH(dim, seed=99, **self.lsh_params) if self.mode == "v2" else None

        self.ckpt_root = tempfile.mkdtemp(prefix=f"rpol_{worker_id}_")
        self._epoch_ckpt_paths = []  # populated each epoch, cleaned up after answering

        self.log.info(f"initialized with {len(self.indices)} local samples, mode={self.mode}, "
                       f"checkpoints spilled to {self.ckpt_root}")

    def _build_model(self):
        m = model_lib.build_model(self.t_cfg["arch"], self.t_cfg["num_classes"],
                                   self.manager_address, self.t_cfg.get("am_c", 0.5))
        return m

    def run_epoch(self, epoch: int, global_state: dict, nonce: int):
        t = self.t_cfg
        model = self._build_model()
        model.load_state_dict(global_state)
        model.train()
        opt = torch.optim.SGD(model.parameters(), lr=t["lr"], momentum=t.get("momentum", 0.9))
        loss_fn = torch.nn.CrossEntropyLoss()

        subset_size = len(self.indices)
        steps_per_epoch = max(1, subset_size // t["batch_size"])
        interval = self.checkpoint_interval
        num_checkpoints = max(1, steps_per_epoch // interval)

        epoch_dir = os.path.join(self.ckpt_root, f"epoch_{epoch}")
        os.makedirs(epoch_dir, exist_ok=True)
        ckpt_paths = []

        def _save_snapshot(idx):
            path = os.path.join(epoch_dir, f"ckpt_{idx}.pt")
            torch.save(copy.deepcopy(model.state_dict()), path)
            ckpt_paths.append(path)

        _save_snapshot(0)  # snapshot_0
        step = 0
        for ckpt in range(num_checkpoints):
            for _ in range(interval):
                batch_pos = prf.batch_indices(nonce, step, t["batch_size"], subset_size)
                batch_idx = [self.indices[p] for p in batch_pos]
                xs, ys = zip(*[self.dataset[i] for i in batch_idx])
                x = torch.stack(xs)
                y = torch.tensor(ys)
                opt.zero_grad()
                out = model(x)
                loss = loss_fn(out, y)
                loss.backward()
                opt.step()
                step += 1
            _save_snapshot(ckpt + 1)

        del model, opt  # free the live training copy; snapshots now live on disk

        # build commitment by reading snapshots back one at a time, so we
        # never hold more than two state_dicts in memory at once
        checkpoint_records = []
        for j in range(len(ckpt_paths) - 1):
            sd_in = torch.load(ckpt_paths[j])
            rec = {"input_hash": hash_state_dict(sd_in)}
            del sd_in
            sd_out = torch.load(ckpt_paths[j + 1])
            if self.mode == "v1":
                rec["output_hash"] = hash_state_dict(sd_out)
            else:
                rec["output_lsh"] = self.lsh.hash(flatten_state_dict(sd_out)).tolist()
            del sd_out
            checkpoint_records.append(rec)

        self._epoch_ckpt_paths = ckpt_paths
        final_state = torch.load(ckpt_paths[-1])

        commit_msg = {
            "type": "commit",
            "epoch": epoch,
            "checkpoint_records": checkpoint_records,
            "final_state": final_state,
        }
        network.send_msg(self.sock, commit_msg)

    def answer_sampling(self):
        req = network.recv_msg(self.sock)
        assert req["type"] == "sample_request"
        proofs = []
        for j in req["indices"]:
            proof = {"index": j, "input_state": torch.load(self._epoch_ckpt_paths[j])}
            if self.mode == "v1":
                proof["output_state"] = torch.load(self._epoch_ckpt_paths[j + 1])
            proofs.append(proof)
        network.send_msg(self.sock, {"type": "proof", "proofs": proofs})

        # handle any double-check requests (LSH mode only)
        while True:
            msg = network.recv_msg(self.sock)
            if msg["type"] == "double_check_request":
                j = msg["index"]
                output_state = torch.load(self._epoch_ckpt_paths[j + 1])
                network.send_msg(self.sock, {"output_state": output_state})
            else:
                self._cleanup_epoch_checkpoints()
                return msg  # epoch_result

    def _cleanup_epoch_checkpoints(self):
        if self._epoch_ckpt_paths:
            epoch_dir = os.path.dirname(self._epoch_ckpt_paths[0])
            shutil.rmtree(epoch_dir, ignore_errors=True)
            self._epoch_ckpt_paths = []

    def run(self):
        try:
            while True:
                msg = network.recv_msg(self.sock)
                if msg["type"] == "done":
                    self.log.info("training complete")
                    break
                assert msg["type"] == "epoch_start"
                epoch = msg["epoch"]
                self.run_epoch(epoch, msg["global_state"], msg["nonce"])
                result = self.answer_sampling()
                status = "accepted" if result.get("accepted") else "REJECTED"
                self.log.info(f"epoch {epoch}: {status}")
        finally:
            shutil.rmtree(self.ckpt_root, ignore_errors=True)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    ap.add_argument("--id", required=True, help="worker id, must match config.json")
    ap.add_argument("--manager-host", default=None, help="override manager host from config")
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = json.load(f)

    host = args.manager_host or cfg["manager"]["host"]
    port = cfg["manager"]["port"]
    w = Worker(args.id, host, port)
    w.run()


if __name__ == "__main__":
    main()

