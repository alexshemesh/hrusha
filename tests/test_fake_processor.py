from hrusha.strategy_order import StrategyOrder, StrategyOrderState,StrategyOrderType
from hrusha.fake_processor import execute_order,make_dict_from_wallet_list,process_orders
from hrusha.ticker import Ticker
from hrusha.wallet import Wallet
from hrusha.coins import Coin, Pair
import sys
from hrusha.strategy import Strategy
from hrusha.strategy_order import load_orders_from_file, save_orders_to_file
from hrusha.wallet import load_wallets_from_file, save_wallet_to_file
import os
sys.path.append("..")


def test_strategy_order_state():
    order_state = StrategyOrderState.NEW

    assert order_state == StrategyOrderState.NEW
    assert order_state != StrategyOrderState.CANCELED

def test_strategy_order():
    order = StrategyOrder(pair = Pair.OMGETH, 
                          price=10.0,
                          amount_to_use=10, 
                          type=StrategyOrderType.LIMIT_BUY)
    assert order.state == StrategyOrderState.NEW

def test_make_dict_from_wallet_list():
    walletETH = Wallet(coin=Coin.ETH,amount=11)
    walletOMG = Wallet(coin=Coin.OMG,amount=0)
    wallet_dict = make_dict_from_wallet_list(wallet_list=[walletETH,walletOMG])
    assert len(wallet_dict.keys()) == 2
    assert wallet_dict[Coin.ETH].amount == 11
    assert wallet_dict[Coin.OMG].amount == 0

def test_execute_order_limit_buy():
    order = StrategyOrder(pair = Pair.OMGETH, 
                          price=10.0,
                          amount_to_use=10, 
                          type=StrategyOrderType.LIMIT_BUY)
    ticker = Ticker(pair=Pair.OMGETH,price=10.0)
    walletETH = Wallet(coin=Coin.ETH,amount=11)
    walletOMG = Wallet(coin=Coin.OMG,amount=0)

    result_order, result_wallets = execute_order(order=order,wallets=[walletOMG,walletETH],ticker=ticker, fee_percent=0.002)

    assert result_order.state == StrategyOrderState.COMPLETE
    wallets = make_dict_from_wallet_list(result_wallets)
    assert wallets[Coin.ETH].amount == 1.0
    assert round(float(wallets[Coin.OMG].amount),2)  == 99.8

def test_process_orders_success():
    try:
        strategy = Strategy('gradually buy than sell')
        orders_filename = 'test_orders.csv'
        wallets_filename = 'test_wallets.csv'

        orders = strategy.generate_orders_for_ticker(pair_ticker=Ticker(Pair.OMGETH, 0.003),
                                                     amount_to_use=100,
                                                     long_or_short=True,
                                                     target_price_range=0.0004)
        save_orders_to_file(orders_filename, orders)
        walletETH = Wallet(coin=Coin.ETH,amount=0)
        walletOMG = Wallet(coin=Coin.OMG,amount=100)        

        save_wallet_to_file(filename=wallets_filename, wallets=[walletETH, walletOMG])

        current_ticker = Ticker(Pair.OMGETH, 0.00309 )
        process_orders(ticker=current_ticker, orders_file_name=orders_filename, wallets_file_name=wallets_filename)
        changed_orders = load_orders_from_file(filename=orders_filename)
        assert len(changed_orders) == 11
    finally:
        os.remove(wallets_filename)
        os.remove(orders_filename)


def test_process_orders_end_sell():
    try:
        strategy = Strategy('gradually buy than sell')
        orders_filename = 'test_orders.csv'
        wallets_filename = 'test_wallets.csv'

        orders = strategy.generate_orders_for_ticker(pair_ticker=Ticker(Pair.OMGETH, 0.003),
                                                     amount_to_use=100,
                                                     long_or_short=True,
                                                     target_price_range=0.0004)
        save_orders_to_file(orders_filename, orders)
        walletETH = Wallet(coin=Coin.ETH,amount=0)
        walletOMG = Wallet(coin=Coin.OMG,amount=100)        

        save_wallet_to_file(filename=wallets_filename, wallets=[walletETH, walletOMG])

        current_ticker = Ticker(Pair.OMGETH, 0.00309 )
        process_orders(ticker=current_ticker, orders_file_name=orders_filename, wallets_file_name=wallets_filename)
        current_ticker = Ticker(Pair.OMGETH, 0.00303 )
        process_orders(ticker=current_ticker, orders_file_name=orders_filename, wallets_file_name=wallets_filename)
        changed_orders = load_orders_from_file(filename=orders_filename)
        assert len(changed_orders) == 11
    finally:
        os.remove(wallets_filename)
        os.remove(orders_filename)