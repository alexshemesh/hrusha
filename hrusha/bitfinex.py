
from bfxapi.rest.bfx_rest import BfxRest


class BitfinexClient():
    def __init__(self, api_key: str, api_secret: str):
        self.bfx_client = BfxRest(API_KEY=api_key, API_SECRET=api_secret)

    async def get_wallets(self):
        wallets = await self.bfx_client.get_wallets()
        return wallets

    async def get_orders(self, symbol: str):
        orders = await self.bfx_client.get_active_orders(symbol=symbol)
        return orders

    async def get_ticker(self, symbol: str):
        ticker = await self.bfx_client.get_public_ticker(symbol=symbol)
        return ticker
