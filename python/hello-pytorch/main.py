#!/usr/bin/env python3
"""
PyTorch GPU/CPU availability checker
Polls every 2 seconds to check if GPU or CPU is available
"""

import time
import torch
from datetime import datetime


def check_device_availability():
    """Check and display PyTorch device availability"""
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Check CUDA availability
    cuda_available = torch.cuda.is_available()

    # Check MPS (Apple Silicon) availability
    mps_available = torch.backends.mps.is_available() if hasattr(torch.backends, 'mps') else False

    # Get device count
    device_count = torch.cuda.device_count() if cuda_available else 0

    print(f"\n[{timestamp}]")
    print(f"  CUDA (GPU) Available: {cuda_available}")

    if cuda_available:
        print(f"  GPU Device Count: {device_count}")
        for i in range(device_count):
            print(f"    - Device {i}: {torch.cuda.get_device_name(i)}")

    print(f"  MPS (Apple GPU) Available: {mps_available}")
    print(f"  CPU Available: True")
    print(f"  PyTorch Version: {torch.__version__}")


def main():
    """Main loop to poll device availability"""
    print("=" * 60)
    print("PyTorch Device Availability Checker")
    print("Polling every 2 seconds... (Press Ctrl+C to stop)")
    print("=" * 60)

    try:
        while True:
            check_device_availability()
            time.sleep(2)
    except KeyboardInterrupt:
        print("\n\nStopping device availability checker...")
        print("Goodbye!")


if __name__ == "__main__":
    main()
