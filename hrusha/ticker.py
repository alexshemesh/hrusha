from .coins import Pair

class Ticker():
    def __init__(self, pair: Pair, price: float):
        self.pair = pair
        self.price = price