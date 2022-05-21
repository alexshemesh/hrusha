import os
from hrusha.coins import Pair
from hrusha.strategy import Strategy
from hrusha.strategy_order import load_orders_from_file, save_orders_to_file
from hrusha.ticker import Ticker
import sys
sys.path.append("..")


def compare_lists(list1: list, list2: list) -> bool:
    index = 0
    retval = True
    if len(list1) != len(list2):
        retval = False
    for i in range(0, len(list1)):
        if list1[i] != list2[i]:
            retval = False
            break
    # else:
    #     while index < len(list1):
    #         item_from_1 = list1[index]
    #         item_from_2 = list2[index]
    #         if not item_from_1 != item_from_2:
    #             retval = False
    #             break
    return retval


def test_strategy():
    strategy = Strategy('gradually buy than sell')

    orders = strategy.generate_orders_for_ticker(pair_ticker=Ticker(Pair.OMGETH, 0.003),
                                                 amount_to_use=100,
                                                 long_or_short=True,
                                                 target_price_range=0.0004)
    assert len(orders) > 0


def test_load_strategy():
    try:
        strategy = Strategy('gradually buy than sell')
        file_name = 'test_orders.csv'

        orders = strategy.generate_orders_for_ticker(pair_ticker=Ticker(Pair.OMGETH, 0.003),
                                                     amount_to_use=100,
                                                     long_or_short=True,
                                                     target_price_range=0.0004)
        save_orders_to_file(file_name, orders)
        orders_loaded = load_orders_from_file(file_name)

        assert compare_lists(orders, orders_loaded)
    finally:
        os.remove(file_name)
