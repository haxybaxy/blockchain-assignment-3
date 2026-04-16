# Exercise 1 — Vending Machine dApp

A smart-contract-backed vending machine: the owner lists products, users pay ETH, the contract tracks ownership on-chain, and a Python Flask app provides the UI.

## Layout

```
exercise-1/
├── contracts/VendingMachine.sol     # Solidity contract
├── tests/VendingMachine.test.ts     # Hardhat + Mocha/Chai tests
├── scripts/deploy.ts                # Deploys + seeds + writes ABI/address for the Flask app
├── hardhat.config.ts, package.json, tsconfig.json
├── docker-compose.yml               # Ganache service
└── app/
    ├── app.py                       # Flask UI
    ├── web3_client.py               # web3.py wrapper
    ├── requirements.txt
    ├── .env.example
    ├── templates/                   # base, index, owned, admin
    └── static/style.css
```

## How to run

1. **Install contract tooling** (Node ≥ 18):
   ```bash
   cd implementation/exercise-1
   npm install
   ```

2. **Start Ganache** (choose one):
   ```bash
   docker compose up -d                       # uses docker-compose.yml
   # OR
   npx ganache --chain.chainId=1337 --wallet.deterministic=true
   ```

3. **Run the tests** (runs against Hardhat's in-memory chain, no Ganache needed):
   ```bash
   npx hardhat test
   ```

4. **Deploy to Ganache + seed catalog:**
   ```bash
   npx hardhat run scripts/deploy.ts --network ganache
   ```
   This writes `app/deployed.json` and `app/VendingMachine.abi.json` — the Flask app reads both on startup.

5. **Run the Flask UI:**
   ```bash
   cd app
   cp .env.example .env
   python3 -m venv .venv && source .venv/bin/activate
   pip install -r requirements.txt
   python app.py
   ```
   Open http://127.0.0.1:5001. Use the wallet dropdown (top right) to act as different Ganache accounts. Account #0 is the contract owner and sees the Admin panel; other accounts see only Products and My Items.

## Testing

All tests live in `tests/VendingMachine.test.ts` and run under Hardhat (`npx hardhat test`). There are ten tests in six `describe` blocks:

| # | Test | Category |
|---|------|---------|
| 1 | Owner can add a product, `ProductAdded` event emitted with correct args | happy path |
| 2 | Empty name / zero price are rejected (`EmptyName`, `ZeroPrice`) | input validation |
| 3 | Purchase happy path: stock decrements, receipt recorded, `ownedQuantity` updated, contract balance reflects payment, event emitted | **successful purchase + state change** |
| 4 | Excess ETH is refunded — buyer's net spend equals cost + gas only | payment edge case |
| 5 | Ownership accumulates across multiple purchases by the same buyer | state change after sequence |
| 6 | `purchase` reverts with `InsufficientPayment` when `msg.value < price × qty` | **failed purchase: insufficient payment** |
| 7 | `purchase` reverts with `InsufficientStock` when qty > available | **failed purchase: out of stock** |
| 8 | `purchase` reverts with `ProductInactive` on paused products | invalid state |
| 9 | `purchase` reverts with `ProductNotFound` for unknown id | invalid input |
| 10 | Non-owner cannot `restockProduct` (revert `OwnableUnauthorizedAccount`) | **permission failure** |
| 11 | Non-owner cannot `addProduct` or `updatePrice` | permission failure |
| 12 | `restockProduct` and `updatePrice` succeed as owner; events emitted; state updated | admin happy path |
| 13 | `withdraw` sends contract balance to owner only; non-owner cannot call | admin + access control |

The five required categories (successful purchase, insufficient payment, out of stock, permission failure, post-tx state check) are all covered; the rest are additional coverage for edge cases.

## Design choices — on-chain vs off-chain

| Data / behavior | Location | Reasoning |
|---|---|---|
| Product `priceWei` | **on-chain** | The contract enforces payment; price has to be authoritative here or the guarantee breaks. |
| Product `stock` | **on-chain** | Prevents over-selling. A centralized store would be a point of trust. |
| Product `active` flag | **on-chain** | Lets the owner pause sales without deleting the product, with the contract enforcing it. |
| Ownership (`Receipt`s + `ownedQuantity`) | **on-chain** | The whole point is verifiable ownership — anyone can read `balanceOf(user, productId)` without trusting the backend. |
| Events (`ProductAdded`, `ProductPurchased`, …) | **on-chain (logs)** | Cheap to emit, enable off-chain indexing / history feeds without extra storage. |
| Excess-payment refund | **on-chain** | Refund logic must be part of the atomic purchase; off-chain would break the trust model. |
| Product images, long descriptions | **off-chain** | Storing blobs on-chain is expensive and gives no trust benefit — the UI is just presentation. |
| Transaction receipt UI (hash, block, gas) | **off-chain** | Derived from `eth_getTransactionReceipt`; no state needs to persist in the contract. |
| Flask session / selected wallet | **off-chain** | Purely UX; irrelevant to the contract. |
| Historical purchase feed | **off-chain, derived from events** | Could be rebuilt by anyone from the chain; no need to duplicate storage. |

## Security considerations

- **Access control:** admin functions use OpenZeppelin's `Ownable`. OZ is a well-audited, minimal choice — `onlyOwner` is one of the most common patterns in the ecosystem, and writing a custom equivalent would add risk without educational value. Non-owners get `OwnableUnauthorizedAccount` (a custom error defined by OZ v5).
- **Reentrancy:** `purchase` uses checks-effects-interactions. All state mutations (stock, receipts, owned qty) happen before the optional `call{value: excess}` refund. No state is read from the refund recipient, so reentering can only succeed against already-updated state. No `ReentrancyGuard` needed for this design.
- **Payment validation:** `msg.value >= totalCost` enforced with a custom error; exact cost computed from the *on-chain* price, not caller-supplied. Excess is refunded via low-level `call` (handles receivers with code); failure reverts the whole tx.
- **Input validation:** reject empty product names, zero prices, zero quantities, unknown product ids. Each has a named custom error so front-ends can present friendly messages.
- **Integer arithmetic:** Solidity ≥ 0.8 has built-in overflow checks. Product stock and per-user quantity are `uint256`; no chance of practical overflow.
- **Replay / frontrunning:** admin price updates could frontrun buyers — documented as accepted for a teaching example. In production you'd use a commit-reveal or accept the user's `expectedPrice` as a parameter.
- **Abuse vectors considered:** dust-price griefing (rejected by `ZeroPrice`); stock-drain via reentrancy (prevented by CEI); denial of service via refund failure (reverts cleanly, nothing stuck).

## Not done / scope limits

- No ERC-721 / ERC-20. Receipts are a minimal custom struct so the data model stays focused on the assignment's ownership requirement without dragging in token-transfer semantics.
- No upgradeability, no proxy patterns. The contract is small enough that re-deployment is the simplest path if changes are needed.
- Private keys live server-side in `.env` for local dev — noted clearly; a production version would use MetaMask + JS frontend.
