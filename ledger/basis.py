"""Cost basis and proceeds math, in integer wei."""


def basis(price_wei: int | None, gas_wei: int) -> int | None:
    """basis(lot) = price paid (ETH or WETH) + gas on the acquiring transaction. None if unpriced."""
    if price_wei is None:
        return None
    return price_wei + gas_wei


def proceeds(price_wei: int | None, fee_wei: int, royalty_wei: int, gas_wei: int) -> int | None:
    """proceeds(sale) = sale price - marketplace fee - creator royalty - gas the wallet paid."""
    if price_wei is None:
        return None
    return price_wei - fee_wei - royalty_wei - gas_wei
