import secrets
from typing import Tuple, Dict
from py_ecc import bls12_381 as b381
from py_ecc.bls.hash_to_curve import hash_to_G1
from py_ecc.optimized_bls12_381 import normalize
import hashlib
import ipaddress


class IPRrelationProof:
    """
    Implements an IP-bound zero-knowledge relation proof using Pedersen-style
    commitments over BLS12-381 G1.

    Mathematical construction:

        C_node     = n·G + r_n·H_node
        C_producer = n·G + r_p·H_producer

    Where:
        H_node     = HashToCurve( SHA256(h || node_ip) )
        H_producer = HashToCurve( SHA256(h || producer_ip) )

    Proof:

        T  = k1·H_node + k2·H_producer
        h  = Hash(T || C_node || C_producer || context)
        u1 = k1 + h·r_n
        u2 = k2 - h·r_p

    Verification equation:

        u1·H_node + u2·H_producer
            ?= T + h·(C_node - C_producer)

    Security assumptions:
        - Discrete logarithm hardness in BLS12-381
        - Random oracle model (Fiat–Shamir)
    """

    curve_order = b381.curve_order
    field_modulus = b381.field_modulus
    COORD_LEN = (field_modulus.bit_length() + 7) // 8

    DST_G = b"GENERATOR_G_v1"
    DST_IP_BASED = b"IP_BASED_GENERATOR_v1"

    # ---------------------------
    # Utility Methods
    # ---------------------------

    @staticmethod
    def int_to_bytes(x: int) -> bytes:
        """Convert non-negative integer to big-endian byte representation."""
        if x < 0:
            raise ValueError("Must be non-negative")
        length = (x.bit_length() + 7) // 8 or 1
        return x.to_bytes(length, "big")

    @staticmethod
    def ipv4_to_bytes(ip_str: str) -> bytes:
        """Convert IPv4 string into packed 4-byte representation."""
        return ipaddress.IPv4Address(ip_str).packed

    @classmethod
    def hash_to_curve_point(cls, msg: bytes, dst: bytes) -> Tuple[int, int]:
        """Map arbitrary bytes deterministically to BLS12-381 G1."""
        P = hash_to_G1(msg, dst, hashlib.sha256)
        P_affine = normalize(P)
        x, y = map(int, P_affine)
        return (x, y)

    @classmethod
    def derive_rp(cls, aSecret: str, order: int) -> int:
        """ Derive r_p as an integer (same type as r_n) from string input"""
        aSecretBytes = (str(aSecret)).encode('utf-8')# convert to bytes
        digest = hashlib.sha256(aSecretBytes).digest()# hash it
        r_p = int.from_bytes(digest, 'big') % order# convert to integer modulo curve order
        return r_p
    # ---------------------------
    # Generator Derivation
    # ---------------------------

    @classmethod
    def derive_G(cls, seed: bytes) -> Tuple[int, int]:
        """Derive base generator G from seed."""
        return cls.hash_to_curve_point(seed, cls.DST_G)

    @classmethod
    def derive_ip_generator(cls, h: bytes, ip: str) -> Tuple[int, int]:
        """
        Derive IP-bound generator:

            H_ip = HashToCurve( SHA256(h || ip) )
        """
        ip_bytes = cls.ipv4_to_bytes(ip)
        inner_hash = hashlib.sha256(h + ip_bytes).digest()
        return cls.hash_to_curve_point(inner_hash, cls.DST_IP_BASED)

    # ---------------------------
    # EC Arithmetic
    # ---------------------------

    @classmethod
    def point_from_int_tuple(cls, pt: Tuple[int, int]):
        return (b381.FQ(pt[0]), b381.FQ(pt[1]))

    @classmethod
    def to_affine(cls, pt):
        if len(pt) == 3:
            return b381.normalize(pt)
        return pt

    @classmethod
    def add_points(cls, A, B):
        P, Q = cls.point_from_int_tuple(A), cls.point_from_int_tuple(B)
        R = b381.add(P, Q)
        R = cls.to_affine(R)
        return (int(R[0]), int(R[1]))

    @classmethod
    def negate_point(cls, P):
        P_fq = cls.point_from_int_tuple(P)
        neg = b381.neg(P_fq)
        neg = cls.to_affine(neg)
        return (int(neg[0]), int(neg[1]))

    @classmethod
    def sub_points(cls, A, B):
        return cls.add_points(A, cls.negate_point(B))

    @classmethod
    def scalar_mul(cls, k: int, P):
        P = cls.point_from_int_tuple(P)
        R = b381.multiply(P, k % cls.curve_order)
        R = cls.to_affine(R)
        return (int(R[0]), int(R[1]))

    # ---------------------------
    # Commitments
    # ---------------------------

    @classmethod
    def pedersen_commit(cls, m: int, r: int, G, H):
        """Compute C = m·G + r·H."""
        return cls.add_points(
            cls.scalar_mul(m, G),
            cls.scalar_mul(r, H)
        )

    # ---------------------------
    # Fiat-Shamir
    # ---------------------------

    @classmethod
    def point_to_bytes(cls, P: Tuple[int, int]) -> bytes:
        x_bytes = cls.int_to_bytes(P[0]).rjust(cls.COORD_LEN, b'\x00')
        y_bytes = cls.int_to_bytes(P[1]).rjust(cls.COORD_LEN, b'\x00')
        return x_bytes + y_bytes

    @classmethod
    def hash_to_int_mod_q(cls, *parts: bytes) -> int:
        h = hashlib.sha256()
        for p in parts:
            h.update(p)
        return int.from_bytes(h.digest(), "big") % cls.curve_order

    # ---------------------------
    # Prover
    # ---------------------------

    @classmethod
    def prove_relation(
        cls,
        C_node,
        C_producer,
        r_n: int,
        r_p: int,
        H_node,
        H_producer,
        context: bytes = b"",
    ) -> Dict:

        k1 = secrets.randbelow(cls.curve_order)
        k2 = secrets.randbelow(cls.curve_order)

        T = cls.add_points(
            cls.scalar_mul(k1, H_node),
            cls.scalar_mul(k2, H_producer)
        )

        h = cls.hash_to_int_mod_q(
            cls.point_to_bytes(T),
            cls.point_to_bytes(C_node),
            cls.point_to_bytes(C_producer),
            context,
        )

        u1 = (k1 + h * r_n) % cls.curve_order
        u2 = (k2 - h * r_p) % cls.curve_order

        return {
            "C_node": C_node,
            "C_producer": C_producer,
            "T": T,
            "u1": u1,
            "u2": u2,
        }

    # ---------------------------
    # Verifier
    # ---------------------------

    @classmethod
    def verify_relation(
        cls,
        proof,
        H_node,
        H_producer,
        context: bytes = b"",
    ) -> bool:

        C_node = proof["C_node"]
        C_producer = proof["C_producer"]
        T = proof["T"]
        u1 = proof["u1"]
        u2 = proof["u2"]

        h = cls.hash_to_int_mod_q(
            cls.point_to_bytes(T),
            cls.point_to_bytes(C_node),
            cls.point_to_bytes(C_producer),
            context,
        )

        left = cls.add_points(
            cls.scalar_mul(u1, H_node),
            cls.scalar_mul(u2, H_producer),
        )

        delta = cls.sub_points(C_node, C_producer)
        right = cls.add_points(T, cls.scalar_mul(h, delta))

        return left == right


# ============================================================
# Example Usage
# ============================================================
if __name__ == "__main__":

    # print("\n--- Example Usage ---\n")

    # Global randomness / epoch seed
    h_global = hashlib.sha256(b"network-epoch-seed").digest()

    # Example IP addresses
    node_ip = "192.168.1.10"
    producer_ip = "192.168.1.20"

    # Derive generators
    G = IPRrelationProof.derive_G(b"base-generator")
    H_node = IPRrelationProof.derive_ip_generator(h_global, node_ip)
    H_producer = IPRrelationProof.derive_ip_generator(h_global, producer_ip)

    # Shared secret value
    n = 100
    #secrets.randbelow(IPRrelationProof.curve_order)


    # Random blindings
    r_n = secrets.randbelow(IPRrelationProof.curve_order)
    r_p = IPRrelationProof.derive_rp("aSecret",IPRrelationProof.curve_order)

    # Commitments
    C_node = IPRrelationProof.pedersen_commit(n, r_n, G, H_node)
    C_producer = IPRrelationProof.pedersen_commit(n, r_p, G, H_producer)

    print("C_node:", C_node)
    print("C_producer:", C_producer)

    # Produce proof
    context = b"relation-proof-demo"
    proof = IPRrelationProof.prove_relation(
        C_node,
        C_producer,
        r_n,
        r_p,
        H_node,
        H_producer,
        context,
    )

    print("\nProof generated.")

    # Verify proof
    valid = IPRrelationProof.verify_relation(
        proof,
        H_node,
        H_producer,
        context,
    )

    print("\nProof generated.")


    # proof = IPRrelationProof.prove_relation(
    #     (2008145610345825103772889288762494952368432566537427005876418752888206851589936890482033717740277080084577239454864, 2612625629381012872353373651334117995111340318352361803372367246025038160902986081034349554899286854993329969336692),
    #     (3187441621192748514297089007696262086845214572049298571732151575365371237783693819328423566071512444314340665313650, 500611421832895735384120514218390203595291313411186642716317828504734046147091452005463134940780422450000036656736),
    #     18363287015719118061660789571954264184193702835732645834532233434536998124760,
    #     38720307207599648528211736436817930416103789439318178660273974026535871438845,
    #     (2386224415176856965938434892349474658136728601373562910890330182075805184345862845265693395860824451288879573616276, 1432853465640388703952532600299528823624688696061215285689248286775759701187314333540133804628357496700458247552006),
    #     (1941954721032290107427188404998889939815201685994199712031898339143367274737296705601255058511967243102822557636561, 34053634233632108024844419208985464794048917964148810427976491035142651065128988522452477358087588404087416851184),
    #     b'192.168.56.12',
    # )

    # # Verify proof
    # valid = IPRrelationProof.verify_relation(
    #     proof,
    #     (2386224415176856965938434892349474658136728601373562910890330182075805184345862845265693395860824451288879573616276, 1432853465640388703952532600299528823624688696061215285689248286775759701187314333540133804628357496700458247552006),
    #     (1941954721032290107427188404998889939815201685994199712031898339143367274737296705601255058511967243102822557636561, 34053634233632108024844419208985464794048917964148810427976491035142651065128988522452477358087588404087416851184),
    #     b'192.168.56.12',
    # )
    print("Verification result:", "✔ VALID" if valid else "❌ INVALID")