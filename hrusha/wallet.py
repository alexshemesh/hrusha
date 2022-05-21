import csv
from typing import List
from .coins import Coin


class Wallet():
    def __init__(self, coin: Coin, amount: float) -> None:
        self.coin = coin
        self.amount = amount

    def _fields(self):
        return [self.coin, self.amount]


def save_wallet_to_file(filename: str, wallets: List[Wallet]):
    with open(filename, 'w', newline='') as csvfile:
        writer = csv.writer(csvfile, delimiter=' ')
        first_wallet = wallets[0]
        writer.writerow(first_wallet.__dict__.keys())
        for wallet in wallets:
            writer.writerow(wallet._fields())


def load_wallets_from_file(filename: str) -> List[Wallet]:
    retval = []

    with open(filename, 'r', newline='') as csvfile:
        reader = csv.reader(csvfile, delimiter=' ')
        keys = None
        for row in reader:
            if keys is None:
                keys = row
            else:
                wallet = Wallet(Coin.UNDEFINED, 0.0)
                k = 0
                for key in keys:
                    splited_values = row[k].split('.')
                    if splited_values[0] == 'Coin':
                        wallet.__dict__[key] = Coin[splited_values[1]]
                    else:
                        wallet.__dict__[key] = float(row[k])
                    k = k + 1
                retval.append(wallet)
    return retval
