
from .ticker import Ticker
from .strategy_order import StrategyOrder, StrategyOrderType


class Strategy():
    def __init__(self, name) -> None:
        self.name = name

    def generate_orders_for_ticker(self, pair_ticker: Ticker, amount_to_use: float,
                                   long_or_short: bool, target_price_range: float) -> list:
        num_of_steps = 10
        step = target_price_range / num_of_steps
        start = pair_ticker.price
        end = pair_ticker.price + target_price_range
        retval = []
        projected_sum = 0.0
        while start < end:
            start = start + step
            ammount_for_order = amount_to_use / num_of_steps
            order = StrategyOrder(
                pair_ticker.pair,
                start,
                ammount_for_order,
                StrategyOrderType.LIMIT_SELL)
            projected_sum = projected_sum + ammount_for_order * start
            retval.append(order)
        final_order = StrategyOrder(
            pair_ticker.pair,
            pair_ticker.price + step,
            projected_sum,
            StrategyOrderType.LIMIT_BUY)
        retval.append(final_order)
        return retval
