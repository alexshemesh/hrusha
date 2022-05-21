import csv
from typing import List
from enum import Enum
from .coins import Coin, Pair


class StrategyOrderState(Enum):
    NEW = 'NEW'
    CANCELED = 'CANCELED'
    COMPLETE = 'COMPLETE'

    def __eq__(self, other):
        if self.__class__ is other.__class__:
            return self.value is other.value
        return NotImplemented


class StrategyOrderType(Enum):
    LIMIT_BUY = 1
    LIMIT_SELL = 2


class StrategyOrder():
    def __init__(self, pair: Pair = Pair.UNDEFINED, price: float = 0.0,
                 amount_to_use: float = 0.0, type: StrategyOrderType = StrategyOrderType.LIMIT_BUY) -> None:
        """Default contructor for orde

        Args:
            pair_symbol (str): string that represents trade pair (e.g. tOMGETH)
            price (float): price for pair ( how many of OMG you can buy for ETH)
            amount_to_use (floar) : how much to spend ( i want to spend 3 OMG to buy ETH at said price)
            type (StrategyOrderType): LIMIT_BUY or LIMIT_SELL
        """
        self.pair = pair
        self.price = price
        self.amount_to_use = amount_to_use
        self.type = type
        self.final_price = 0.0
        if pair != Pair.UNDEFINED:
            self.from_coin = get_from_coin(pair, type)
            self.to_coin = get_to_coin(pair, type)
        self.fee = 0.0
        self.fee_coin = Coin.UNDEFINED
        self.state = StrategyOrderState.NEW

    def _fields(self):
        return [self.pair, self.price,
                self.amount_to_use, self.type, self.state]

    def __eq__(self, other):
        retval = self.pair == other.pair and \
            self.price == other.price and \
            self.amount_to_use == other.amount_to_use and \
            self.type == other.type
        return retval

    def __ne__(self, other):
        result = not self.__eq__(other)
        return result


def save_orders_to_file(filename: str, orders: List[StrategyOrder]):
    with open(filename, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile, delimiter=' ')
        writer.writerow(orders[0].__dict__.keys())
        for order in orders:
            writer.writerow(order._fields())


def load_orders_from_file(filename: str) -> List[StrategyOrder]:
    retval = []

    with open(filename, 'r', newline='') as csvfile:
        reader = csv.reader(csvfile, delimiter=' ')
        keys = None
        for row in reader:
            if keys is None:
                keys = row
            else:
                order = StrategyOrder()
                k = 0
                for key in keys:
                    if k < len(row):
                        splited_values = row[k].split('.')
                        if splited_values[0] == 'Pair':
                            order.__dict__['pair'] = Pair[splited_values[1]]
                        elif splited_values[0] == 'StrategyOrderType':
                            order.__dict__[
                                'type'] = StrategyOrderType[splited_values[1]]
                        elif splited_values[0] == 'StrategyOrderState':
                            order.__dict__[
                                'state'] = StrategyOrderState[splited_values[1]]
                        elif splited_values[0] == 'Coin':
                            order.__dict__['coin'] = Coin[splited_values[1]]
                        else:
                            order.__dict__[key] = float(row[k])
                    else:
                        break
                    k = k + 1
                order.from_coin = get_from_coin(order.pair, order.type)
                order.to_coin = get_to_coin(order.pair, order.type)
                retval.append(order)
    return retval


def get_from_coin(pair: Pair, order_type: StrategyOrderType) -> Coin:
    retval = Coin.UNDEFINED
    if pair == Pair.OMGETH:
        if order_type == StrategyOrderType.LIMIT_SELL:
            retval = Coin.OMG
        elif order_type == StrategyOrderType.LIMIT_BUY:
            retval = Coin.ETH
    if retval == Coin.UNDEFINED:
        raise Exception('Unsupported pair {} at get_from_coin'.format(pair))
    return retval


def get_to_coin(pair: Pair, order_type: StrategyOrderType) -> str:
    retval = Coin.UNDEFINED
    if pair == Pair.OMGETH:
        if order_type == StrategyOrderType.LIMIT_SELL:
            retval = Coin.ETH
        elif order_type == StrategyOrderType.LIMIT_BUY:
            retval = Coin.OMG
    if retval == Coin.UNDEFINED:
        raise('Unsupported pair {} at get_to_coin'.format(pair))
    return retval
