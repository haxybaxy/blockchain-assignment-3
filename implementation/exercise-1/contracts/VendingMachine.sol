// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @title VendingMachine
/// @notice A simple on-chain vending machine: the owner lists products with a
///         price and stock, buyers pay ETH to receive ownership receipts for
///         the products they purchase. Ownership is tracked on chain so anyone
///         can verify who holds what.
/// @dev    Design notes:
///         - Product price/stock/active flag are authoritative on chain because
///           they are the inputs to the payment-enforcement logic.
///         - Each purchase creates a Receipt (kind of like a lightweight NFT
///           without the ERC-721 overhead) so the history and ownership are
///           transparent and enumerable per user.
///         - Excess ETH is refunded via a low-level call after state updates
///           (checks-effects-interactions) to avoid reentrancy.
contract VendingMachine is Ownable {
    struct Product {
        string name;
        uint256 priceWei;
        uint256 stock;
        bool active;
        bool exists;
    }

    struct Receipt {
        uint256 productId;
        address buyer;
        uint256 quantity;
        uint256 unitPrice;
        uint256 timestamp;
    }

    // ---------- Storage ----------

    uint256 public productCount;
    mapping(uint256 => Product) private _products;

    uint256 public receiptCount;
    mapping(uint256 => Receipt) private _receipts;

    // Per-user list of receipt ids — useful for showing purchase history off-chain.
    mapping(address => uint256[]) private _userReceiptIds;

    // Quick lookup: how many units of product P does address A currently own?
    // (Receipts remain immutable history; this is the aggregated balance.)
    mapping(address => mapping(uint256 => uint256)) private _ownedQuantity;

    // ---------- Events ----------

    event ProductAdded(uint256 indexed productId, string name, uint256 priceWei, uint256 stock);
    event ProductRestocked(uint256 indexed productId, uint256 addedAmount, uint256 newStock);
    event ProductPriceUpdated(uint256 indexed productId, uint256 oldPrice, uint256 newPrice);
    event ProductStatusChanged(uint256 indexed productId, bool active);
    event ProductPurchased(
        uint256 indexed receiptId,
        uint256 indexed productId,
        address indexed buyer,
        uint256 quantity,
        uint256 totalPaid
    );
    event Withdrawn(address indexed to, uint256 amount);

    // ---------- Errors ----------

    error ProductNotFound(uint256 productId);
    error ProductInactive(uint256 productId);
    error InsufficientPayment(uint256 required, uint256 provided);
    error InsufficientStock(uint256 productId, uint256 requested, uint256 available);
    error ZeroQuantity();
    error EmptyName();
    error ZeroPrice();
    error TransferFailed();

    // ---------- Constructor ----------

    constructor() Ownable(msg.sender) {}

    // ---------- Admin (onlyOwner) ----------

    /// @notice List a new product for sale.
    function addProduct(
        string calldata name,
        uint256 priceWei,
        uint256 initialStock
    ) external onlyOwner returns (uint256 productId) {
        if (bytes(name).length == 0) revert EmptyName();
        if (priceWei == 0) revert ZeroPrice();

        productId = productCount;
        _products[productId] = Product({
            name: name,
            priceWei: priceWei,
            stock: initialStock,
            active: true,
            exists: true
        });
        productCount += 1;

        emit ProductAdded(productId, name, priceWei, initialStock);
    }

    /// @notice Add more units to an existing product's stock.
    function restockProduct(uint256 productId, uint256 amount) external onlyOwner {
        Product storage p = _requireProduct(productId);
        if (amount == 0) revert ZeroQuantity();
        p.stock += amount;
        emit ProductRestocked(productId, amount, p.stock);
    }

    /// @notice Update the unit price of an existing product.
    function updatePrice(uint256 productId, uint256 newPriceWei) external onlyOwner {
        Product storage p = _requireProduct(productId);
        if (newPriceWei == 0) revert ZeroPrice();
        uint256 old = p.priceWei;
        p.priceWei = newPriceWei;
        emit ProductPriceUpdated(productId, old, newPriceWei);
    }

    /// @notice Pause or unpause sales of a product without losing its state.
    function setProductActive(uint256 productId, bool active) external onlyOwner {
        Product storage p = _requireProduct(productId);
        p.active = active;
        emit ProductStatusChanged(productId, active);
    }

    /// @notice Withdraw the contract's collected ETH to the owner.
    function withdraw() external onlyOwner {
        uint256 balance = address(this).balance;
        if (balance == 0) revert ZeroQuantity();
        (bool ok, ) = payable(owner()).call{value: balance}("");
        if (!ok) revert TransferFailed();
        emit Withdrawn(owner(), balance);
    }

    // ---------- Purchase ----------

    /// @notice Buy `quantity` units of product `productId`. Any ETH above the
    ///         required total is refunded to the caller.
    function purchase(uint256 productId, uint256 quantity) external payable {
        if (quantity == 0) revert ZeroQuantity();
        Product storage p = _requireProduct(productId);
        if (!p.active) revert ProductInactive(productId);
        if (quantity > p.stock) revert InsufficientStock(productId, quantity, p.stock);

        uint256 totalCost = p.priceWei * quantity;
        if (msg.value < totalCost) revert InsufficientPayment(totalCost, msg.value);

        // Effects: decrement stock, record receipt, update owned quantity
        p.stock -= quantity;

        uint256 receiptId = receiptCount;
        _receipts[receiptId] = Receipt({
            productId: productId,
            buyer: msg.sender,
            quantity: quantity,
            unitPrice: p.priceWei,
            timestamp: block.timestamp
        });
        _userReceiptIds[msg.sender].push(receiptId);
        _ownedQuantity[msg.sender][productId] += quantity;
        receiptCount += 1;

        emit ProductPurchased(receiptId, productId, msg.sender, quantity, totalCost);

        // Interaction: refund any excess ETH
        uint256 excess = msg.value - totalCost;
        if (excess > 0) {
            (bool ok, ) = payable(msg.sender).call{value: excess}("");
            if (!ok) revert TransferFailed();
        }
    }

    // ---------- Views ----------

    /// @notice Fetch a single product by id.
    function getProduct(uint256 productId)
        external
        view
        returns (string memory name, uint256 priceWei, uint256 stock, bool active)
    {
        Product storage p = _products[productId];
        if (!p.exists) revert ProductNotFound(productId);
        return (p.name, p.priceWei, p.stock, p.active);
    }

    /// @notice Return the full product catalog (small set; fine for a demo).
    function getAllProducts()
        external
        view
        returns (
            uint256[] memory ids,
            string[] memory names,
            uint256[] memory prices,
            uint256[] memory stocks,
            bool[] memory actives
        )
    {
        uint256 n = productCount;
        ids = new uint256[](n);
        names = new string[](n);
        prices = new uint256[](n);
        stocks = new uint256[](n);
        actives = new bool[](n);
        for (uint256 i = 0; i < n; i++) {
            Product storage p = _products[i];
            ids[i] = i;
            names[i] = p.name;
            prices[i] = p.priceWei;
            stocks[i] = p.stock;
            actives[i] = p.active;
        }
    }

    /// @notice Return the raw receipt ids owned by `who`.
    function getReceiptIdsOf(address who) external view returns (uint256[] memory) {
        return _userReceiptIds[who];
    }

    /// @notice Fetch a specific receipt by id.
    function getReceipt(uint256 receiptId)
        external
        view
        returns (uint256 productId, address buyer, uint256 quantity, uint256 unitPrice, uint256 timestamp)
    {
        if (receiptId >= receiptCount) revert ProductNotFound(receiptId);
        Receipt storage r = _receipts[receiptId];
        return (r.productId, r.buyer, r.quantity, r.unitPrice, r.timestamp);
    }

    /// @notice How many units of `productId` does `who` currently own?
    function balanceOf(address who, uint256 productId) external view returns (uint256) {
        return _ownedQuantity[who][productId];
    }

    // ---------- Internals ----------

    function _requireProduct(uint256 productId) private view returns (Product storage p) {
        p = _products[productId];
        if (!p.exists) revert ProductNotFound(productId);
    }
}
