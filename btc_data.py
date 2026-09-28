import math
import time
from collections import deque


class BRTITracker:
    """
    Kalshi의 CF Benchmarks passthrough를 사용해
    BRTI(CME CF Bitcoin Real Time Index)를 추적합니다.
    """

    def __init__(self, history_seconds=120):
        self.history_seconds = history_seconds
        self.history = deque()

    def get_current_price(self, client):
        response = client.request(
            "GET",
            "/cfbenchmarks/latest_values",
            params={
                "id": "BRTI",
                "maxResolution": "PER_SECOND",
            },
        )

        try:
            item = (
                response["data"]
                ["payload"]
                ["latest_values"]
                ["BRTI"]
            )

            price = float(item["value"])
            source_timestamp_ms = int(item["time"])

        except (KeyError, TypeError, ValueError) as exc:
            raise RuntimeError(
                f"BRTI 응답을 해석하지 못했습니다: {response}"
            ) from exc

        now = time.time()

        self.history.append(
            (
                now,
                price,
                source_timestamp_ms,
            )
        )

        self._trim(now)

        return price

    def _trim(self, now):
        cutoff = now - self.history_seconds

        while (
            self.history
            and self.history[0][0] < cutoff
        ):
            self.history.popleft()

    def sample_count(self):
        return len(self.history)

    def get_price_change(self):
        if len(self.history) < 2:
            return None

        first = self.history[0][1]
        current = self.history[-1][1]

        return (current / first) - 1.0

    def get_volatility_per_sqrt_second(self):
        """
        로그수익률을 이용해 초 단위 변동성을 추정합니다.

        strategy.py에서 남은 시간의 변동성을
        sigma * sqrt(seconds_remaining)
        형태로 확장합니다.
        """

        if len(self.history) < 8:
            return None

        normalized_returns = []

        items = list(self.history)

        for i in range(1, len(items)):
            t1, p1, _ = items[i - 1]
            t2, p2, _ = items[i]

            dt = t2 - t1

            if (
                dt <= 0
                or p1 <= 0
                or p2 <= 0
            ):
                continue

            r = math.log(
                p2 / p1
            )

            normalized_returns.append(
                r / math.sqrt(dt)
            )

        if len(normalized_returns) < 5:
            return None

        mean = (
            sum(normalized_returns)
            / len(normalized_returns)
        )

        variance = sum(
            (x - mean) ** 2
            for x in normalized_returns
        ) / (
            len(normalized_returns) - 1
        )

        return math.sqrt(variance)