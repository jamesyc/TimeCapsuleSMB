"""Hang guards for tests that wait across a product's real-time budgets."""


def hang_guard(*steps_ms):
    """Seconds to wait for an outcome that takes these steps (in ms) in turn.

    Pass every budget the wait crosses, including grace and retry steps, so
    raising one of them widens each guard that waits for it. The guard keeps
    5x their sum and at least 1.5 s over it: only a failing run reaches it.
    """
    expected = sum(steps_ms) / 1000
    return max(5 * expected, expected + 1.5)
