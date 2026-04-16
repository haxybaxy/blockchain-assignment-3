import { expect } from "chai";
import { ethers } from "hardhat";
import { EventTicketing } from "../typechain-types";

const ONE_ETH = ethers.parseEther("1");

async function deploy(): Promise<EventTicketing> {
  const Factory = await ethers.getContractFactory("EventTicketing");
  const c = await Factory.deploy();
  await c.waitForDeployment();
  return c as unknown as EventTicketing;
}

async function seedTwoEvents(c: EventTicketing) {
  // Two events as required
  await c.createEvent("Concert A", ethers.parseEther("0.1"), 2); // small supply to test sold out
  await c.createEvent("Conference B", ethers.parseEther("0.05"), 10);
}

describe("EventTicketing", () => {
  // 1
  describe("admin: createEvent", () => {
    it("lets the owner create an event and emits EventCreated", async () => {
      const c = await deploy();
      await expect(c.createEvent("Concert A", ethers.parseEther("0.1"), 5))
        .to.emit(c, "EventCreated")
        .withArgs(0n, "Concert A", ethers.parseEther("0.1"), 5n);

      const [name, price, supply, sold, active] = await c.getEventInfo(0);
      expect(name).to.equal("Concert A");
      expect(price).to.equal(ethers.parseEther("0.1"));
      expect(supply).to.equal(5n);
      expect(sold).to.equal(0n);
      expect(active).to.equal(true);
    });

    // 2 — permission failure on createEvent
    it("reverts when a non-owner tries to create an event (permission failure)", async () => {
      const c = await deploy();
      const [, nonOwner] = await ethers.getSigners();
      await expect(
        c.connect(nonOwner).createEvent("Hack", ONE_ETH, 1)
      ).to.be.revertedWithCustomError(c, "OwnableUnauthorizedAccount");
    });
  });

  // 3
  describe("buyTicket (primary sale)", () => {
    it("mints a ticket to the buyer, increments sold, emits TicketPurchased (successful purchase)", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, buyer] = await ethers.getSigners();
      const price = ethers.parseEther("0.1");

      await expect(c.connect(buyer).buyTicket(0, { value: price }))
        .to.emit(c, "TicketPurchased")
        .withArgs(0n, 0n, buyer.address, price);

      const [, , , sold] = await c.getEventInfo(0);
      expect(sold).to.equal(1n);

      const [eventId, owner, originalPrice, listingPrice, forSale] = await c.getTicket(0);
      expect(eventId).to.equal(0n);
      expect(owner).to.equal(buyer.address);
      expect(originalPrice).to.equal(price);
      expect(listingPrice).to.equal(0n);
      expect(forSale).to.equal(false);

      const owned = await c.ticketsOf(buyer.address);
      expect(owned.map((x: bigint) => Number(x))).to.deep.equal([0]);
    });

    // 4 — sold out
    it("reverts with SoldOut when the event is full", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, b1, b2, b3] = await ethers.getSigners();
      const price = ethers.parseEther("0.1");

      await c.connect(b1).buyTicket(0, { value: price });
      await c.connect(b2).buyTicket(0, { value: price });
      await expect(c.connect(b3).buyTicket(0, { value: price })).to.be.revertedWithCustomError(c, "SoldOut");
    });

    // 5 — incorrect payment (failed purchase)
    it("reverts with IncorrectPayment when the buyer underpays (failed purchase)", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, buyer] = await ethers.getSigners();
      await expect(
        c.connect(buyer).buyTicket(0, { value: ethers.parseEther("0.01") })
      ).to.be.revertedWithCustomError(c, "IncorrectPayment");
    });

    it("reverts when the event is paused", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      await c.setEventActive(0, false);
      const [, buyer] = await ethers.getSigners();
      await expect(
        c.connect(buyer).buyTicket(0, { value: ethers.parseEther("0.1") })
      ).to.be.revertedWithCustomError(c, "EventInactive");
    });

    it("refunds excess ETH when the buyer overpays", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, buyer] = await ethers.getSigners();
      const price = ethers.parseEther("0.1");
      const overpay = ethers.parseEther("0.3");

      const balBefore = await ethers.provider.getBalance(buyer.address);
      const tx = await c.connect(buyer).buyTicket(0, { value: price + overpay });
      const receipt = await tx.wait();
      const gas = receipt!.fee;
      const balAfter = await ethers.provider.getBalance(buyer.address);

      expect(balBefore - balAfter).to.equal(price + gas);
    });
  });

  // 6 — transfer happy path
  describe("transferTicket", () => {
    it("transfers a ticket to a new owner (successful transfer)", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, alice, bob] = await ethers.getSigners();
      const price = ethers.parseEther("0.1");
      await c.connect(alice).buyTicket(0, { value: price });

      await expect(c.connect(alice).transferTicket(0, bob.address))
        .to.emit(c, "TicketTransferred")
        .withArgs(0n, alice.address, bob.address);

      const [, newOwner] = await c.getTicket(0);
      expect(newOwner).to.equal(bob.address);
      expect(await c.ticketsOf(bob.address)).to.have.lengthOf(1);
      expect(await c.ticketsOf(alice.address)).to.have.lengthOf(0);
    });

    // 7 — transfer by non-owner
    it("reverts when a non-owner tries to transfer (failed transfer)", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, alice, bob, eve] = await ethers.getSigners();
      await c.connect(alice).buyTicket(0, { value: ethers.parseEther("0.1") });
      await expect(
        c.connect(eve).transferTicket(0, bob.address)
      ).to.be.revertedWithCustomError(c, "NotTicketOwner");
    });
  });

  // 8 — resale happy path
  describe("resale: list, buy, cancel", () => {
    it("allows the owner to list and another user to buy, with proceeds and fee accounted", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, alice, bob] = await ethers.getSigners();
      const price = ethers.parseEther("0.1");

      // Alice buys primary, lists at 1.5× face
      await c.connect(alice).buyTicket(0, { value: price });
      const listPrice = ethers.parseEther("0.15");
      await expect(c.connect(alice).listForResale(0, listPrice))
        .to.emit(c, "TicketListed")
        .withArgs(0n, listPrice);

      // Bob buys the resale
      const aliceBalBefore = await ethers.provider.getBalance(alice.address);
      const feeExpected = (listPrice * 200n) / 10000n;
      const proceedsExpected = listPrice - feeExpected;

      await expect(c.connect(bob).buyResale(0, { value: listPrice }))
        .to.emit(c, "TicketResold")
        .withArgs(0n, alice.address, bob.address, listPrice, proceedsExpected, feeExpected);

      // Ownership flipped
      const [, newOwner, , listingPrice, forSale] = await c.getTicket(0);
      expect(newOwner).to.equal(bob.address);
      expect(listingPrice).to.equal(0n);
      expect(forSale).to.equal(false);

      // Alice received proceeds
      const aliceBalAfter = await ethers.provider.getBalance(alice.address);
      expect(aliceBalAfter - aliceBalBefore).to.equal(proceedsExpected);

      // Contract retained the fee
      expect(await ethers.provider.getBalance(await c.getAddress())).to.equal(price + feeExpected);
    });

    // 9 — buyResale when not listed
    it("reverts buyResale when the ticket is not listed", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, alice, bob] = await ethers.getSigners();
      await c.connect(alice).buyTicket(0, { value: ethers.parseEther("0.1") });
      await expect(
        c.connect(bob).buyResale(0, { value: ethers.parseEther("0.1") })
      ).to.be.revertedWithCustomError(c, "NotForSale");
    });

    // 10 — transfer while listed (invalid state transition)
    it("reverts transferTicket while the ticket is listed for resale", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, alice, bob] = await ethers.getSigners();
      await c.connect(alice).buyTicket(0, { value: ethers.parseEther("0.1") });
      await c.connect(alice).listForResale(0, ethers.parseEther("0.12"));
      await expect(
        c.connect(alice).transferTicket(0, bob.address)
      ).to.be.revertedWithCustomError(c, "AlreadyListed");
    });

    // 11 — price cap
    it("reverts listForResale above the 2× face-value cap (invalid input)", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, alice] = await ethers.getSigners();
      await c.connect(alice).buyTicket(0, { value: ethers.parseEther("0.1") });
      const tooHigh = ethers.parseEther("0.3"); // > 2 × 0.1
      await expect(
        c.connect(alice).listForResale(0, tooHigh)
      ).to.be.revertedWithCustomError(c, "PriceTooHigh");
    });

    it("allows the seller to cancel a listing", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, alice] = await ethers.getSigners();
      await c.connect(alice).buyTicket(0, { value: ethers.parseEther("0.1") });
      await c.connect(alice).listForResale(0, ethers.parseEther("0.12"));
      await expect(c.connect(alice).cancelResale(0)).to.emit(c, "TicketUnlisted").withArgs(0n);
      const [, , , , forSale] = await c.getTicket(0);
      expect(forSale).to.equal(false);
    });
  });

  // 12 — final ownership across a sequence
  describe("integration: final ownership after a sequence", () => {
    it("tracks ownership correctly through buy → transfer → resale", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [, alice, bob, carol] = await ethers.getSigners();
      const face = ethers.parseEther("0.1");

      // Alice buys
      await c.connect(alice).buyTicket(0, { value: face });
      let [, owner] = await c.getTicket(0);
      expect(owner).to.equal(alice.address);

      // Alice transfers to Bob
      await c.connect(alice).transferTicket(0, bob.address);
      [, owner] = await c.getTicket(0);
      expect(owner).to.equal(bob.address);

      // Bob lists and Carol buys via resale
      const listPrice = ethers.parseEther("0.12");
      await c.connect(bob).listForResale(0, listPrice);
      await c.connect(carol).buyResale(0, { value: listPrice });
      [, owner] = await c.getTicket(0);
      expect(owner).to.equal(carol.address);

      // ticketsOf returns only current holdings
      expect(await c.ticketsOf(alice.address)).to.have.lengthOf(0);
      expect(await c.ticketsOf(bob.address)).to.have.lengthOf(0);
      const carolTickets = await c.ticketsOf(carol.address);
      expect(carolTickets.map(Number)).to.deep.equal([0]);
    });
  });

  describe("admin: withdraw", () => {
    it("withdraws accumulated protocol fees to owner only", async () => {
      const c = await deploy();
      await seedTwoEvents(c);
      const [owner, alice, bob] = await ethers.getSigners();
      const face = ethers.parseEther("0.1");
      const listPrice = ethers.parseEther("0.15");

      await c.connect(alice).buyTicket(0, { value: face });
      await c.connect(alice).listForResale(0, listPrice);
      await c.connect(bob).buyResale(0, { value: listPrice });

      const fee = (listPrice * 200n) / 10000n;
      const contractBal = await ethers.provider.getBalance(await c.getAddress());
      expect(contractBal).to.equal(face + fee);

      await expect(c.connect(alice).withdraw()).to.be.revertedWithCustomError(c, "OwnableUnauthorizedAccount");

      const ownerBefore = await ethers.provider.getBalance(owner.address);
      const tx = await c.withdraw();
      const receipt = await tx.wait();
      const gas = receipt!.fee;
      const ownerAfter = await ethers.provider.getBalance(owner.address);

      expect(ownerAfter - ownerBefore + gas).to.equal(contractBal);
      expect(await ethers.provider.getBalance(await c.getAddress())).to.equal(0n);
    });
  });
});
