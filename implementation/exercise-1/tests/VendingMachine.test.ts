import { expect } from "chai";
import { ethers } from "hardhat";
import { VendingMachine } from "../typechain-types";

const ONE_ETHER = ethers.parseEther("1");
const HALF_ETHER = ethers.parseEther("0.5");

async function deploy(): Promise<VendingMachine> {
  const Factory = await ethers.getContractFactory("VendingMachine");
  const vm = await Factory.deploy();
  await vm.waitForDeployment();
  return vm as unknown as VendingMachine;
}

async function seedProducts(vm: VendingMachine) {
  // Three products as required by the assignment
  await vm.addProduct("Soda", ethers.parseEther("0.01"), 10);
  await vm.addProduct("Chips", ethers.parseEther("0.02"), 5);
  await vm.addProduct("Candy", ethers.parseEther("0.005"), 20);
}

describe("VendingMachine", () => {
  describe("admin: addProduct", () => {
    it("lets the owner add a product and emits ProductAdded", async () => {
      const vm = await deploy();
      await expect(vm.addProduct("Soda", ONE_ETHER, 3))
        .to.emit(vm, "ProductAdded")
        .withArgs(0n, "Soda", ONE_ETHER, 3n);

      const [name, price, stock, active] = await vm.getProduct(0);
      expect(name).to.equal("Soda");
      expect(price).to.equal(ONE_ETHER);
      expect(stock).to.equal(3n);
      expect(active).to.equal(true);
    });

    it("rejects products with empty name or zero price", async () => {
      const vm = await deploy();
      await expect(vm.addProduct("", ONE_ETHER, 1)).to.be.revertedWithCustomError(vm, "EmptyName");
      await expect(vm.addProduct("X", 0, 1)).to.be.revertedWithCustomError(vm, "ZeroPrice");
    });
  });

  describe("purchase: happy paths and state changes", () => {
    it("allows a user to purchase, decrements stock, records receipt, updates ownership", async () => {
      const vm = await deploy();
      await seedProducts(vm);

      const [, buyer] = await ethers.getSigners();
      const price = ethers.parseEther("0.01");

      // Purchase 2 Soda units (productId = 0)
      await expect(vm.connect(buyer).purchase(0, 2, { value: price * 2n }))
        .to.emit(vm, "ProductPurchased")
        .withArgs(0n, 0n, buyer.address, 2n, price * 2n);

      // Stock decremented
      const [, , stock] = await vm.getProduct(0);
      expect(stock).to.equal(8n);

      // Ownership tracked
      expect(await vm.balanceOf(buyer.address, 0)).to.equal(2n);

      // Receipt recorded
      const receiptIds = await vm.getReceiptIdsOf(buyer.address);
      expect(receiptIds.length).to.equal(1);
      const [productId, buyerAddr, qty, unitPrice] = await vm.getReceipt(receiptIds[0]);
      expect(productId).to.equal(0n);
      expect(buyerAddr).to.equal(buyer.address);
      expect(qty).to.equal(2n);
      expect(unitPrice).to.equal(price);

      // Contract balance equals total paid (no excess here)
      const contractBal = await ethers.provider.getBalance(await vm.getAddress());
      expect(contractBal).to.equal(price * 2n);
    });

    it("refunds excess ETH sent by the buyer", async () => {
      const vm = await deploy();
      await seedProducts(vm);

      const [, buyer] = await ethers.getSigners();
      const price = ethers.parseEther("0.01");
      const qty = 1n;
      const overpay = ethers.parseEther("0.5");

      const balBefore = await ethers.provider.getBalance(buyer.address);
      const tx = await vm.connect(buyer).purchase(0, qty, { value: price * qty + overpay });
      const receipt = await tx.wait();
      const gasCost = receipt!.fee;
      const balAfter = await ethers.provider.getBalance(buyer.address);

      // Net cost should be exactly price * qty + gas, not the overpay
      const netSpent = balBefore - balAfter;
      expect(netSpent).to.equal(price * qty + gasCost);

      // Contract balance should equal only what was owed
      expect(await ethers.provider.getBalance(await vm.getAddress())).to.equal(price * qty);
    });

    it("accumulates ownership across multiple purchases", async () => {
      const vm = await deploy();
      await seedProducts(vm);

      const [, buyer] = await ethers.getSigners();
      const price = ethers.parseEther("0.01");
      await vm.connect(buyer).purchase(0, 1, { value: price });
      await vm.connect(buyer).purchase(0, 3, { value: price * 3n });

      expect(await vm.balanceOf(buyer.address, 0)).to.equal(4n);
      expect((await vm.getReceiptIdsOf(buyer.address)).length).to.equal(2);
      const [, , stock] = await vm.getProduct(0);
      expect(stock).to.equal(6n);
    });
  });

  describe("purchase: validation & reverts", () => {
    it("reverts with InsufficientPayment when msg.value is too low", async () => {
      const vm = await deploy();
      await seedProducts(vm);
      const [, buyer] = await ethers.getSigners();
      const price = ethers.parseEther("0.01");
      await expect(
        vm.connect(buyer).purchase(0, 2, { value: price })
      ).to.be.revertedWithCustomError(vm, "InsufficientPayment");
    });

    it("reverts with InsufficientStock when quantity exceeds available", async () => {
      const vm = await deploy();
      await seedProducts(vm);
      const [, buyer] = await ethers.getSigners();
      const price = ethers.parseEther("0.02"); // Chips
      await expect(
        vm.connect(buyer).purchase(1, 99, { value: price * 99n })
      ).to.be.revertedWithCustomError(vm, "InsufficientStock");
    });

    it("reverts when trying to purchase an inactive product", async () => {
      const vm = await deploy();
      await seedProducts(vm);
      await vm.setProductActive(0, false);
      const [, buyer] = await ethers.getSigners();
      await expect(
        vm.connect(buyer).purchase(0, 1, { value: HALF_ETHER })
      ).to.be.revertedWithCustomError(vm, "ProductInactive");
    });

    it("reverts with ProductNotFound for a non-existent product id", async () => {
      const vm = await deploy();
      const [, buyer] = await ethers.getSigners();
      await expect(
        vm.connect(buyer).purchase(42, 1, { value: HALF_ETHER })
      ).to.be.revertedWithCustomError(vm, "ProductNotFound");
    });
  });

  describe("access control: admin-only functions", () => {
    it("reverts when a non-owner tries to restock", async () => {
      const vm = await deploy();
      await seedProducts(vm);
      const [, nonOwner] = await ethers.getSigners();
      await expect(
        vm.connect(nonOwner).restockProduct(0, 5)
      ).to.be.revertedWithCustomError(vm, "OwnableUnauthorizedAccount");
    });

    it("reverts when a non-owner tries to addProduct or updatePrice", async () => {
      const vm = await deploy();
      const [, nonOwner] = await ethers.getSigners();
      await expect(
        vm.connect(nonOwner).addProduct("Hack", ONE_ETHER, 1)
      ).to.be.revertedWithCustomError(vm, "OwnableUnauthorizedAccount");
      await vm.addProduct("Soda", ONE_ETHER, 1);
      await expect(
        vm.connect(nonOwner).updatePrice(0, HALF_ETHER)
      ).to.be.revertedWithCustomError(vm, "OwnableUnauthorizedAccount");
    });
  });

  describe("admin: restockProduct, updatePrice, withdraw", () => {
    it("restocks and updates price correctly with events", async () => {
      const vm = await deploy();
      await seedProducts(vm);

      await expect(vm.restockProduct(0, 5))
        .to.emit(vm, "ProductRestocked")
        .withArgs(0n, 5n, 15n);

      const newPrice = ethers.parseEther("0.05");
      await expect(vm.updatePrice(0, newPrice))
        .to.emit(vm, "ProductPriceUpdated")
        .withArgs(0n, ethers.parseEther("0.01"), newPrice);

      const [, price, stock] = await vm.getProduct(0);
      expect(price).to.equal(newPrice);
      expect(stock).to.equal(15n);
    });

    it("withdraws the contract balance to the owner only", async () => {
      const vm = await deploy();
      await seedProducts(vm);

      const [owner, buyer] = await ethers.getSigners();
      const price = ethers.parseEther("0.01");
      await vm.connect(buyer).purchase(0, 3, { value: price * 3n });

      const contractBal = await ethers.provider.getBalance(await vm.getAddress());
      expect(contractBal).to.equal(price * 3n);

      // Non-owner cannot withdraw
      await expect(vm.connect(buyer).withdraw()).to.be.revertedWithCustomError(
        vm,
        "OwnableUnauthorizedAccount"
      );

      // Owner receives the balance
      const ownerBalBefore = await ethers.provider.getBalance(owner.address);
      const tx = await vm.withdraw();
      const receipt = await tx.wait();
      const gasCost = receipt!.fee;
      const ownerBalAfter = await ethers.provider.getBalance(owner.address);

      expect(ownerBalAfter - ownerBalBefore + gasCost).to.equal(contractBal);
      expect(await ethers.provider.getBalance(await vm.getAddress())).to.equal(0n);
    });
  });
});
