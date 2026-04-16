"""
web3.py wrapper around EventTicketing.sol for the Flask app.

Same pattern as Exercise 1: deterministic Ganache accounts are derived from
the mnemonic; the UI picks which address to act as; the server signs tx on
behalf of the selected account.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account
from web3 import Web3
from web3.middleware import geth_poa_middleware

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
    p = _app_dir() / "deployed.json"
    if not p.exists():
        raise FileNotFoundError(
            "deployed.json missing. Run: npx hardhat run scripts/deploy.ts --network ganache"
        )
    with p.open() as f:
        return json.load(f)


def _load_abi() -> list:
    p = _app_dir() / "EventTicketing.abi.json"
    if not p.exists():
        raise FileNotFoundError("EventTicketing.abi.json missing — deploy first.")
    with p.open() as f:
        return json.load(f)


def _derive_accounts(mnemonic: str) -> list[LocalAccount]:
    out = []
    for i in range(_NUM_ACCOUNTS):
        acct = Account.from_mnemonic(mnemonic, account_path=_HD_PATH.format(index=i))
        out.append(LocalAccount(index=i, address=acct.address, private_key=acct.key.hex()))
    return out


class TicketingClient:
    def __init__(self, rpc_url: str, mnemonic: str):
        self.w3 = Web3(Web3.HTTPProvider(rpc_url))
        self.w3.middleware_onion.inject(geth_poa_middleware, layer=0)
        if not self.w3.is_connected():
            raise ConnectionError(f"Cannot reach RPC at {rpc_url}")
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

    def list_events(self) -> list[dict]:
        ids, names, prices, supplies, solds, actives = self.contract.functions.getAllEvents().call()
        return [
            {
                "id": int(i),
                "name": n,
                "priceWei": int(p),
                "maxSupply": int(s),
                "sold": int(so),
                "remaining": int(s) - int(so),
                "active": bool(a),
            }
            for i, n, p, s, so, a in zip(ids, names, prices, supplies, solds, actives)
        ]

    def get_event(self, event_id: int) -> dict:
        name, price, supply, sold, active = self.contract.functions.getEventInfo(event_id).call()
        return {
            "id": event_id,
            "name": name,
            "priceWei": int(price),
            "maxSupply": int(supply),
            "sold": int(sold),
            "remaining": int(supply) - int(sold),
            "active": bool(active),
        }

    def tickets_of(self, address: str) -> list[dict]:
        ids = self.contract.functions.ticketsOf(Web3.to_checksum_address(address)).call()
        out = []
        for tid in ids:
            t = self.get_ticket(int(tid))
            out.append(t)
        return out

    def listed_tickets(self) -> list[dict]:
        ids = self.contract.functions.listedTickets().call()
        return [self.get_ticket(int(tid)) for tid in ids]

    def get_ticket(self, ticket_id: int) -> dict:
        event_id, owner, original, listing, for_sale = self.contract.functions.getTicket(ticket_id).call()
        return {
            "id": ticket_id,
            "eventId": int(event_id),
            "owner": owner,
            "originalPriceWei": int(original),
            "listingPriceWei": int(listing),
            "forSale": bool(for_sale),
        }

    # ----- Writes -----

    def _send(self, fn, sender: LocalAccount, value_wei: int = 0) -> dict:
        tx = fn.build_transaction(
            {
                "from": sender.address,
                "nonce": self.w3.eth.get_transaction_count(sender.address),
                "chainId": self.chain_id,
                "value": value_wei,
            }
        )
        if "gas" not in tx:
            try:
                tx["gas"] = self.w3.eth.estimate_gas(tx)
            except Exception:
                tx["gas"] = 500_000
        if "gasPrice" not in tx and "maxFeePerGas" not in tx:
            tx["gasPrice"] = self.w3.eth.gas_price
        signed = self.w3.eth.account.sign_transaction(tx, sender.private_key)
        h = self.w3.eth.send_raw_transaction(signed.rawTransaction)
        r = self.w3.eth.wait_for_transaction_receipt(h, timeout=60)
        return {
            "txHash": h.hex(),
            "blockNumber": r.blockNumber,
            "gasUsed": r.gasUsed,
            "status": "success" if r.status == 1 else "failed",
        }

    def buy_ticket(self, sender_addr: str, event_id: int, value_wei: int) -> dict:
        sender = self.account_by_address(sender_addr)
        return self._send(self.contract.functions.buyTicket(event_id), sender, value_wei=value_wei)

    def transfer_ticket(self, sender_addr: str, ticket_id: int, to_addr: str) -> dict:
        sender = self.account_by_address(sender_addr)
        return self._send(
            self.contract.functions.transferTicket(ticket_id, Web3.to_checksum_address(to_addr)),
            sender,
        )

    def list_for_resale(self, sender_addr: str, ticket_id: int, price_wei: int) -> dict:
        sender = self.account_by_address(sender_addr)
        return self._send(self.contract.functions.listForResale(ticket_id, price_wei), sender)

    def cancel_resale(self, sender_addr: str, ticket_id: int) -> dict:
        sender = self.account_by_address(sender_addr)
        return self._send(self.contract.functions.cancelResale(ticket_id), sender)

    def buy_resale(self, sender_addr: str, ticket_id: int, value_wei: int) -> dict:
        sender = self.account_by_address(sender_addr)
        return self._send(self.contract.functions.buyResale(ticket_id), sender, value_wei=value_wei)

    def create_event(self, sender_addr: str, name: str, price_wei: int, supply: int) -> dict:
        sender = self.account_by_address(sender_addr)
        return self._send(self.contract.functions.createEvent(name, price_wei, supply), sender)

    def set_event_active(self, sender_addr: str, event_id: int, active: bool) -> dict:
        sender = self.account_by_address(sender_addr)
        return self._send(self.contract.functions.setEventActive(event_id, active), sender)

    def withdraw(self, sender_addr: str) -> dict:
        sender = self.account_by_address(sender_addr)
        return self._send(self.contract.functions.withdraw(), sender)


def build_client_from_env() -> TicketingClient:
    rpc = os.environ.get("RPC_URL", "http://127.0.0.1:8545")
    mnemonic = os.environ.get("GANACHE_MNEMONIC")
    if not mnemonic:
        raise RuntimeError("GANACHE_MNEMONIC not set; copy .env.example to .env")
    return TicketingClient(rpc_url=rpc, mnemonic=mnemonic)
