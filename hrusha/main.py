import configparser
import asyncio
from bitfinex import BitfinexClient

cfg_file_name = '/home/stan/.hrusha/config.ini'


async def main():
    print('Starting')
    config = configparser.ConfigParser()
    res = config.read(cfg_file_name)
    if len(res) == 0:
        print('Failed to read config file', cfg_file_name)
        exit(1)
    print('Get wallets info')
    bfx_client = BitfinexClient(
        api_key=config['bitfinex']['API_KEY'],
        api_secret=config['bitfinex']['API_SECRET'])
    wallets = await bfx_client.get_wallets()

    for w in wallets:
        print('Wallet data:', w.balance, w.currency)

    ticker = await bfx_client.get_ticker(symbol='tOMGETH')
    print('Ticker:', ticker[0])

    orders = await bfx_client.get_orders(symbol='tOMGETH')

    for o in orders:
        print('Order data:', o.cid, o.price, o.status, o.tag)

if __name__ == '__main__':
    asyncio.run(main())
