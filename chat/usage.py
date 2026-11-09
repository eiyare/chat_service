"""Chat token usage against a workspace's token allowance."""
from dataclasses import dataclass
from datetime import datetime

from django.db.models import Sum
from django.db.models.functions import Coalesce

from .models import Message


@dataclass
class Usage:
    used: int
    allowance: int | None  # None = unlimited
    period: str
    period_start: datetime
    resets_at: datetime

    @property
    def remaining(self) -> int | None:
        return None if self.allowance is None else max(0, self.allowance - self.used)

    @property
    def exhausted(self) -> bool:
        return self.allowance is not None and self.used >= self.allowance

    @property
    def share(self) -> float | None:
        return None if not self.allowance else min(1.0, self.used / self.allowance)


async def tokens_used(tenant, since) -> int:
    totals = await Message.objects.filter(
        conversation__tenant=tenant, role=Message.Role.ASSISTANT, created_at__gte=since,
    ).aaggregate(
        prompt=Coalesce(Sum("prompt_tokens"), 0), completion=Coalesce(Sum("completion_tokens"), 0),
    )
    return totals["prompt"] + totals["completion"]


async def current(tenant, now=None) -> Usage:
    policy = tenant.token_policy
    start, reset = policy.period_bounds(now)
    return Usage(
        used=await tokens_used(tenant, start), allowance=policy.allowance, period=policy.period,
        period_start=start, resets_at=reset,
    )
