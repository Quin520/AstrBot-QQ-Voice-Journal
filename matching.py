"""Match reviewed voice exemplars without treating predictions as training data."""

import math
from collections import defaultdict


def unit_vector(values: list[float]) -> list[float]:
    """Validate and normalize one embedding.

    Raises:
        ValueError: The vector is empty, non-finite, or has zero magnitude.
    """
    if not values or len(values) > 4096:
        raise ValueError("Invalid embedding size")
    vector = [float(value) for value in values]
    if not all(math.isfinite(value) for value in vector):
        raise ValueError("Non-finite embedding")
    norm = math.sqrt(sum(value * value for value in vector))
    if norm < 1e-8:
        raise ValueError("Empty embedding")
    return [value / norm for value in vector]


def candidates(
    embedding: list[float],
    examples: list[dict],
    model: str,
    threshold: float = 0.72,
    margin: float = 0.1,
) -> dict:
    """Rank identities from approved examples of the same embedding model.

    Returns:
        Candidate identities and a conservative suggestion, never an approval.
    """
    query = unit_vector(embedding)
    people = defaultdict(list)
    for example in examples:
        if example["model"] != model:
            continue
        vector = unit_vector(example["embedding"])
        if len(vector) != len(query):
            continue
        similarity = sum(a * b for a, b in zip(query, vector))
        people[example["qq"]].append((similarity, example["nickname"]))
    ranked = []
    for qq, matches in people.items():
        best = sorted(matches, reverse=True)[:3]
        # Multiple exemplars cover different microphones; a lone outlier is damped.
        score = sum(item[0] for item in best) / len(best)
        ranked.append({"qq": qq, "nickname": best[0][1], "score": round(score, 4)})
    ranked.sort(key=lambda row: row["score"], reverse=True)
    gap = ranked[0]["score"] - ranked[1]["score"] if len(ranked) > 1 else 1.0
    suggested = bool(ranked and ranked[0]["score"] >= threshold and gap >= margin)
    return {"ranked": ranked[:3], "suggested": suggested, "margin": round(gap, 4)}
