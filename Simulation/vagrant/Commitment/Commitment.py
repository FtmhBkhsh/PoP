import secrets
from typing import Tuple, Dict
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

# --- Serialization helpers for hashing ---
def point_to_bytes(P: Tuple[int,int]) -> bytes:
    """Encode affine point (x,y) as fixed-length bytes x||y"""
    x_bytes = int_to_bytes(P[0]).rjust(COORD_LEN, b'\x00')
    y_bytes = int_to_bytes(P[1]).rjust(COORD_LEN, b'\x00')
    return x_bytes + y_bytes

def hash_bytes_sha256(*parts: bytes) -> bytes:
    h = hashlib.sha256()
    for p in parts:
        h.update(p)
    return h.digest()

def hash_to_int_mod_q(*parts: bytes) -> int:
    digest = hash_bytes_sha256(*parts)
    return int.from_bytes(digest, "big") % curve_order

# --- NIZK: Prover & Verifier for "I know (m,r) opening of C" ---
def prove_pedersen_opening(C: Tuple[int,int], m: int, r: int, G: Tuple[int,int], H: Tuple[int,int], context: bytes = b"") -> Dict:
    """
    Produce a non-interactive Schnorr-style proof of knowledge of (m, r) for Pedersen commitment C.
    Returns dict {'c': int, 'T': (x,y), 'u': int, 'v': int, 'h': bytes}
    where h = H(c || T || context) (an integrity field).
    """
    # randomizers
    k = secrets.randbelow(curve_order)
    s = secrets.randbelow(curve_order)

    # T = kG + sH
    T = pedersen_commit(k, s, G, H)

    # challenge c = H(T || C || context) mod q  (Fiat-Shamir)
    T_bytes = point_to_bytes(T)
    C_bytes = point_to_bytes(C)
    c = hash_to_int_mod_q(T_bytes, C_bytes, context)

    # responses u = k + c*m, v = s + c*r  (mod q)
    u = (k + (c * m)) % curve_order
    v = (s + (c * r)) % curve_order

    # integrity hash h = H(c || T || context)
    c_bytes = int_to_bytes(c).rjust((curve_order.bit_length()+7)//8, b'\x00')
    h = hash_bytes_sha256(c_bytes, T_bytes, context)

    return {'c': c, 'T': T, 'u': u, 'v': v, 'h': h}

def verify_pedersen_proof(C: Tuple[int,int], proof: Dict, G: Tuple[int,int], H: Tuple[int,int], context: bytes = b"") -> bool:
    """
    Verify the proof produced by prove_pedersen_opening.
    Checks:
      1) h == H(c || T || context)
      2) uG + vH == T + c*C
    """
    c = proof['c']
    T = proof['T']
    u = proof['u']
    v = proof['v']
    h = proof['h']

    # recompute integrity hash
    c_bytes = int_to_bytes(c).rjust((curve_order.bit_length()+7)//8, b'\x00')
    T_bytes = point_to_bytes(T)
    recomputed_h = hash_bytes_sha256(c_bytes, T_bytes, context)
    if recomputed_h != h:
        # integrity/hash mismatch
        return False

    # compute left = uG + vH
    left_uG = scalar_mul(u, G)
    left_vH = scalar_mul(v, H)
    left = add_points(left_uG, left_vH)

    # compute right = T + c*C
    cC = scalar_mul(c, C)
    right = add_points(T, cC)

    return left == right

# --- Relationship check (as before) ---
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

    print(" G:", G)
    print(" H:", H)

    r = secrets.randbelow(curve_order)
    m = secrets.randbelow(curve_order)

    C = pedersen_commit(m, r, G, H)
    print("\nCommitment C:", C)

    # context binds the proof to an application (e.g. "session id", "tx id", or IP)
    context = b"example-context-v1"
    print(type(context))
    proof = prove_pedersen_opening(C, m, r, G, H, context=context)
    print("*************************************")
    print(C, proof, G,H,context)
    print("*************************************")
    print("\nProof produced:")
    print(" proof:", proof)
    print(" c:", proof['c'])
    print(" T:", type(proof['T']))
    print(" u:", proof['u'])
    print(" v:", proof['v'])
    print(" h (hex):", proof['h'].hex())
    print(verify_pedersen_proof(C, proof, G, H, context=context))
    ok = verify_pedersen_proof(C, proof, G, H, context=context)
    print("\nVerification:", "✔️ OK" if ok else "❌ FAILED")
    assert ok
