"""整数金额分配工具。

所有金额均为人民币分，业务计算全程使用整数，禁止用浮点数分摊金额。
约定余分按键的字典序（line_id / merchant_id）分配，与资料约定一致。
"""
from __future__ import annotations

from typing import Mapping, Sequence


def allocate_by_weight(amount: int, weights: Mapping[str, int]) -> dict[str, int]:
    """按正整数权重把 ``amount`` 分钱分摊到各键。

    * 只以正权重参与分配，零权重键得 0；
    * 先取整（截断），余分按“小数余量从大到小、平手按键名字典序”逐分分配；
    * 当所有精确份额的小数部分相同（例如等权）时，余分自然落到字典序靠前的键；
    * 权重合计为 0 时，任何正金额都无法归属，抛出 ``ValueError``。

    返回值之和恒等于 ``amount``（``amount`` 为负时不允许，业务不存在负金额池）。
    """
    if amount < 0:
        raise ValueError(f"待分摊金额不能为负: {amount}")
    positive = {k: w for k, w in weights.items() if w > 0}
    result = {k: 0 for k in weights}
    if amount == 0:
        return result
    total = sum(positive.values())
    if total == 0:
        raise ValueError("正金额无法按零权重分摊")
    floors = {k: amount * w // total for k, w in positive.items()}
    remainder = amount - sum(floors.values())
    # 余量 = amount*w - floor*total，越大越先拿余分；平手按键名字典序。
    order = sorted(positive, key=lambda k: (-(amount * positive[k] - floors[k] * total), k))
    for i in range(remainder):
        floors[order[i % len(order)]] += 1
    result.update(floors)
    return result


def allocate_pro_rata(amount: int, bases: Mapping[str, int]) -> dict[str, int]:
    """:func:`allocate_by_weight` 的语义别名，用于按基数比例扣减的场景。"""
    return allocate_by_weight(amount, bases)


def per_unit_split(line_amount: int, quantity: int, line_id: str) -> list[int]:
    """把一行的金额（如行折扣、行收入）拆到每件，余分归字典序靠前的第 0 件。

    退货按件回冲时使用，保证各件之和等于行金额，且不出现负成本。
    """
    if quantity <= 0:
        raise ValueError(f"数量必须为正: {line_id} quantity={quantity}")
    if line_amount < 0:
        raise ValueError(f"行金额不能为负: {line_id} amount={line_amount}")
    base, extra = divmod(line_amount, quantity)
    return [base + (1 if i < extra else 0) for i in range(quantity)]
