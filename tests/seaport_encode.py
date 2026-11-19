"""Test helper: ABI-encode a synthetic Seaport OrderFulfilled log."""
from sources.chain import ORDER_FULFILLED


def _w(v) -> bytes:
    if isinstance(v, str):
        return bytes.fromhex(v[2:].rjust(64, "0"))
    return int(v).to_bytes(32, "big")


def encode_log(seaport, order_hash, offerer, zone, recipient, offer, consideration, log_index=0):
    """offer: [(type, token, id, amount)], consideration: [(type, token, id, amount, recipient)]."""
    head_len = 4 * 32
    offer_blob = _w(len(offer)) + b"".join(_w(x) for it in offer for x in it)
    cons_blob = _w(len(consideration)) + b"".join(_w(x) for it in consideration for x in it)
    data = (_w(order_hash) + _w(recipient) + _w(head_len) + _w(head_len + len(offer_blob))
            + offer_blob + cons_blob)
    return {"address": seaport, "topics": [ORDER_FULFILLED, "0x" + _w(offerer).hex(), "0x" + _w(zone).hex()],
            "data": "0x" + data.hex(), "logIndex": hex(log_index)}
