# leakreport

How much has your NFT trading quietly cost you?

`leak <address|ens.eth>` rebuilds one Ethereum wallet's NFT history from OpenSea events and Seaport
receipts, then prices six behavioural leaks in ETH and USD: fees you didn't have to pay, holding past the
floor, buying at the top, gas burned on listing churn, selling into bids, and dead inventory. You get a
0-100 score, a JSON report (with a schema) and a 1200x630 card to share.

Read-only. No private keys, no signing, no wallet connection. Every formula and judgment call is written
down below, so you can argue with the number.

```
leak <address|ens.eth> [--since 2022-06-14] [--refresh] [--benchmark-fee 0.005]
     [--json report.json] [--card card.png] [--linked 0xabc,0xdef] [--cache cache.sqlite]
```

## Run it

You need Python 3.12, [uv](https://docs.astral.sh/uv/), a free OpenSea API key and an Ethereum RPC URL
(Alchemy, Infura or similar). Put `OPENSEA_API_KEY` and `ETH_RPC_URL` in an env file in the repo root
(gitignored, loaded with python-dotenv).

`--refresh` is the only path that touches the network. Without it the report is computed from
`cache.sqlite` alone, so the first run for a wallet needs `--refresh`:

```
uv sync
uv run leak <address|ens.eth> --refresh                  # fetch into cache.sqlite
uv run leak <address|ens.eth> --json out/report.json --card out/card.png
uv run pytest
```

Keys are never printed, logged, or written to the cache or to fixtures. `cache.sqlite` holds the full
history of every wallet you ran, so don't commit or share it. Pass your other wallets with `--linked` so
transfers between them keep their cost basis and aren't scored as trades.

Data comes from OpenSea's API, which requires attribution and limits redistribution: the card credits
OpenSea, and the report is meant for showing a wallet owner their own history.

## Leak definitions

Everything is computed per lot (one token, or one FIFO unit of an ERC-1155), then summed per collection
and per wallet. All amounts in ETH, converted to USD at the event's daily close.

- `basis(lot)` = price paid (ETH or WETH) + gas on the acquiring transaction. Mints: mint price + gas.
  Transfers in with no payment: basis 0, flagged `gift_or_airdrop`.
- `proceeds(sale)` = sale price − marketplace fee − creator royalty − gas the wallet paid on that
  transaction (non-zero when the wallet accepted a bid). Fee and royalty come from the Seaport
  `OrderFulfilled` consideration items.
- `floor(c, t)` = collection floor at day `t`; `median30(c, t)` = median floor over the 30 days before `t`.

| # | Leak | Formula (per lot or sale, summed) | Weight |
| --- | --- | --- | --- |
| 1 | Fee and royalty drag | `fee_total` = Σ marketplace fees; `royalty_total` = Σ royalties; `avoidable` = Σ max(0, fee − benchmark_rate × price), benchmark 0.5% by default, only for sales on or after 2022-10-19 (Blur's launch). Royalties are reported, never counted avoidable | 1.0 on `avoidable` |
| 2 | Held past the floor | For each lot whose floor fell below 90% of basis while held: `exit` = floor on the first day of that breach; `outcome` = sale proceeds, or current floor if still held; leak = max(0, exit − outcome) | 0.5 |
| 3 | Bought at the top | Per purchase: max(0, price − median30 at purchase). `floor_90d_later / price` reported as context | 0.5 |
| 4 | Listing churn | Σ gas on transactions to Seaport, Blur or WETH contracts that are not fulfilments: approvals, cancels, failed transactions | 1.0 |
| 5 | Sold into bids | For sales where the wallet accepted an offer: max(0, floor on that day − sale price) | 0.5 |
| 6 | Dead inventory | Lots held where the collection has had no sale in 90 days or floor is 0: leak = basis | 0.5 |
| F | Concentration | Share of current floor value in the top collection and top three; flag only | none |

Score: `weighted = Σ weight_i × leak_i`, `deployed = Σ basis` over lots with non-zero basis that did not exit unpriced,
`score = round(100 × (1 − min(1, weighted / deployed)))`. Fewer than 10 acquisitions sets `low_sample`.

Edge rules: wash and self-trades (counterparty is the wallet or a `--linked` wallet) are excluded from
every leak. A transfer out with no matching OpenSea sale is `disposed_unpriced`: it closes the lot, is
excluded from every leak and counted in `quality.unpriced_disposals`. Bundles split price and fees pro rata
by each item's floor. Output is deterministic: same cache in, identical JSON out.

## Layout

```
ledger/    venue-agnostic lots, FIFO, basis/proceeds, P&L (imports nothing from sources/ or leaks/;
           tests/test_ledger.py walks the AST to enforce it)
sources/   cache.py (SQLite), http.py (token bucket 4 GET/s, Retry-After, backoff), opensea.py,
           chain.py (receipts, Seaport decode, Blockscout tx list, ENS), prices.py, normalize.py
leaks/     one module per leak, compute(ledger, prices, params) -> LeakResult; concentration.py
score.py   report.py (+ report.schema.json)   card/render.py   cli.py
scripts/   record_fixtures.py (records a real refresh into tests/fixtures), build_eth_usd_csv.py
```

## Decisions

Judgment calls, and the simpler reading taken in each:

1. **Period.** Lots are built from the wallet's full OpenSea history so pre-2022-06-14 purchases keep
   their basis. `--since` decides what counts: sales, purchases and churn before it are out of every
   leak, and lots disposed before it are out of scope. `deployed` sums the basis of in-scope lots (alive at
   some point in the period), not every lot ever acquired, and leaves out lots that exited unpriced
   (`disposed_unpriced`, `sale_unpriced`): no leak can judge them, so they would only dilute the score.
   Their basis is reported as `quality.unscored_basis_eth`.
2. **Royalty attribution.** Royalty = every consideration payment that goes neither to an OpenSea fee
   collector nor to the seller. The obvious alternative, "the collection's payout address", fails because payout addresses change
   over time, and this reading gives the same number when there is one recipient.
3. **Sold into bids** counts every accepted offer. Seaport resolves criteria items before emitting
   `OrderFulfilled`, so a collection or trait offer cannot be told apart from an item offer on-chain.
4. **Bundles** split by each item's floor that day; if any item has no floor, by units.
5. **Gas in a multi-item transaction** is split across the wallet's items by units.
6. **Mint price** is the transaction's ETH value, split across the units minted in it. A transfer in, in a
   transaction the wallet sent with ETH attached, is priced at that value (`paid_unattributed`, e.g. a buy
   on another venue) instead of being called a gift.
7. **Held past the floor** scans from max(acquired, `--since`) and compares floor × qty with 0.9 × basis.
8. **median30** is the lower median of the carried-forward daily floors over the 30 days before the day.
9. **Concentration level**: `high` if top1 > 50% or top3 > 80%, else `normal`; `none` with no floor value.
10. **Other payment tokens**: USDC, USDT and DAI are priced at $1 and converted at the day's ETH close.
    Anything else is unpriced and counted in `unpriced_sales`.
11. **ETH/USD.** CoinGecko's free tier rejects `days=max` (HTTP 401, 365-day cap), so CoinGecko supplies
    the last 365 days and the bundled `sources/data/eth_usd.csv` (daily closes from Coinbase public candles,
    built by `scripts/build_eth_usd_csv.py`) covers older days. A CoinGecko point stamped 00:00 UTC is used
    as the previous day's close.
12. **Transaction list for leak 4** comes from Blockscout's free Etherscan-compatible `txlist`. Alchemy
    `getAssetTransfers` skips zero-value calls such as cancels and approvals, and Etherscan needs a key.
13. **Listing churn** also counts `setApprovalForAll` to marketplace operators (OpenSea conduit, Blur
    delegate) and WETH wraps, unwraps and approvals. A Seaport or Blur tx that moved an NFT is a fulfilment.
14. **`as_of`** is the date of the last `--refresh`, so re-running on the same cache gives byte-identical
    JSON.
15. **Wash trades** (sales between the wallet and `--linked` wallets) are dropped entirely, fees included.
16. **Sales with no Seaport decode** (other venues in OpenSea's feed, pre-Seaport history) use OpenSea's
    payment as the price with fees unknown (`fees_unknown`, counted in `sales_without_seaport_decode`).
17. **Score rounding** is half up; `low_sample` counts in-scope lots with non-zero basis.
18. **Card font** is Aileron Regular (public domain), the font Pillow embeds, saved to `card/assets/`.
    It has no arrow or minus glyphs, so the card sticks to ASCII and `·`.
19. **Receipts the RPC lacks** come from dRPC's free keyless endpoint (`https://eth.drpc.org`). Pruned nodes
    (publicnode and most free endpoints) answer `null` for anything older than a few days; a `null` receipt is
    never cached, so a later refresh retries it. Blockscout's eth-rpc was tried first and allows ~10 calls per
    40 minutes. `sources.rpc` reads e.g. `alchemy+drpc` when the fallback was used.
20. **Floor history** is fetched only for collections where it can move a number: a trade, or a lot with a
    cost (price or gas). Pure airdrops get collection stats only (current floor, last sale).
21. **Sweep prices.** In a multi-item buy, OpenSea's event reports the average price per item; the tool uses each
    item's own Seaport fill, so basis can differ from OpenSea's number by item while the tx total matches.
22. **Shared Storefront twins.** OpenSea lists some trades on both `0x495f…7b5e` and its successor `0xa604…dacec`;
    only the successor appears in the Seaport fill. The legacy row is kept (holdings list it there) and priced
    from the successor's fill.
23. **A Blockscout tx-list failure** (429) does not stop a refresh: the cached list is used and the next refresh
    continues from the last block.
24. **Floors before OpenSea's history.** When a collection needs days before its OpenSea series starts, its sales in
    just that window are fetched once (`after`/`before`, remembered in meta) and the daily lowest sale is the floor
    there; OpenSea wins from its first day. `floor_source = opensea+sales_proxy`.
25. **One lot, one leak.** A lot that is both held past the floor and dead inventory counts only in the larger of
    the two (both weigh 0.5). Since the held-past leak is always below basis, in practice dead lots drop out of
    leak 2; `detail.counted_in_dead_inventory` says how many.
26. **ENS** goes through the ENS Universal Resolver (`0xeeee…eeee`) for both name to address and the card's
    reverse name. The legacy registry alone misses names managed through newer ENS
    contracts. A resolver revert means "no name", not an error.

## Known gaps

- No Blur fills, LooksRare, X2Y2 or Wyvern fee decoding; no Base or Solana.
- Valuation is collection-level. Leaks 2, 3 and 6 misread gen-art wallets where a strong output trades far
  above floor (`quality.valuation = "collection_level"`).
- Royalties are never counted avoidable until the enforcement check exists.
- The sales-proxy floor (used when OpenSea's floor history is missing) reads at most 200 pages of a
  collection's sales, so very busy collections may lack early history.
- `holdings_check` in `quality` compares open lots with OpenSea's current NFTs; differences are tokens the
  events feed missed or spam it hides.
- **OpenSea's floor history (`floor_prices?timeframe=all_time`) starts around 2024-09-16 for most collections.**
  Earlier days use the daily lowest sale in that window (decision 24). Busy collections can hit the 200-page cap
  and keep holes; those purchases show up in `purchases_without_floor`.
  The daily lowest sale is noisy for editions and mixed collections (e.g. meme-ltd's median30 of 0.003 ETH).
- A first refresh on a big wallet is slow: one with ~4,000 events across ~1,000 collections takes over an hour,
  mostly the per-collection floor fetch at OpenSea's 4 GET/s. Every refresh re-fetches every collection's floors.
- Tokens moved in from your own other wallets have basis 0 unless those wallets are passed with `--linked`.

## License

MIT. The bundled Aileron font is public domain.
