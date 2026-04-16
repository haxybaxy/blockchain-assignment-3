import { ethers, artifacts } from "hardhat";
import * as fs from "fs";
import * as path from "path";

// Deploy EventTicketing, seed two events, and drop ABI + address into the
// web3 Flask app so it can connect on startup.
async function main() {
  const [deployer] = await ethers.getSigners();
  console.log(`Deploying EventTicketing from: ${deployer.address}`);

  const Factory = await ethers.getContractFactory("EventTicketing");
  const c = await Factory.deploy();
  await c.waitForDeployment();
  const address = await c.getAddress();
  console.log(`EventTicketing deployed at: ${address}`);

  // Seed two events
  const seeds = [
    { name: "Concert: Local Night", priceEth: "0.05", supply: 100 },
    { name: "Tech Conference 2026", priceEth: "0.08", supply: 50 },
  ];
  for (const s of seeds) {
    const tx = await c.createEvent(s.name, ethers.parseEther(s.priceEth), s.supply);
    await tx.wait();
    console.log(`  + event "${s.name}" @ ${s.priceEth} ETH × ${s.supply}`);
  }

  const artifact = await artifacts.readArtifact("EventTicketing");
  const targetDir = path.resolve(__dirname, "..", "app", "web3");
  if (!fs.existsSync(targetDir)) fs.mkdirSync(targetDir, { recursive: true });
  fs.writeFileSync(
    path.join(targetDir, "deployed.json"),
    JSON.stringify({ address, owner: deployer.address, rpcUrl: "http://127.0.0.1:8545", chainId: 1337 }, null, 2)
  );
  fs.writeFileSync(
    path.join(targetDir, "EventTicketing.abi.json"),
    JSON.stringify(artifact.abi, null, 2)
  );
  console.log(`Wrote app/web3/deployed.json and app/web3/EventTicketing.abi.json`);
}

main().catch((err) => {
  console.error(err);
  process.exit(1);
});
