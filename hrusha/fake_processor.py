from .strategy_order import StrategyOrderState, StrategyOrderType, load_orders_from_file, StrategyOrder, save_orders_to_file
from typing import List, Dict
from .coins import Coin
from .wallet import Wallet, load_wallets_from_file, save_wallet_to_file
from .ticker import Ticker


def load_wallets_to_dict(filename: str) -> Dict[Coin, Wallet]:
    retval = dict()
    wallets = load_wallets_from_file(filename)
    for w in wallets:
        retval[w.coin] = w
    return retval


def generate_pairs_from_orders(orders: List[StrategyOrder]) -> List[str]:
    list_of_pairs = []
    for order in orders:
        if order.pair not in list_of_pairs:
            list_of_pairs.append[order.pair]
    return list_of_pairs


def process_orders(ticker: Ticker, orders_file_name: str = 'current_orders.csv',
                   wallets_file_name: str = 'wallets.csv'):
    processed_orders = []
    wallets = load_wallets_from_file(wallets_file_name)
    orders = load_orders_from_file(orders_file_name)
    for order in orders:
        if order.pair == ticker.pair \
                and order.state == StrategyOrderState.NEW:

            result_order, wallets = execute_order(
                order=order, wallets=wallets, ticker=ticker, fee_percent=0.02)

            processed_orders.append(result_order)
        else:
            processed_orders.append(order)

    save_wallet_to_file(wallets_file_name, wallets=wallets)
    save_orders_to_file(orders_file_name, orders=processed_orders)


def make_dict_from_wallet_list(wallet_list: List[Wallet]) -> Dict[str, Wallet]:
    retval = {}
    for w in wallet_list:
        if w.coin in retval.keys():
            raise Exception(f'Wallet already present {w.coin}')
        retval[w.coin] = w
    return retval


def execute_order(order: StrategyOrder,
                  wallets: List[Wallet], ticker: Ticker, fee_percent: float):
    fee = fee_percent * order.amount_to_use
    order.fee = fee
    order.fee_coin = order.from_coin
    amount_to_use_after_fee = (order.amount_to_use - fee)
    wallet_dict = make_dict_from_wallet_list(wallet_list=wallets)
    from_wallet = wallet_dict[order.from_coin]
    to_wallet = wallet_dict[order.to_coin]
    if order.type == StrategyOrderType.LIMIT_BUY and ticker.price <= order.price:
        to_wallet.amount = to_wallet.amount + ticker.price * amount_to_use_after_fee
        from_wallet.amount = from_wallet.amount - order.amount_to_use
        order.state = StrategyOrderState.COMPLETE
    elif order.type == StrategyOrderType.LIMIT_SELL and ticker.price >= order.price:
        to_wallet.amount = to_wallet.amount + amount_to_use_after_fee / ticker.price
        from_wallet.amount = from_wallet.amount - order.amount_to_use
        order.state = StrategyOrderState.COMPLETE
    wallet_dict[order.to_coin] = to_wallet
    wallet_dict[order.from_coin] = from_wallet
    list_of_wallets = list(wallet_dict.values())
    return order, list_of_wallets
