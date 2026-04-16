# Assignment 3 — Smart Contracts and DApp Development

Two exercises, each self-contained with its own Hardhat project, contracts, tests, and client apps.

- **[Exercise 1 — Vending Machine dApp](./exercise-1/README.md)**
  Solidity contract + Python Flask client. Tracks on-chain ownership of purchased items.
- **[Exercise 2 — Event Ticket Booking (web2 + web3)](./exercise-2/README.md)**
  Same app built twice for comparison. Flask + SQLite version vs Flask + `EventTicketing.sol`.

## Quick grading path

```bash
# Exercise 1 — 10 contract tests
cd implementation/exercise-1
npm install
npx hardhat test

# Exercise 2 — 14 contract tests
cd ../exercise-2
npm install
npx hardhat test
```

Each exercise's README covers running the Flask UI end-to-end against a local Ganache instance.
