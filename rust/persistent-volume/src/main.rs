//! Persistent Volume Example
//!
//! Demonstrates reading and writing to a persistent volume that survives container restarts.

use std::fs;
use std::path::Path;

const DATA_DIR: &str = "/data";

fn main() {
    println!("Persistent Volume Example");
    println!("{}", "=".repeat(40));

    let data_path = Path::new(DATA_DIR);

    // Ensure the data directory exists
    fs::create_dir_all(data_path).expect("Failed to create data directory");

    // Create and write to foo.md
    let foo_path = data_path.join("foo.md");
    let content = r#"# Hello from Persistent Storage

This file was created by the persistent-volume example.

It will survive container restarts because it's stored
in a persistent volume mounted at `/data`.
"#;

    println!("\nWriting to {}...", foo_path.display());
    fs::write(&foo_path, content).expect("Failed to write foo.md");
    println!("Done!");

    // Read and display foo.md
    println!("\nReading from {}:", foo_path.display());
    println!("{}", "-".repeat(40));
    let read_content = fs::read_to_string(&foo_path).expect("Failed to read foo.md");
    print!("{}", read_content);
    println!("{}", "-".repeat(40));

    // List all items in the persistent volume
    println!("\nContents of {}:", DATA_DIR);
    let entries = fs::read_dir(data_path).expect("Failed to read data directory");
    for entry in entries {
        let entry = entry.expect("Failed to read directory entry");
        let item_type = if entry.file_type().map(|t| t.is_dir()).unwrap_or(false) {
            "DIR "
        } else {
            "FILE"
        };
        println!("  [{}] {}", item_type, entry.file_name().to_string_lossy());
    }

    println!("\nPersistent volume example complete!");
}
