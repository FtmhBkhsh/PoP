"""
Mini-batch stochastic-yet-deterministic data selection (paper Sec. V-B).

Given a per-epoch nonce N_t issued by the manager, worker w picks the n-th
element of a training step m as PRF(N_t * m + n) mod |D_w|. Because the PRF
is a keyed pseudo-random function (HMAC-SHA256) rather than plain SGD's
random shuffling, both the worker and (upon verification) the manager can
regenerate the exact same batch indices deterministically, while an outside
observer without the nonce cannot predict them ahead of time.
"""
import hashlib
import hmac


def _prf_int(key: bytes, message: int) -> int:
    digest = hmac.new(key, message.to_bytes(16, "big", signed=False), hashlib.sha256).digest()
    return int.from_bytes(digest, "big")


def batch_indices(nonce: int, step: int, batch_size: int, subset_size: int) -> list:
    """Deterministically select `batch_size` indices in [0, subset_size)."""
    key = nonce.to_bytes(16, "big", signed=False)
    indices = []
    for n in range(batch_size):
        msg = nonce * step + n
        idx = _prf_int(key, msg) % subset_size
        indices.append(idx)
    return indices
