"""
Thin wrapper around web3.py used by the Flask front-end.

Design notes:
- The Ganache mnemonic is deterministic in dev, so we derive the 10 accounts
  locally and let the UI pick which one to act as. In production, the user
  would bring their own wallet (MetaMask) — this simplification is dev-only
  and is called out in the README.
- All signing happens server-side. Private keys come from the mnemonic in
  `.env`; they never leave the machine.
- Reads are plain calls. Writes build a transaction, sign with the selected
  account, and wait for a receipt so the UI can show status + tx hash.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

from eth_account import Account
from web3 import Web3
from web3.middleware import geth_poa_middleware

# HD path used by Ganache for account derivation (BIP-44, Ethereum).
_HD_PATH = "m/44'/60'/0'/0/{index}"
_NUM_ACCOUNTS = 10

Account.enable_unaudited_hdwallet_features()


@dataclass(frozen=True)
class LocalAccount:
    index: int
    address: str
    private_key: str


def _app_dir() -> Path:
    return Path(__file__).resolve().parent


def _load_deployed() -> dict:
    path = _app_dir() / "deployed.json"
    if not path.exists():
        raise FileNotFoundError(
            "deployed.json not found. Run the Hardhat deploy script first:\n"
            "  npx hardhat run scripts/deploy.ts --network ganache"
        )
    with path.open() as f:
        return json.load(f)


def _load_abi() -> list:
    path = _app_dir() / "VendingMachine.abi.json"
    if not path.exists():
        raise FileNotFoundError(
            "VendingMachine.abi.json not found. Run the Hardhat deploy script first."
        )
    with path.open() as f:
        return json.load(f)


def _derive_accounts(mnemonic: str) -> list[LocalAccount]:
    accounts = []
    for i in range(_NUM_ACCOUNTS):
        acct = Account.from_mnemonic(mnemonic, account_path=_HD_PATH.format(index=i))
        accounts.append(LocalAccount(index=i, address=acct.address, private_key=acct.key.hex()))
    return accounts


class VendingClient:
    def __init__(self, rpc_url: str, mnemonic: str):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        # Some local chains (e.g. Ganache behind certain forks) emit POA blocks.
        # Injecting the middleware is a no-op on plain Ganache.
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        if not self.w3.is_connected():
            raise ConnectionError(f"Cannot connect to RPC at {rpc_url}")

        info = _load_deployed()
        abi = _load_abi()
        self.address = Web3.to_checksum_address(info["address"])
        self.owner = Web3.to_checksum_address(info["owner"])
        self.chain_id = int(info["chainId"])
        self.contract = self.w3.eth.contract(address=self.address, abi=abi)
        self.accounts = _derive_accounts(mnemonic)
        self._by_addr = {a.address: a for a in self.accounts}

    # ----- Accounts -----

    def account_by_address(self, address: str) -> LocalAccount:
        return self._by_addr[Web3.to_checksum_address(address)]

    def eth_balance(self, address: str) -> int:
        return self.w3.eth.get_balance(Web3.to_checksum_address(address))

    def is_owner(self, address: str) -> bool:
        return Web3.to_checksum_address(address) == self.owner

    # ----- Reads -----

    def list_products(self) -> list[dict]:
        ids, names, prices, stocks, actives = self.contract.functions.getAllProducts().call()
        return [
            {"id": int(i), "name": n, "priceWei": int(p), "stock": int(s), "active": bool(a)}
            for i, n, p, s, a in zip(ids, names, prices, stocks, actives)
        ]

    def receipts_of(self, address: str) -> list[dict]:
        address = Web3.to_checksum_address(address)
        ids = self.contract.functions.getReceiptIdsOf(address).call()
        out = []
        for rid in ids:
            product_id, buyer, qty, unit_price, ts = self.contract.functions.getReceipt(rid).call()
            out.append(
                {
                    "id": int(rid),
                    "productId": int(product_id),
                    "buyer": buyer,
                    "quantity": int(qty),
                    "unitPrice": int(unit_price),
                    "timestamp": int(ts),
                }
            )
        return out

    # ----- Writes -----

    def _send(self, fn, sender: LocalAccount, value_wei: int = 0) -> dict:
        """Build, sign, broadcast a tx and return a receipt summary."""
        tx = fn.build_transaction(
            {
                "from": sender.address,
                "nonce": self.w3.eth.get_transaction_count(sender.address),
                "chainId": self.chain_id,
                "value": value_wei,
                # Let the node estimate gas; fall back to a sane cap on failure.
            }
        )
        # Fill gas if not filled by estimation (some reverting calls skip estimateGas).
        if "gas" not in tx:
            try:
                tx["gas"] = self.w3.eth.estimate_gas(tx)
            except Exception:
                tx["gas"] = 500_000
        if "gasPrice" not in tx and "maxFeePerGas" not in tx:
            tx["gasPrice"] = self.w3.eth.gas_price

        signed = self.w3.eth.account.sign_transaction(tx, sender.private_key)
        tx_hash = self.w3.eth.send_raw_transaction(signed.rawTransaction)
        receipt = self.w3.eth.wait_for_transaction_receipt(tx_hash, timeout=60)
        return {
            "txHash": tx_hash.hex(),
            "blockNumber": receipt.blockNumber,
            "gasUsed": receipt.gasUsed,
            "status": "success" if receipt.status == 1 else "failed",
        }

    def purchase(self, sender_addr: str, product_id: int, quantity: int, value_wei: int) -> dict:
        sender = self.account_by_address(sender_addr)
        fn = self.contract.functions.purchase(product_id, quantity)
        return self._send(fn, sender, value_wei=value_wei)

    def add_product(self, sender_addr: str, name: str, price_wei: int, stock: int) -> dict:
        sender = self.account_by_address(sender_addr)
        fn = self.contract.functions.addProduct(name, price_wei, stock)
        return self._send(fn, sender)

    def restock(self, sender_addr: str, product_id: int, amount: int) -> dict:
        sender = self.account_by_address(sender_addr)
        fn = self.contract.functions.restockProduct(product_id, amount)
        return self._send(fn, sender)

    def update_price(self, sender_addr: str, product_id: int, new_price_wei: int) -> dict:
        sender = self.account_by_address(sender_addr)
        fn = self.contract.functions.updatePrice(product_id, new_price_wei)
        return self._send(fn, sender)

    def set_active(self, sender_addr: str, product_id: int, active: bool) -> dict:
        sender = self.account_by_address(sender_addr)
        fn = self.contract.functions.setProductActive(product_id, active)
        return self._send(fn, sender)

    def withdraw(self, sender_addr: str) -> dict:
        sender = self.account_by_address(sender_addr)
        fn = self.contract.functions.withdraw()
        return self._send(fn, sender)


def build_client_from_env() -> VendingClient:
    rpc = os.environ.get("RPC_URL", "http://127.0.0.1:8545")
    mnemonic = os.environ.get("GANACHE_MNEMONIC")
    if not mnemonic:
        raise RuntimeError("GANACHE_MNEMONIC not set; copy .env.example to .env")
    return VendingClient(rpc_url=rpc, mnemonic=mnemonic)
