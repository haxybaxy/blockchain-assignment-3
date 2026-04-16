# Exercise 2 — Event Ticket Booking and Resale

The same application built twice: once as a traditional web2 backend (Flask + SQLite), once as a web3 dApp (Flask frontend talking to `EventTicketing.sol` via web3.py). The two versions share the same UX and the same feature set; the interesting difference is what the backend looks like and what it trusts.

## Layout

```
exercise-2/
├── contracts/EventTicketing.sol
├── tests/EventTicketing.test.ts       # 14 tests (assignment requires ≥8)
├── scripts/deploy.ts                  # deploys + seeds + writes ABI for web3 app
├── hardhat.config.ts, package.json, tsconfig.json, docker-compose.yml
└── app/
    ├── web2/                          # Flask + SQLAlchemy + SQLite
    │   ├── app.py, models.py
    │   ├── requirements.txt
    │   ├── templates/ static/
    └── web3/                          # Flask + web3.py + EventTicketing.sol
        ├── app.py, web3_client.py
        ├── requirements.txt, .env.example
        ├── templates/ static/
```

## How to run

### Web3 version

1. Install Node deps and start Ganache:
   ```bash
   cd implementation/exercise-2
   npm install
   docker compose up -d       # or: npx ganache --chain.chainId=1337 --wallet.deterministic=true
   ```
2. Run the contract tests (Hardhat's in-memory chain):
   ```bash
   npx hardhat test
   ```
3. Deploy to Ganache + seed:
   ```bash
   npx hardhat run scripts/deploy.ts --network ganache
   ```
4. Run the Flask web3 app:
   ```bash
   cd app/web3
   cp .env.example .env
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python app.py   # http://127.0.0.1:5003
   ```
   Select wallets from the top-right dropdown. Account #0 is the contract owner (★).

### Web2 version

```bash
cd implementation/exercise-2/app/web2
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py   # http://127.0.0.1:5002
```
Seeded users:
- `admin@example.com` / `admin` — admin (can create events)
- `alice@example.com` / `alice`
- `bob@example.com` / `bob`

The two apps can run side-by-side on different ports.

## Testing

All 14 tests live in `tests/EventTicketing.test.ts`. Run them with `npx hardhat test`.

| # | Test | Assignment requirement |
|---|------|---|
| 1 | Owner can `createEvent`, `EventCreated` emitted | happy path |
| 2 | Non-owner `createEvent` reverts `OwnableUnauthorizedAccount` | **permission failure (admin-only)** |
| 3 | `buyTicket` happy path — ticket minted, sold incremented, owner set, event emitted | **successful ticket purchase** |
| 4 | `buyTicket` reverts `SoldOut` when supply exhausted | invalid state |
| 5 | `buyTicket` reverts `IncorrectPayment` on underpay | **failed purchase (rules not satisfied)** |
| 6 | `buyTicket` reverts `EventInactive` on paused events | invalid state |
| 7 | `buyTicket` refunds overpayment | payment edge case |
| 8 | `transferTicket` happy path — owner changes, history updated | **successful transfer** |
| 9 | `transferTicket` by non-owner reverts `NotTicketOwner` | **failed transfer** |
| 10 | Resale happy path: `listForResale` + `buyResale` — ownership flips, seller paid, fee retained | **successful resale flow** |
| 11 | `buyResale` on unlisted ticket reverts `NotForSale` | invalid state |
| 12 | `transferTicket` on a listed ticket reverts `AlreadyListed` | **repeated / invalid state transition** |
| 13 | `listForResale` above the 2× cap reverts `PriceTooHigh` | invalid input |
| 14 | `cancelResale` happy path |  supplementary |
| 15 | Sequence: buy → transfer → list → resale — final ownership verified across three addresses | **final ownership after a sequence of actions** |
| 16 | `withdraw` pays owner only; fee accumulates from resale; non-owner reverts | admin + access control |

The eight required scenarios are covered by #3, #5, #8, #9, #10, #2, #12, and #15 respectively.

## Design choices — on-chain vs off-chain

### On the chain (web3 version)

| Data / behavior | Why on-chain |
|---|---|
| Event `pricePerTicket`, `maxSupply`, `active` | Primary sale is enforced by the contract — these are the inputs to its decisions; off-chain values would be unenforceable. |
| `sold` counter per event | Prevents over-issuance; has to be atomic with payment. |
| Ticket `owner` (current) | The whole trust story is "who owns this ticket" — anyone can verify without asking a server. |
| Ticket `originalPrice` (snapshot) | Needed for the 2×-face-value resale cap to be enforced. Without the snapshot, a price change on the event would re-anchor the cap, which defeats the scalping constraint. |
| Resale `listingPrice` + `forSale` | Decentralized secondary market — listings visible to anyone, settled atomically. |
| 2% protocol fee on resale | Trust-minimised revenue capture; no need for the owner to chase payments. |
| Events (`EventCreated`, `TicketPurchased`, …) | Cheap to emit, enable off-chain indexers / explorers / activity feeds. |

### Off the chain

| Data / behavior | Why off-chain |
|---|---|
| Event description, images, venue | Blobs on-chain are expensive and offer no trust benefit — display only. |
| User profile (email, display name, etc.) | Identity on-chain is the address; anything richer belongs in a separate identity system. |
| QR codes / check-in tokens | Derive at scan time from a signed challenge proving ownership; no need for extra on-chain state. |
| Transaction status UI | Already available from `eth_getTransactionReceipt`; the Flask app just formats it. |
| Account selector / session | Pure UX. |
| Seeded test users in web2 | Bootstrap convenience; not part of the domain model. |

## Security considerations

- **Access control:** admin uses OpenZeppelin `Ownable`. All admin functions (`createEvent`, `setEventActive`, `withdraw`) use `onlyOwner`; non-owners hit `OwnableUnauthorizedAccount`.
- **Reentrancy:** `buyTicket` and `buyResale` both use checks-effects-interactions. State (ownership, flags, counters) is updated *before* the low-level `call` that pays seller / refunds buyer. No external calls precede state changes.
- **Payment flow on resale:** contract pays seller with `proceeds = price − fee`; fee stays in the contract. If the seller's receiver contract rejects ETH, the whole resale reverts — better to fail loudly than leave a ticket half-sold.
- **Invalid state transitions:** transfer is rejected while listed (`AlreadyListed`); double-listing is rejected; `cancelResale` requires the caller to be the current owner. This forces the resale state machine to be explicit.
- **Scalping constraint:** 2× face-value cap is deliberately simple. A production version might use a buyer's signed maximum price, or auction mechanics. The cap uses `originalPrice` (snapshot at mint) so a later price update on the event can't be abused.
- **Integer safety:** Solidity 0.8+ handles overflow. All values are `uint256`.
- **Frontrunning / MEV:** `buyResale` accepts `msg.value < listingPrice` -> revert, `msg.value > listingPrice` -> refund excess. A frontrunner can still snipe a cheap listing, documented as accepted for the assignment.
- **Self-trades:** `buyResale` rejects purchases from the current owner (`InvalidRecipient`); `transferTicket` rejects zero address and self-transfer.

## Web2 vs web3 — comparison

| Dimension | Web2 (Flask + SQLite) | Web3 (`EventTicketing.sol`) |
|---|---|---|
| **Trust root** | The backend operator. Anyone with DB access can rewrite tickets, change supplies, refund silently. | The contract + its deployer's privileges. Anyone can read the state; rules are enforced atomically. |
| **Identity** | Email + password + session cookie. | Ethereum address (here selected from Ganache's deterministic accounts; in production, MetaMask). No PII. |
| **Payment** | Not actually handled — the demo flips ownership without charging. A real web2 would integrate Stripe and rely on a PSP for dispute handling. | Native ETH; resale settles on-chain in one transaction (seller paid, fee retained). |
| **Admin power** | `is_admin` flag in `users` table; changeable by anyone with DB write. | `owner` is whoever deployed the contract; transferring requires an explicit tx. The rules that admin can't change (2× cap, fee %) are hardcoded in the bytecode. |
| **Data ownership** | App operator owns the DB. Can export, redact, or lose it. | State is replicated by every node running that chain — the operator can't unilaterally delete. |
| **Failure modes** | Backend down → whole app down. Data loss if DB isn't backed up. | Chain congestion → tx delayed / expensive. Contract bug → funds possibly stuck (no upgradability here, deliberately). |
| **Cost profile** | Flat: a few dollars / month for a small DB and host. | Per-action: every state change costs gas; reads are free. Primary mint ~50–80k gas, resale ~70–100k gas. |
| **Latency** | Millisecond DB writes; instant UI feedback. | Block-time (~2–12s on public chains, ~1s on Ganache). UI must show pending state. |
| **Anti-scalping policy** | Decided by the operator at runtime — could be changed for an event, bypassed for a VIP, disabled quietly. | Encoded in the contract. Changing it requires a redeploy, which everyone can see. |
| **Refunds / cancellations** | Backend issues a DB update + payment reversal. | No automatic path — you'd need an explicit contract method, or a signed off-chain refund redeemed by the holder. (Not implemented here.) |
| **Auditability** | Whatever logs the operator chose to keep. | Full event log is public and permanent. An indexer can reconstruct ticket lineage from mint to current owner. |
| **Code reuse** | Frontend template is ~identical across versions — the delta is in the route handlers and the storage layer. | Same. | 

### When each makes sense

- **Web2 wins** when you don't need the trust guarantee (small events, trusted venues), need speed and low operating cost, want flexible admin controls, and don't want users to buy ETH before they can buy a ticket.
- **Web3 wins** when tickets need to be portable across venues / platforms, when you want a transparent anti-scalping policy users can verify, or when you want users to provably own the ticket without the venue's continued cooperation.

## Not done / scope limits

- No ERC-721. Assignment explicitly discourages rote-copying existing token standards; the minimal struct-based model is clearer and fits the "explain your choices" requirement.
- No proxy/upgradeability. Small contract — redeploy is the correct path here.
- Web2 payment flow is stubbed (ownership flips without charging). Adding Stripe would add dependencies unrelated to the assignment's focus.
- Web3 frontend uses server-held private keys derived from the Ganache mnemonic. This is clearly only appropriate for local dev; a production rewrite would swap Flask for a JS frontend + MetaMask.
