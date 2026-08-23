"""
Commitment-based secure sampling (paper Sec. V-B).

A worker stores raw model weights every `checkpoint_interval` steps as
"training proofs". At the end of an epoch it publishes a commitment BEFORE
learning which checkpoints will actually be sampled, preventing it from
retroactively faking only the sampled ones. We implement the commitment as
an ordered hash chain over the checkpoint hashes (a valid alternative the
paper explicitly allows to a Merkle tree):

    commit_0 = H(worker_id || epoch)
    commit_i = H(commit_{i-1} || H(checkpoint_i))

The final `commit_n` plus the ordered list of individual checkpoint hashes
is sent to the manager as the commitment. Any single checkpoint's hash can
then be independently checked against what the worker reveals later.
"""
import hashlib

import torch


def hash_state_dict(state_dict) -> bytes:
    h = hashlib.sha256()
    for name in sorted(state_dict.keys()):
        v = state_dict[name]
        if torch.is_tensor(v):
            h.update(name.encode())
            h.update(v.detach().cpu().numpy().tobytes())
    return h.digest()


def build_commitment(worker_id: str, epoch: int, checkpoint_hashes: list) -> dict:
    root = hashlib.sha256((worker_id + str(epoch)).encode()).digest()
    chain = [root]
    for ch in checkpoint_hashes:
        root = hashlib.sha256(root + ch).digest()
        chain.append(root)
    return {
        "worker_id": worker_id,
        "epoch": epoch,
        "checkpoint_hashes": checkpoint_hashes,
        "root": chain[-1],
    }


def verify_checkpoint_hash(commit: dict, index: int, state_dict) -> bool:
    if index < 0 or index >= len(commit["checkpoint_hashes"]):
        return False
    return hash_state_dict(state_dict) == commit["checkpoint_hashes"][index]
