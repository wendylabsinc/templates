/**
 * Persistent Volume Example
 *
 * Demonstrates reading and writing to a persistent volume that survives container restarts.
 */

import * as fs from "fs/promises";
import * as path from "path";

const DATA_DIR = "/data";

async function main(): Promise<void> {
  console.log("Persistent Volume Example");
  console.log("=".repeat(40));

  // Ensure the data directory exists
  await fs.mkdir(DATA_DIR, { recursive: true });

  // Create and write to foo.md
  const fooPath = path.join(DATA_DIR, "foo.md");
  const content = `# Hello from Persistent Storage

This file was created by the persistent-volume example.

It will survive container restarts because it's stored
in a persistent volume mounted at \`/data\`.
`;

  console.log(`\nWriting to ${fooPath}...`);
  await fs.writeFile(fooPath, content, "utf-8");
  console.log("Done!");

  // Read and display foo.md
  console.log(`\nReading from ${fooPath}:`);
  console.log("-".repeat(40));
  const readContent = await fs.readFile(fooPath, "utf-8");
  console.log(readContent);
  console.log("-".repeat(40));

  // List all items in the persistent volume
  console.log(`\nContents of ${DATA_DIR}:`);
  const items = await fs.readdir(DATA_DIR, { withFileTypes: true });
  for (const item of items) {
    const itemType = item.isDirectory() ? "DIR " : "FILE";
    console.log(`  [${itemType}] ${item.name}`);
  }

  console.log("\nPersistent volume example complete!");
}

main().catch(console.error);
