import { ethers, artifacts } from "hardhat";
import * as fs from "fs";
import * as path from "path";

// Deploys the VendingMachine, seeds three products, and writes deployment
// metadata + ABI to files that the Python Flask app reads on startup.
async function main() {
  const [deployer] = await ethers.getSigners();
  console.log(`Deploying VendingMachine from: ${deployer.address}`);

  const Factory = await ethers.getContractFactory("VendingMachine");
  const vm = await Factory.deploy();
  await vm.waitForDeployment();
  const address = await vm.getAddress();
  console.log(`VendingMachine deployed at: ${address}`);

  // Seed catalog
  const seeds: Array<{ name: string; priceEth: string; stock: number }> = [
    { name: "Soda", priceEth: "0.01", stock: 10 },
    { name: "Chips", priceEth: "0.02", stock: 5 },
    { name: "Candy Bar", priceEth: "0.005", stock: 20 },
    { name: "Water Bottle", priceEth: "0.008", stock: 15 },
  ];
  for (const s of seeds) {
    const tx = await vm.addProduct(s.name, ethers.parseEther(s.priceEth), s.stock);
    await tx.wait();
    console.log(`  + added ${s.name} @ ${s.priceEth} ETH, stock ${s.stock}`);
  }

  // Persist deployment info for the Flask client
  const artifact = await artifacts.readArtifact("VendingMachine");
  const appDir = path.resolve(__dirname, "..", "app");
  if (!fs.existsSync(appDir)) fs.mkdirSync(appDir, { recursive: true });

  const info = {
    address,
    owner: deployer.address,
    rpcUrl: "http://127.0.0.1:8545",
    chainId: 1337,
  };
  fs.writeFileSync(
    path.join(appDir, "deployed.json"),
    JSON.stringify(info, null, 2)
  );
  fs.writeFileSync(
    path.join(appDir, "VendingMachine.abi.json"),
    JSON.stringify(artifact.abi, null, 2)
  );
  console.log(`Wrote app/deployed.json and app/VendingMachine.abi.json`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
