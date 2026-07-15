/**
 * Persistent Volume Example
 *
 * Demonstrates reading and writing to a persistent volume that survives container restarts.
 */

#include <iostream>
#include <fstream>
#include <filesystem>
#include <string>

namespace fs = std::filesystem;

const std::string DATA_DIR = "/data";

int main() {
    std::cout << "Persistent Volume Example" << std::endl;
    std::cout << std::string(40, '=') << std::endl;

    fs::path data_path(DATA_DIR);

    // Ensure the data directory exists
    fs::create_directories(data_path);

    // Create and write to foo.md
    fs::path foo_path = data_path / "foo.md";
    std::string content = R"(# Hello from Persistent Storage

This file was created by the persistent-volume example.

It will survive container restarts because it's stored
in a persistent volume mounted at `/data`.
)";

    std::cout << "\nWriting to " << foo_path << "..." << std::endl;
    {
        std::ofstream file(foo_path);
        file << content;
    }
    std::cout << "Done!" << std::endl;

    // Read and display foo.md
    std::cout << "\nReading from " << foo_path << ":" << std::endl;
    std::cout << std::string(40, '-') << std::endl;
    {
        std::ifstream file(foo_path);
        std::string line;
        while (std::getline(file, line)) {
            std::cout << line << std::endl;
        }
    }
    std::cout << std::string(40, '-') << std::endl;

    // List all items in the persistent volume
    std::cout << "\nContents of " << DATA_DIR << ":" << std::endl;
    for (const auto& entry : fs::directory_iterator(data_path)) {
        std::string item_type = entry.is_directory() ? "DIR " : "FILE";
        std::cout << "  [" << item_type << "] " << entry.path().filename().string() << std::endl;
    }

    std::cout << "\nPersistent volume example complete!" << std::endl;

    return 0;
}
