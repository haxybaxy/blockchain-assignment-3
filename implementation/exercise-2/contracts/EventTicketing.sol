// SPDX-License-Identifier: MIT
pragma solidity ^0.8.24;

import {Ownable} from "@openzeppelin/contracts/access/Ownable.sol";

/// @title EventTicketing
/// @notice Minimal on-chain ticketing: admin creates events, users pay to mint
///         tickets, tickets can be transferred freely or listed for resale
///         with an enforced price cap. Resale includes a small protocol fee
///         kept by the contract.
/// @dev    Design notes:
///         - Tickets are NOT ERC-721 — the assignment emphasises modelling
///           over rote copying. A compact struct with explicit ownership is
///           clearer and less opinionated about URIs/approvals.
///         - Resale has a 2× price cap to demonstrate the contract enforcing
///           a policy the web2 backend would ordinarily decide unilaterally.
///         - A 2% protocol fee on resales retains value in the contract for
///           the owner to withdraw; the rest goes to the seller.
contract EventTicketing is Ownable {
    struct EventInfo {
        string name;
        uint256 pricePerTicket;
        uint256 maxSupply;
        uint256 sold;
        bool active;
        bool exists;
    }

    struct Ticket {
        uint256 eventId;
        address owner;
        uint256 originalPrice; // snapshot of event price at mint — basis for resale cap
        uint256 listingPrice;  // in wei when `forSale`, otherwise 0
        bool forSale;
        bool exists;
    }

    // ---------- Storage ----------

    uint256 public eventCount;
    mapping(uint256 => EventInfo) private _events;

    uint256 public ticketCount;
    mapping(uint256 => Ticket) private _tickets;

    /// @notice Numerator for the resale fee (bps of 10000).
    uint256 public constant RESALE_FEE_BPS = 200; // 2%
    uint256 public constant RESALE_FEE_DENOM = 10000;

    /// @notice Multiplier cap on resale price relative to the original.
    uint256 public constant MAX_RESALE_MULTIPLIER = 2; // no more than 2× face value

    // ---------- Events ----------

    event EventCreated(uint256 indexed eventId, string name, uint256 pricePerTicket, uint256 maxSupply);
    event EventStatusChanged(uint256 indexed eventId, bool active);
    event TicketPurchased(uint256 indexed ticketId, uint256 indexed eventId, address indexed buyer, uint256 pricePaid);
    event TicketTransferred(uint256 indexed ticketId, address indexed from, address indexed to);
    event TicketListed(uint256 indexed ticketId, uint256 priceWei);
    event TicketUnlisted(uint256 indexed ticketId);
    event TicketResold(
        uint256 indexed ticketId,
        address indexed seller,
        address indexed buyer,
        uint256 pricePaid,
        uint256 sellerProceeds,
        uint256 protocolFee
    );
    event Withdrawn(address indexed to, uint256 amount);

    // ---------- Errors ----------

    error EventNotFound(uint256 eventId);
    error EventInactive(uint256 eventId);
    error SoldOut(uint256 eventId);
    error IncorrectPayment(uint256 required, uint256 provided);
    error TicketNotFound(uint256 ticketId);
    error NotTicketOwner(uint256 ticketId, address caller);
    error NotForSale(uint256 ticketId);
    error AlreadyListed(uint256 ticketId);
    error PriceTooHigh(uint256 requested, uint256 max);
    error InvalidRecipient();
    error EmptyName();
    error ZeroPrice();
    error ZeroSupply();
    error NothingToWithdraw();
    error TransferFailed();

    // ---------- Constructor ----------

    constructor() Ownable(msg.sender) {}

    // ---------- Admin (onlyOwner) ----------

    /// @notice Create a new event with a fixed face-value price and supply cap.
    function createEvent(
        string calldata name,
        uint256 pricePerTicket,
        uint256 maxSupply
    ) external onlyOwner returns (uint256 eventId) {
        if (bytes(name).length == 0) revert EmptyName();
        if (pricePerTicket == 0) revert ZeroPrice();
        if (maxSupply == 0) revert ZeroSupply();

        eventId = eventCount;
        _events[eventId] = EventInfo({
            name: name,
            pricePerTicket: pricePerTicket,
            maxSupply: maxSupply,
            sold: 0,
            active: true,
            exists: true
        });
        eventCount += 1;
        emit EventCreated(eventId, name, pricePerTicket, maxSupply);
    }

    /// @notice Pause or resume an event's primary sales.
    function setEventActive(uint256 eventId, bool active) external onlyOwner {
        EventInfo storage e = _requireEvent(eventId);
        e.active = active;
        emit EventStatusChanged(eventId, active);
    }

    /// @notice Withdraw protocol fees (and any accidental ETH) to the owner.
    function withdraw() external onlyOwner {
        uint256 balance = address(this).balance;
        if (balance == 0) revert NothingToWithdraw();
        (bool ok, ) = payable(owner()).call{value: balance}("");
        if (!ok) revert TransferFailed();
        emit Withdrawn(owner(), balance);
    }

    // ---------- Primary sale ----------

    /// @notice Buy one primary-sale ticket for `eventId`. Payment must equal
    ///         the face value; excess is refunded.
    function buyTicket(uint256 eventId) external payable returns (uint256 ticketId) {
        EventInfo storage e = _requireEvent(eventId);
        if (!e.active) revert EventInactive(eventId);
        if (e.sold >= e.maxSupply) revert SoldOut(eventId);
        if (msg.value < e.pricePerTicket) revert IncorrectPayment(e.pricePerTicket, msg.value);

        // Effects
        ticketId = ticketCount;
        _tickets[ticketId] = Ticket({
            eventId: eventId,
            owner: msg.sender,
            originalPrice: e.pricePerTicket,
            listingPrice: 0,
            forSale: false,
            exists: true
        });
        e.sold += 1;
        ticketCount += 1;

        emit TicketPurchased(ticketId, eventId, msg.sender, e.pricePerTicket);

        // Interaction: refund excess
        uint256 excess = msg.value - e.pricePerTicket;
        if (excess > 0) {
            (bool ok, ) = payable(msg.sender).call{value: excess}("");
            if (!ok) revert TransferFailed();
        }
    }

    // ---------- Transfer ----------

    /// @notice Transfer `ticketId` to `to`. The ticket must NOT be listed for
    ///         resale — the owner must `cancelResale` first, preventing a
    ///         race between a pending buyer and an off-market transfer.
    function transferTicket(uint256 ticketId, address to) external {
        if (to == address(0) || to == msg.sender) revert InvalidRecipient();
        Ticket storage t = _requireTicket(ticketId);
        if (t.owner != msg.sender) revert NotTicketOwner(ticketId, msg.sender);
        if (t.forSale) revert AlreadyListed(ticketId);

        address from = t.owner;
        t.owner = to;
        emit TicketTransferred(ticketId, from, to);
    }

    // ---------- Resale ----------

    /// @notice List an owned ticket for resale at `priceWei`. Capped at
    ///         MAX_RESALE_MULTIPLIER × the ticket's original face value.
    function listForResale(uint256 ticketId, uint256 priceWei) external {
        if (priceWei == 0) revert ZeroPrice();
        Ticket storage t = _requireTicket(ticketId);
        if (t.owner != msg.sender) revert NotTicketOwner(ticketId, msg.sender);
        if (t.forSale) revert AlreadyListed(ticketId);

        uint256 cap = t.originalPrice * MAX_RESALE_MULTIPLIER;
        if (priceWei > cap) revert PriceTooHigh(priceWei, cap);

        t.forSale = true;
        t.listingPrice = priceWei;
        emit TicketListed(ticketId, priceWei);
    }

    /// @notice Cancel a resale listing; callable only by the current owner.
    function cancelResale(uint256 ticketId) external {
        Ticket storage t = _requireTicket(ticketId);
        if (t.owner != msg.sender) revert NotTicketOwner(ticketId, msg.sender);
        if (!t.forSale) revert NotForSale(ticketId);

        t.forSale = false;
        t.listingPrice = 0;
        emit TicketUnlisted(ticketId);
    }

    /// @notice Buy a listed ticket. Must send exactly the listing price.
    ///         The listing price is paid to the seller minus RESALE_FEE_BPS.
    function buyResale(uint256 ticketId) external payable {
        Ticket storage t = _requireTicket(ticketId);
        if (!t.forSale) revert NotForSale(ticketId);
        if (t.owner == msg.sender) revert InvalidRecipient();
        if (msg.value < t.listingPrice) revert IncorrectPayment(t.listingPrice, msg.value);

        address seller = t.owner;
        uint256 price = t.listingPrice;
        uint256 fee = (price * RESALE_FEE_BPS) / RESALE_FEE_DENOM;
        uint256 proceeds = price - fee;

        // Effects
        t.owner = msg.sender;
        t.forSale = false;
        t.listingPrice = 0;

        emit TicketResold(ticketId, seller, msg.sender, price, proceeds, fee);

        // Interactions (CEI): pay seller, refund excess
        (bool okSeller, ) = payable(seller).call{value: proceeds}("");
        if (!okSeller) revert TransferFailed();

        uint256 excess = msg.value - price;
        if (excess > 0) {
            (bool okRefund, ) = payable(msg.sender).call{value: excess}("");
            if (!okRefund) revert TransferFailed();
        }
        // Protocol fee stays in contract balance.
    }

    // ---------- Views ----------

    function getEventInfo(uint256 eventId)
        external
        view
        returns (
            string memory name,
            uint256 pricePerTicket,
            uint256 maxSupply,
            uint256 sold,
            bool active
        )
    {
        EventInfo storage e = _events[eventId];
        if (!e.exists) revert EventNotFound(eventId);
        return (e.name, e.pricePerTicket, e.maxSupply, e.sold, e.active);
    }

    function getAllEvents()
        external
        view
        returns (
            uint256[] memory ids,
            string[] memory names,
            uint256[] memory prices,
            uint256[] memory supplies,
            uint256[] memory solds,
            bool[] memory actives
        )
    {
        uint256 n = eventCount;
        ids = new uint256[](n);
        names = new string[](n);
        prices = new uint256[](n);
        supplies = new uint256[](n);
        solds = new uint256[](n);
        actives = new bool[](n);
        for (uint256 i = 0; i < n; i++) {
            EventInfo storage e = _events[i];
            ids[i] = i;
            names[i] = e.name;
            prices[i] = e.pricePerTicket;
            supplies[i] = e.maxSupply;
            solds[i] = e.sold;
            actives[i] = e.active;
        }
    }

    function getTicket(uint256 ticketId)
        external
        view
        returns (
            uint256 eventId,
            address owner_,
            uint256 originalPrice,
            uint256 listingPrice,
            bool forSale
        )
    {
        Ticket storage t = _tickets[ticketId];
        if (!t.exists) revert TicketNotFound(ticketId);
        return (t.eventId, t.owner, t.originalPrice, t.listingPrice, t.forSale);
    }

    /// @notice Return ticket ids currently owned by `who`. O(ticketCount) scan;
    ///         cheap as a view. Callers paginate off-chain if needed.
    function ticketsOf(address who) external view returns (uint256[] memory) {
        uint256 n = ticketCount;
        uint256 count = 0;
        for (uint256 i = 0; i < n; i++) {
            if (_tickets[i].owner == who) count++;
        }
        uint256[] memory out = new uint256[](count);
        uint256 k = 0;
        for (uint256 i = 0; i < n; i++) {
            if (_tickets[i].owner == who) out[k++] = i;
        }
        return out;
    }

    /// @notice List tickets currently on the secondary market.
    function listedTickets() external view returns (uint256[] memory) {
        uint256 count = 0;
        for (uint256 i = 0; i < ticketCount; i++) {
            if (_tickets[i].forSale) count++;
        }
        uint256[] memory out = new uint256[](count);
        uint256 k = 0;
        for (uint256 i = 0; i < ticketCount; i++) {
            if (_tickets[i].forSale) out[k++] = i;
        }
        return out;
    }

    // ---------- Internals ----------

    function _requireEvent(uint256 eventId) private view returns (EventInfo storage e) {
        e = _events[eventId];
        if (!e.exists) revert EventNotFound(eventId);
    }

    function _requireTicket(uint256 ticketId) private view returns (Ticket storage t) {
        t = _tickets[ticketId];
        if (!t.exists) revert TicketNotFound(ticketId);
    }
}
