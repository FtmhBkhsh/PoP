"""
Locality-sensitive hashing over flattened DNN weight vectors, based on
p-stable distributions (paper Sec. II-C, V-C).

h_{a,b}(x) = floor((a . x + b) / r)

l independent groups of k hash functions each. Two vectors match under the
LSH scheme if all k hashes agree in at least one of the l groups.
"""
import math

import torch


def flatten_state_dict(state_dict) -> torch.Tensor:
    parts = [v.reshape(-1).float() for v in state_dict.values() if torch.is_tensor(v)]
    return torch.cat(parts)


class PStableLSH:
    """p-stable LSH that never materializes a dense (l, k, dim) projection
    matrix. For high-dimensional DNN weight vectors (millions of params)
    that matrix would itself be gigabytes (e.g. dim=11M, l=8, k=4 -> ~1.4GB),
    which OOM-kills small VMs. Instead each hash function's projection
    vector `a_{i,j}` is generated on demand from a small deterministic
    per-(i,j) seed and immediately discarded, so peak extra memory is
    O(dim) (one vector at a time) rather than O(l*k*dim). Manager and
    workers derive identical seeds, so they always agree on the same
    hash functions."""

    def __init__(self, dim: int, r: float, k: int, l: int, seed: int = 0):
        self.dim = dim
        self.r = r
        self.k = k
        self.l = l
        self.seed = seed
        gen = torch.Generator().manual_seed(seed)
        self.b = torch.rand(l, k, generator=gen) * r  # (l, k) - tiny, fine to store

    def _a_vector(self, i: int, j: int) -> torch.Tensor:
        sub_seed = (self.seed * 1_000_003 + i * 97 + j) % (2 ** 31 - 1)
        g = torch.Generator().manual_seed(sub_seed)
        return torch.randn(self.dim, generator=g)

    def hash(self, x: torch.Tensor) -> torch.Tensor:
        """Return an (l, k) integer tensor of bucket ids for vector x."""
        out = torch.empty(self.l, self.k, dtype=torch.int64)
        for i in range(self.l):
            for j in range(self.k):
                a = self._a_vector(i, j)
                proj = torch.dot(a, x)
                out[i, j] = int(torch.floor((proj + self.b[i, j]) / self.r).item())
        return out

    @staticmethod
    def matches(h1: torch.Tensor, h2: torch.Tensor) -> bool:
        """True if h1 and h2 agree on all k hashes in at least one of the l groups."""
        agree = (h1 == h2).all(dim=1)  # (l,)
        return bool(agree.any().item())

    @staticmethod
    def match_probability(distance: float, r: float, k: int, l: int) -> float:
        """Prlsh(c, r, k, l) = 1 - (1 - p^k)^l, p = collision prob for one
        hash function under p-stable (Gaussian) LSH."""
        if distance <= 0:
            return 1.0
        c = distance / r

        def phi(z):
            return 0.5 * (1 + math.erf(z / math.sqrt(2)))

        # collision probability for a single hash function, standard formula
        # for 2-stable LSH: p(c) = 1 - 2*Phi(-1/c) - (2/(c*sqrt(2*pi))) * (1 - exp(-1/(2*c^2)))
        p = 1 - 2 * phi(-1 / c) - (2 / (c * math.sqrt(2 * math.pi))) * (1 - math.exp(-1 / (2 * c ** 2)))
        p = min(max(p, 0.0), 1.0)
        return 1 - (1 - p ** k) ** l


def tune_lsh_params(alpha: float, beta: float, target_p_alpha: float = 0.95,
                     target_p_beta: float = 0.05, k_max: int = 4, l_max: int = 8):
    """Grid-search small (r, k, l) satisfying Prlsh(alpha) >= target_p_alpha
    and Prlsh(beta) <= target_p_beta with k*l <= k_max*l_max (paper Eq. 6)."""
    best = None
    for r in [alpha * f for f in (0.5, 1.0, 1.5, 2.0, 3.0)]:
        for k in range(1, k_max + 1):
            for l in range(1, l_max + 1):
                pa = PStableLSH.match_probability(alpha, r, k, l)
                pb = PStableLSH.match_probability(beta, r, k, l)
                if pa >= target_p_alpha and pb <= target_p_beta:
                    cost = k * l
                    if best is None or cost < best[0]:
                        best = (cost, r, k, l)
    if best is None:
        # fall back to a conservative default
        return {"r": alpha * 2.0, "k": 4, "l": 8}
    _, r, k, l = best
    return {"r": r, "k": k, "l": l}
