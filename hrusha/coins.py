from enum import Enum


class Coin(Enum):
    OMG = 'OMG'
    ETH = 'ETH'
    USD = 'USD'
    UNDEFINED = 'UNDEFINED'

    def __eq__(self, other):
        if self.__class__ is other.__class__:
            return self.value is other.value
        return NotImplemented

    def __hash__(self):
        return hash(self.value)

    def __ne__(self, other):
        return not (self is other)


class Pair(Enum):
    UNDEFINED = 'UNDEFINED'
    OMGETH = 'tOMGETH'
    ETHUSD = 'tETHUSD'

    def __eq__(self, other):
        if self.__class__ is other.__class__:
            return self.value is other.value
        return NotImplemented

    def __hash__(self):
        return hash(self.value)

    def __ne__(self, other):
        return not (self is other)
