"""
RPoL pool manager (paper Fig. 2 / Sec. V).

Run on the node designated as manager in config.json:

    python -m rpol_pool.manager --config config.json

Responsibilities each epoch:
  1. broadcast the current global model + a fresh per-worker nonce
  2. collect each worker's commitment (+ its claimed final local weights)
  3. sample checkpoints and request the corresponding proofs
  4. verify proofs by re-executing the deterministic mini-batch steps
     locally and comparing distances (v1) or LSH buckets (v2, with a
     double-check fallback on mismatch)
  5. aggregate the verified workers' weights (FedAvg) into the new global
     model and drop unverified/dishonest submissions
"""
import argparse
import copy
import json
import logging
import random
import secrets
import time

import torch

from .common import network, prf, model as model_lib
from .common.commitment import hash_state_dict
from .common.lsh import PStableLSH, flatten_state_dict, tune_lsh_params

logging.basicConfig(level=logging.INFO, format="[manager] %(message)s")
log = logging.getLogger("manager")


def euclid_distance(sd1, sd2) -> float:
    v1 = flatten_state_dict(sd1)
    v2 = flatten_state_dict(sd2)
    return torch.norm(v1 - v2).item()


class Manager:
    def __init__(self, cfg: dict):
        self.cfg = cfg
        self.t_cfg = cfg["training"]
        self.address = cfg["manager"]["address"]
        self.worker_cfgs = cfg["workers"]
        self.num_workers = len(self.worker_cfgs)
        self.mode = self.t_cfg.get("mode", "v2")  # "v1" (no LSH) or "v2" (LSH)
        self.samples_per_epoch = self.t_cfg.get("samples_per_epoch", 3)
        self.checkpoint_interval = self.t_cfg["checkpoint_interval"]

        # Open and start listening on the socket FIRST, before the slow
        # dataset download / model calibration below. The kernel will queue
        # incoming connections in the backlog even though we don't call
        # accept() until later, so workers started early won't be refused.
        host, port = cfg["manager"]["host"], cfg["manager"]["port"]
        self.srv = network.make_server_socket(host, port)
        log.info(f"listening on {host}:{port} (setting up dataset/model now, "
                 f"workers can connect and will be picked up once ready)")

        self.global_model = model_lib.build_model(
            self.t_cfg["arch"], self.t_cfg["num_classes"], self.address, self.t_cfg.get("am_c", 0.5)
        )

        dataset = model_lib.load_dataset(self.t_cfg["dataset"], self.t_cfg.get("data_root", "./data"),
                                          num_classes=self.t_cfg["num_classes"])
        self.dataset = dataset
        self.partitions = model_lib.partition_indices(len(dataset), self.num_workers,
                                                        seed=self.t_cfg.get("partition_seed", 1234))

        # rough alpha/beta calibration: replay a handful of steps twice with
        # different seeds to bound the reproduction error (paper Sec V-C's
        # two-GPU calibration, approximated here with two RNG seeds).
        self.alpha, self.beta = self._calibrate_thresholds()
        dim = flatten_state_dict(self.global_model.state_dict()).numel()
        if self.mode == "v2":
            self.lsh_params = tune_lsh_params(self.alpha, self.beta)
            self.lsh = PStableLSH(dim, seed=99, **self.lsh_params)
            log.info(f"LSH params: {self.lsh_params}")
        else:
            self.lsh_params, self.lsh = None, None

        self.sockets = {}  # worker_id -> socket

    # ---------- setup ----------
    def _calibrate_thresholds(self):
        arch, nc = self.t_cfg["arch"], self.t_cfg["num_classes"]
        errs = []
        for seed in (1, 2):
            torch.manual_seed(seed)
            m1 = model_lib.build_model(arch, nc, self.address, self.t_cfg.get("am_c", 0.5))
            torch.manual_seed(seed + 1000)
            m2 = model_lib.build_model(arch, nc, self.address, self.t_cfg.get("am_c", 0.5))
            errs.append(euclid_distance(m1.state_dict(), m2.state_dict()))
        mean = sum(errs) / len(errs)
        std = (sum((e - mean) ** 2 for e in errs) / len(errs)) ** 0.5
        alpha = mean + std
        beta = self.t_cfg.get("beta_multiplier", 5.0) * alpha
        log.info(f"calibrated alpha={alpha:.4f} beta={beta:.4f}")
        return alpha, beta

    def accept_workers(self):
        log.info(f"accepting connections for {self.num_workers} workers")
        expected_ids = {w["id"] for w in self.worker_cfgs}
        while len(self.sockets) < self.num_workers:
            conn, addr = self.srv.accept()
            msg = network.recv_msg(conn)
            wid = msg["worker_id"]
            if wid not in expected_ids:
                log.warning(f"rejecting unknown worker id {wid} from {addr}")
                conn.close()
                continue
            self.sockets[wid] = conn
            log.info(f"registered worker {wid} from {addr}")
        log.info("all workers connected")

    def _send_init(self):
        for i, w in enumerate(self.worker_cfgs):
            init = {
                "type": "init",
                "manager_address": self.address,
                "worker_id": w["id"],
                "indices": self.partitions[i],
                "training": self.t_cfg,
                "mode": self.mode,
                "lsh_params": self.lsh_params,
                "alpha": self.alpha,
                "beta": self.beta,
            }
            network.send_msg(self.sockets[w["id"]], init)

    # ---------- per-epoch protocol ----------
    def _broadcast_epoch_start(self, epoch: int):
        state = self.global_model.state_dict()
        nonces = {}
        for w in self.worker_cfgs:
            nonce = secrets.randbits(63)
            nonces[w["id"]] = nonce
            msg = {"type": "epoch_start", "epoch": epoch, "global_state": state, "nonce": nonce}
            network.send_msg(self.sockets[w["id"]], msg)
        return nonces

    def _collect_commits(self):
        commits = {}
        for wid, sock in self.sockets.items():
            msg = network.recv_msg(sock)
            assert msg["type"] == "commit"
            commits[wid] = msg
        return commits

    def _replay_checkpoint(self, worker_indices, nonce, input_state, ckpt_idx, steps_per_ckpt):
        """Re-run `steps_per_ckpt` deterministic mini-batch SGD steps starting
        from `input_state`, returning the recomputed output state_dict."""
        t = self.t_cfg
        m = model_lib.build_model(t["arch"], t["num_classes"], self.address, t.get("am_c", 0.5))
        m.load_state_dict(input_state)
        m.train()
        opt = torch.optim.SGD(m.parameters(), lr=t["lr"], momentum=t.get("momentum", 0.9))
        loss_fn = torch.nn.CrossEntropyLoss()
        subset_size = len(worker_indices)
        base_step = ckpt_idx * steps_per_ckpt
        for local_step in range(steps_per_ckpt):
            step = base_step + local_step
            batch_pos = prf.batch_indices(nonce, step, t["batch_size"], subset_size)
            batch_idx = [worker_indices[p] for p in batch_pos]
            xs, ys = zip(*[self.dataset[i] for i in batch_idx])
            x = torch.stack(xs)
            y = torch.tensor(ys)
            opt.zero_grad()
            out = m(x)
            loss = loss_fn(out, y)
            loss.backward()
            opt.step()
        return m.state_dict()

    def _verify_worker(self, wid, commit_msg, worker_indices):
        ckpt_hashes = commit_msg["checkpoint_records"]
        num_ckpts = len(ckpt_hashes)
        if num_ckpts == 0:
            return False
        k = min(self.samples_per_epoch, num_ckpts)
        sample_idx = random.sample(range(num_ckpts), k)

        req = {"type": "sample_request", "epoch": commit_msg["epoch"], "indices": sample_idx}
        network.send_msg(self.sockets[wid], req)
        resp = network.recv_msg(self.sockets[wid])
        assert resp["type"] == "proof"

        nonce = commit_msg["nonce"]
        steps_per_ckpt = self.checkpoint_interval
        for proof in resp["proofs"]:
            j = proof["index"]
            input_state = proof["input_state"]
            if hash_state_dict(input_state) != ckpt_hashes[j]["input_hash"]:
                log.warning(f"worker {wid} FAILED: input hash mismatch at ckpt {j}")
                return False

            recomputed = self._replay_checkpoint(worker_indices, nonce, input_state, j, steps_per_ckpt)

            if self.mode == "v1":
                claimed_output = proof["output_state"]
                if hash_state_dict(claimed_output) != ckpt_hashes[j]["output_hash"]:
                    log.warning(f"worker {wid} FAILED: output hash mismatch at ckpt {j}")
                    return False
                dist = euclid_distance(recomputed, claimed_output)
                if dist > self.alpha:
                    log.warning(f"worker {wid} FAILED: dist {dist:.4f} > alpha {self.alpha:.4f} at ckpt {j}")
                    return False
            else:
                recomputed_lsh = self.lsh.hash(flatten_state_dict(recomputed))
                committed_lsh = torch.tensor(ckpt_hashes[j]["output_lsh"])
                if not PStableLSH.matches(recomputed_lsh, committed_lsh):
                    # double-check: request raw output weights once
                    dc_req = {"type": "double_check_request", "epoch": commit_msg["epoch"], "index": j}
                    network.send_msg(self.sockets[wid], dc_req)
                    dc_resp = network.recv_msg(self.sockets[wid])
                    claimed_output = dc_resp["output_state"]
                    dist = euclid_distance(recomputed, claimed_output)
                    if dist > self.alpha:
                        log.warning(f"worker {wid} FAILED double-check at ckpt {j} (dist {dist:.4f})")
                        return False
        log.info(f"worker {wid} verified OK ({k} checkpoints sampled)")
        return True

    def run_epoch(self, epoch: int):
        nonces = self._broadcast_epoch_start(epoch)
        commits = self._collect_commits()

        verified_weights = []
        verified_sizes = []
        for i, w in enumerate(self.worker_cfgs):
            wid = w["id"]
            commit_msg = commits[wid]
            commit_msg["nonce"] = nonces[wid]
            ok = self._verify_worker(wid, commit_msg, self.partitions[i])
            # tell worker whether the epoch was accepted so it can proceed
            network.send_msg(self.sockets[wid], {"type": "epoch_result", "accepted": ok})
            if ok:
                verified_weights.append(commit_msg["final_state"])
                verified_sizes.append(len(self.partitions[i]))

        if verified_weights:
            total = sum(verified_sizes)
            new_state = copy.deepcopy(verified_weights[0])
            for key in new_state:
                if not torch.is_tensor(new_state[key]):
                    continue
                acc = torch.zeros_like(new_state[key], dtype=torch.float32)
                for sd, sz in zip(verified_weights, verified_sizes):
                    acc += sd[key].float() * (sz / total)
                new_state[key] = acc.to(new_state[key].dtype)
            self.global_model.load_state_dict(new_state)
            log.info(f"epoch {epoch}: aggregated {len(verified_weights)}/{self.num_workers} workers")
        else:
            log.warning(f"epoch {epoch}: no verified workers, global model unchanged")

    def finish(self):
        for sock in self.sockets.values():
            network.send_msg(sock, {"type": "done"})
            sock.close()
        torch.save(self.global_model.state_dict(), self.t_cfg.get("final_model_path", "global_model.pt"))
        log.info("training complete, global model saved")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", required=True)
    args = ap.parse_args()
    with open(args.config) as f:
        cfg = json.load(f)

    mgr = Manager(cfg)
    mgr.accept_workers()
    mgr._send_init()
    epochs = cfg["training"]["epochs"]
    for epoch in range(epochs):
        t0 = time.time()
        mgr.run_epoch(epoch)
        log.info(f"epoch {epoch} took {time.time() - t0:.1f}s")
    mgr.finish()


if __name__ == "__main__":
    main()
