"""Choose bounded, distributed snippets for identity review and quality checks."""


def review_windows(segments: list[dict], maximum: int = 3) -> list[tuple[float, float]]:
    """Sample different turns or positions without double-counting the same audio."""
    windows = []
    for row in segments:
        start, end = float(row["start"]), float(row["end"])
        duration = end - start
        if duration <= 0:
            continue
        if duration <= 12:
            windows.append((start, end))
        else:
            # A long VAD region may contain unseparated turns; test its interior too.
            count = min(3, max(2, int(duration / 6)))
            width = min(8, duration / count)
            for index in range(count):
                left = start + (duration - width) * index / (count - 1)
                windows.append((left, left + width))
    windows.sort(key=lambda item: item[1] - item[0], reverse=True)
    return sorted(windows[:maximum])
