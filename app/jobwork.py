"""Real CPU busy-work payload for a submitted batch Job.

This is what each demo Job actually *runs* — NOT a sleep. It spins a tight
integer compute loop (counting primes via trial division and folding them into a
rolling hash) until a wall-clock deadline, so the pod genuinely consumes the CPU
it requested for the chosen duration. Kueue admits/preempts the Workload that
wraps this pod; preemption shows up here as the process receiving SIGTERM and the
pod terminating before it reaches its deadline.

Env (set by the controller on the Job spec):
  JOB_DURATION_SECONDS  wall-clock budget for the compute loop (default 60)

The pod's node and start time are read by the controller from the Kubernetes API
(downward API not required), so this payload only needs to burn CPU and exit 0
when it finishes its budget.
"""

from __future__ import annotations

import os
import signal
import sys
import time


def _is_prime(n: int) -> bool:
    if n < 2:
        return False
    if n % 2 == 0:
        return n == 2
    i = 3
    while i * i <= n:
        if n % i == 0:
            return False
        i += 2
    return True


def main() -> int:
    duration = float(os.environ.get("JOB_DURATION_SECONDS", "60"))
    deadline = time.monotonic() + duration

    # Exit promptly and cleanly when Kueue preempts us (pod gets SIGTERM).
    def _on_term(_signum, _frame):
        elapsed = duration - max(0.0, deadline - time.monotonic())
        print(f"[jobwork] preempted (SIGTERM) after ~{elapsed:.1f}s", flush=True)
        sys.exit(0)

    signal.signal(signal.SIGTERM, _on_term)

    print(f"[jobwork] starting busy-compute for {duration:.0f}s", flush=True)
    candidate = 2
    primes = 0
    rolling_hash = 1469598103934665603  # FNV-1a offset basis
    last_log = time.monotonic()

    while time.monotonic() < deadline:
        # A bounded chunk of real arithmetic between deadline checks so SIGTERM is
        # handled responsively (we are not blocked in a syscall).
        for _ in range(2000):
            if _is_prime(candidate):
                primes += 1
                rolling_hash = (rolling_hash ^ candidate) * 1099511628211 & 0xFFFFFFFFFFFFFFFF
            candidate += 1
        now = time.monotonic()
        if now - last_log >= 10:
            print(f"[jobwork] {primes} primes, hash={rolling_hash:016x}", flush=True)
            last_log = now

    print(
        f"[jobwork] done: {primes} primes up to {candidate}, hash={rolling_hash:016x}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
