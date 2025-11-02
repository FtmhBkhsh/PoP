
import secrets
from typing import Tuple
from py_ecc import bls12_381 as b381
from py_ecc.bls.hash_to_curve import hash_to_G1
from py_ecc.optimized_bls12_381 import normalize
import hashlib



curve_order = b381.curve_order
field_modulus = b381.field_modulus
COORD_LEN = (field_modulus.bit_length() + 7) // 8

DST_G = b"PEDERSEN_GENERATOR_G_v1"
DST_H = b"PEDERSEN_GENERATOR_H_v1"

# --- Helper: integer → bytes (fixed-length, deterministic) ---

def int_to_bytes(x: int) -> bytes:
    if x < 0:
        raise ValueError("seed integers must be non-negative")
    length = (x.bit_length() + 7) // 8 or 1
    return x.to_bytes(length, "big")

# --- Helper: ipv4 → int (fixed-length, deterministic) ---
def ipv4_to_int(ip_str):
    a,b,c,d = map(int, ip_str.split('.'))
    return (a<<24) | (b<<16) | (c<<8) | d

# --- Derive G, H using integer seeds ---
def derive_generator_from_int(v: int, dst: bytes) -> Tuple[int, int]:
    seed = int_to_bytes(v)
    P = hash_to_G1(seed, dst, hashlib.sha256)
    P_affine = normalize(P)               # Convert to (x, y)
    x, y = map(int, P_affine)
    return (x, y)


def derive_generator_pair(v1: int, v2: int):
    G = derive_generator_from_int(v1, DST_G)
    H = derive_generator_from_int(v2, DST_H)
    if G == H:
        raise ValueError("G and H must differ; use distinct integers")
    return G, H


# --- EC arithmetic helpers ---
def point_from_int_tuple(pt: Tuple[int, int]):
    return (b381.FQ(pt[0]), b381.FQ(pt[1]))


def to_affine(pt):
    if len(pt) == 3:
        return b381.normalize(pt)
    return pt


def add_points(A, B):
    P, Q = point_from_int_tuple(A), point_from_int_tuple(B)
    R = b381.add(P, Q)
    R = to_affine(R)
    return (int(R[0]), int(R[1]))


def scalar_mul(k: int, P):
    P = point_from_int_tuple(P)
    R = b381.multiply(P, k % curve_order)
    R = to_affine(R)
    return (int(R[0]), int(R[1]))


# --- Pedersen commit & verify ---
def pedersen_commit(m: int, r: int, G, H):
    mG = scalar_mul(m, G)
    rH = scalar_mul(r, H)
    return add_points(mG, rH)

# --- Relationship check ---
def verify_same_message(C1, C2, r1, r2, H):
    """Verify that C1 and C2 commit to the same message (m unknown)."""
    delta_r = (r2 - r1) % curve_order
    rhs = add_points(C1, scalar_mul(delta_r, H))
    return C2 == rhs

# --- Example usage ---
if __name__ == "__main__":

    # Arbitrary Hashes as seeds
    v1 = 12345678901234567890
    v2 = 98765432109876543210

    G, H = derive_generator_pair(v1, v2)
    print("Generator G:", G)
    print("Generator H:", H)

    r1 = secrets.randbelow(curve_order)
    r2 = secrets.randbelow(curve_order)
    m = secrets.randbelow(curve_order)

    C1 = pedersen_commit(m, r1, G, H)
    C2 = pedersen_commit(m, r2, G, H)

    print("\nC1:", C1)
    print("C2:", C2)

    # Verify that both commit to the same message
    ok = verify_same_message(C1, C2, r1, r2, H)
    assert verify_same_message(C1, C2, r1, r2, H)
    print("\nSame-message verification:", "✔️ OK" if ok else "❌ FAILED")