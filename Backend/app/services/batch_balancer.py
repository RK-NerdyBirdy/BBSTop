"""Assign students to batches so every batch has (almost) the same size and average CGPA.

Pure functions, no database access, so they can be tested and previewed cheaply.

- fresh_assignment: shuffled snake draft, then pairwise swaps to close the remaining gap.
  Used when a semester is set up (nobody has seen their batch yet).
- rebalance_min_moves: keep the current batches and swap as few students as possible
  until the averages are within a target gap. Used once a semester is live.
"""

import random
from dataclasses import dataclass
from statistics import mean, median

TARGET_GAP = 0.05          # max-min batch average we consider "balanced"
MAX_IMPROVE_ROUNDS = 2000  # safety cap for the swap search


@dataclass(frozen=True)
class StudentCgpa:
    register_number: str
    cgpa: float


@dataclass
class BatchStats:
    batch_id: int
    size: int
    average: float
    median: float
    minimum: float
    maximum: float


def batch_stats(assignment: dict[str, int], cgpas: dict[str, float], batch_ids: list[int]) -> list[BatchStats]:
    stats = []
    for batch_id in batch_ids:
        values = [cgpas[reg] for reg, b in assignment.items() if b == batch_id]
        stats.append(BatchStats(
            batch_id=batch_id,
            size=len(values),
            average=round(mean(values), 3) if values else 0.0,
            median=round(median(values), 3) if values else 0.0,
            minimum=min(values) if values else 0.0,
            maximum=max(values) if values else 0.0,
        ))
    return stats


def average_gap(assignment: dict[str, int], cgpas: dict[str, float], batch_ids: list[int]) -> float:
    averages = [s.average for s in batch_stats(assignment, cgpas, batch_ids) if s.size]
    return round(max(averages) - min(averages), 3) if averages else 0.0


def _sums(assignment: dict[str, int], cgpas: dict[str, float], batch_ids: list[int]) -> tuple[dict[int, float], dict[int, int]]:
    sums = {b: 0.0 for b in batch_ids}
    counts = {b: 0 for b in batch_ids}
    for reg, b in assignment.items():
        sums[b] += cgpas[reg]
        counts[b] += 1
    return sums, counts


def _spread(sums: dict[int, float], counts: dict[int, int]) -> float:
    averages = [sums[b] / counts[b] for b in sums if counts[b]]
    return max(averages) - min(averages) if averages else 0.0


def _improve_by_swaps(
    assignment: dict[str, int],
    cgpas: dict[str, float],
    batch_ids: list[int],
    rng: random.Random,
    target_gap: float,
    max_swaps: int | None = None,
) -> int:
    """Swap students between the highest- and lowest-average batches while it narrows the gap.

    Swapping keeps batch sizes unchanged. Each step picks the single swap that reduces the
    spread the most (ties broken randomly). Returns the number of swaps made.
    """
    sums, counts = _sums(assignment, cgpas, batch_ids)
    swaps = 0
    for _ in range(MAX_IMPROVE_ROUNDS):
        spread = _spread(sums, counts)
        if spread <= target_gap or (max_swaps is not None and swaps >= max_swaps):
            break
        ordered = sorted((b for b in batch_ids if counts[b]), key=lambda b: sums[b] / counts[b])
        low, high = ordered[0], ordered[-1]
        high_members = [r for r, b in assignment.items() if b == high]
        low_members = [r for r, b in assignment.items() if b == low]
        rng.shuffle(high_members)
        rng.shuffle(low_members)

        best: tuple[float, str, str] | None = None
        for h in high_members:
            for l in low_members:
                diff = cgpas[h] - cgpas[l]
                if diff <= 0:
                    continue
                sums[high] -= diff
                sums[low] += diff
                new_spread = _spread(sums, counts)
                sums[high] += diff
                sums[low] -= diff
                if new_spread < spread - 1e-9 and (best is None or new_spread < best[0] - 1e-9):
                    best = (new_spread, h, l)
        if best is None:
            break
        _, h, l = best
        diff = cgpas[h] - cgpas[l]
        assignment[h], assignment[l] = low, high
        sums[high] -= diff
        sums[low] += diff
        swaps += 1
    return swaps


def fresh_assignment(
    students: list[StudentCgpa],
    batch_ids: list[int],
    seed: int,
    target_gap: float = TARGET_GAP,
) -> dict[str, int]:
    """Deal students into batches: equal sizes (within one), near-equal average and spread.

    Students are sorted by CGPA with random tie-breaking inside 0.1-wide bands, then dealt
    in a snake order (B1 B2 B3 B3 B2 B1 ...) with the batch order shuffled each round, so
    the result is balanced but not predictable. A swap pass then closes any remaining gap.
    """
    if not batch_ids:
        raise ValueError("The semester has no batches")
    rng = random.Random(seed)
    jitter = {s.register_number: rng.random() for s in students}
    ordered = sorted(students, key=lambda s: (-round(s.cgpa, 1), jitter[s.register_number]))

    assignment: dict[str, int] = {}
    k = len(batch_ids)
    for start in range(0, len(ordered), k):
        round_batches = list(batch_ids)
        rng.shuffle(round_batches)
        if (start // k) % 2:
            round_batches.reverse()
        for student, batch_id in zip(ordered[start:start + k], round_batches):
            assignment[student.register_number] = batch_id

    cgpas = {s.register_number: s.cgpa for s in students}
    _improve_by_swaps(assignment, cgpas, batch_ids, rng, target_gap)
    return assignment


def rebalance_min_moves(
    current: dict[str, int],
    cgpas: dict[str, float],
    batch_ids: list[int],
    seed: int,
    target_gap: float = TARGET_GAP,
    size_tolerance: int = 2,
) -> dict[str, int]:
    """Start from the current batches and change as few students as possible.

    First evens out sizes (moving students from the biggest to the smallest batch, picking
    the student whose move helps the averages most) until sizes differ by at most
    `size_tolerance`; then swaps pairs until the average gap is within `target_gap`.
    """
    if not batch_ids:
        raise ValueError("The semester has no batches")
    rng = random.Random(seed)
    assignment = dict(current)
    sums, counts = _sums(assignment, cgpas, batch_ids)

    for _ in range(len(assignment) + 1):
        biggest = max(batch_ids, key=lambda b: counts[b])
        smallest = min(batch_ids, key=lambda b: counts[b])
        if counts[biggest] - counts[smallest] <= size_tolerance:
            break
        # Move the student that leaves the two averages closest to the overall average
        overall = sum(cgpas.values()) / len(cgpas)
        candidates = [r for r, b in assignment.items() if b == biggest]

        def score(reg: str) -> float:
            v = cgpas[reg]
            a = (sums[biggest] - v) / max(counts[biggest] - 1, 1)
            c = (sums[smallest] + v) / (counts[smallest] + 1)
            return abs(a - overall) + abs(c - overall)

        reg = min(candidates, key=lambda r: (score(r), rng.random()))
        assignment[reg] = smallest
        sums[biggest] -= cgpas[reg]
        sums[smallest] += cgpas[reg]
        counts[biggest] -= 1
        counts[smallest] += 1

    _improve_by_swaps(assignment, cgpas, batch_ids, rng, target_gap)
    return assignment


def moved(before: dict[str, int], after: dict[str, int]) -> list[str]:
    return sorted(reg for reg, b in after.items() if before.get(reg) != b)
