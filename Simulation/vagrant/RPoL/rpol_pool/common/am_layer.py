"""
Address-encoded mapping layer (AMLayer), paper Sec. V-A.

A non-trainable residual convolution whose weights are a deterministic
(pseudo-random) function of a blockchain address. It is spectrally
normalized so that ||AMLayer(x1) - AMLayer(x2)||_2 <= c * ||x1 - x2||_2
with c < 1, which the paper shows preserves model performance while making
the layer both tamper-evident and publicly re-derivable by any consensus
node that knows the address.
"""
import hashlib

import torch
import torch.nn as nn


def _seed_from_address(address: str) -> int:
    h = hashlib.sha256(address.encode("utf-8")).digest()
    return int.from_bytes(h[:8], "big")


class AMLayer(nn.Module):
    """Residual, non-trainable conv layer keyed by a blockchain address."""

    def __init__(self, address: str, in_channels: int = 3, out_channels: int = 64,
                 kernel_size: int = 3, c: float = 0.5, power_iters: int = 20):
        super().__init__()
        self.address = address
        self.c = c
        self.in_channels = in_channels
        self.out_channels = out_channels

        seed = _seed_from_address(address)
        gen = torch.Generator().manual_seed(seed)

        self.conv = nn.Conv2d(in_channels, out_channels, kernel_size,
                               padding=kernel_size // 2, stride=1, bias=False)
        with torch.no_grad():
            self.conv.weight.copy_(torch.randn(self.conv.weight.shape, generator=gen))
            self._spectral_normalize(power_iters, gen)

        # 1x1 projection so the residual add is shape-compatible when
        # in_channels != out_channels (kept fixed by the same seed).
        if in_channels != out_channels:
            self.proj = nn.Conv2d(in_channels, out_channels, 1, bias=False)
            with torch.no_grad():
                self.proj.weight.copy_(torch.randn(self.proj.weight.shape, generator=gen))
        else:
            self.proj = None

        for p in self.parameters():
            p.requires_grad_(False)

    def _spectral_normalize(self, power_iters: int, gen: torch.Generator) -> None:
        """Estimate the largest singular value via power iteration and rescale
        the conv weight so its Lipschitz constant is <= self.c (Eq. 4)."""
        w = self.conv.weight.data
        w_mat = w.reshape(w.shape[0], -1)
        u = torch.randn(w_mat.shape[0], generator=gen)
        u = u / u.norm()
        for _ in range(power_iters):
            v = w_mat.t() @ u
            v = v / (v.norm() + 1e-12)
            u = w_mat @ v
            u = u / (u.norm() + 1e-12)
        sigma = torch.dot(u, w_mat @ v).item()
        if sigma > 0 and (self.c / sigma) < 1:
            w.mul_(self.c / sigma)

    def forward(self, x: torch.Tensor) -> torch.Tensor:
        out = self.conv(x)
        residual = self.proj(x) if self.proj is not None else x
        return residual + out

    def matches_address(self, address: str, atol: float = 1e-6) -> bool:
        """Recompute the expected weights for `address` and compare."""
        ref = AMLayer(address, self.in_channels, self.out_channels,
                       self.conv.kernel_size[0], self.c)
        same = torch.allclose(ref.conv.weight, self.conv.weight, atol=atol)
        if self.proj is not None:
            same = same and torch.allclose(ref.proj.weight, self.proj.weight, atol=atol)
        return same
