import hashlib
import secrets


class PoUW:
    """
    Proof of Useful Work (PoUW) consensus.

    Reference: Li, G., Zhao, Q., Li, X. "A Decentralized Data Processing
    Framework Based on PoUW Blockchain" (BlockSys 2020).

    A worker that has just completed `m` units of useful work (measured
    as executed CPU instructions) draws a random nonce and hashes it
    together with m. The higher m is relative to the current difficulty
    d, the wider the winning threshold -- so doing more real work raises
    your odds of becoming scheduler, instead of wasting cycles on
    arbitrary hashing the way plain PoW does.

    NOTE: the paper requires m to be attested by a Trusted Execution
    Environment (e.g. Intel SGX) so a worker can't just lie about how
    much work it did. This implementation still self-reports m (see
    Worker.measure_cpu) -- that's a known simplification, not something
    fixed here.
    """

    DEFAULT_DIFFICULTY = 10_000_000

    @staticmethod
    def scheduler_election(m, difficulty=DEFAULT_DIFFICULTY):
        """
        Run one PoUW election attempt.

        Parameters
        ----------
        m : int
            Number of executed CPU instructions for the just-completed task.
        difficulty : int
            Current blockchain difficulty coefficient (d).

        Returns
        -------
        (win, proof) : (bool, dict | None)
            win   -> True if this worker becomes the new scheduler.
            proof -> PoUW credential others can verify, or None if m <= 0.
        """
        if m <= 0:
            return False, None

        nonce = secrets.randbits(64)
        message = f"{nonce}:{m}".encode()
        digest = hashlib.sha256(message).hexdigest()
        value = int(digest, 16)

        # More useful work (m) -> larger threshold -> higher win chance.
        threshold = (2 ** 256 * m) // difficulty
        win = value < threshold

        proof = {
            "nonce": nonce,
            "instruction": m,
            "difficulty": difficulty,
            "hash": digest,
        }
        return win, proof

    @staticmethod
    def verify(proof):
        """Verify a PoUW proof produced by scheduler_election."""
        if not proof:
            return False

        nonce = proof["nonce"]
        m = proof["instruction"]
        difficulty = proof["difficulty"]

        digest = hashlib.sha256(f"{nonce}:{m}".encode()).hexdigest()
        if digest != proof["hash"]:
            return False

        value = int(digest, 16)
        threshold = (2 ** 256 * m) // difficulty
        return value < threshold
