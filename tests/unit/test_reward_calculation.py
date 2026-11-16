from decimal import Decimal

import pytest

from app.modules.referrals.models import RewardType
from app.modules.rewards.service import RewardAmountOverflow, calculate_reward


@pytest.mark.parametrize(
    "kind,value,cap,payment,balance,expected",
    [
        ("percentage", "10", None, "100", "0.05", "0.01"),
        ("percentage", "10", "5", "100", "90", "5.00"),
        ("percentage", "10", None, "100", "120", "12.00"),
        ("fixed", "10", None, "3", "1", "3.33"),
        ("fixed", "10", None, "3", "2", "6.67"),
        ("fixed", "150", "100", "1000", "500", "50.00"),
        ("fixed", "10", None, "100", "150", "10.00"),
        ("percentage", "10", None, "100", "0", "0.00"),
    ],
)
def test_decimal_calculation(kind, value, cap, payment, balance, expected):
    assert calculate_reward(
        RewardType(kind),
        Decimal(value),
        Decimal(cap) if cap else None,
        Decimal(payment),
        Decimal(balance),
    ) == Decimal(expected)


def test_overflow_is_explicit():
    with pytest.raises(RewardAmountOverflow):
        calculate_reward(
            RewardType.PERCENTAGE,
            Decimal(100),
            None,
            Decimal("999999999999.99"),
            Decimal("1000000000000"),
        )
