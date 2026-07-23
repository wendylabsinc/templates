#!/bin/bash
# Quick setup and run script for Mac

# Don't exit on error - we handle errors manually
set +e

echo "Setting up Wendy Voice Assistant for Mac..."

# Check if dependencies are installed
if ! command -v brew &> /dev/null; then
    echo "Homebrew not found. Installing dependencies manually..."
    echo "Please install: portaudio, ffmpeg"
else
    echo "Installing system dependencies..."
    brew install portaudio ffmpeg || true
fi

# Check for compatible Python version (3.11 or 3.12 recommended)
PYTHON_CMD="python3"
if command -v python3.12 &> /dev/null; then
    PYTHON_CMD="python3.12"
    echo "Using Python 3.12 (recommended for compatibility)"
elif command -v python3.11 &> /dev/null; then
    PYTHON_CMD="python3.11"
    echo "Using Python 3.11 (recommended for compatibility)"
elif command -v python3.13 &> /dev/null; then
    PYTHON_CMD="python3.13"
    echo "Using Python 3.13"
else
    PYTHON_VERSION=$($PYTHON_CMD --version 2>&1 | cut -d' ' -f2 | cut -d'.' -f1,2)
    if [ "$(echo "$PYTHON_VERSION >= 3.14" | bc 2>/dev/null || echo "0")" = "1" ]; then
        echo "Warning: Python 3.14+ may have compatibility issues with some packages."
        echo "Consider installing Python 3.12: brew install python@3.12"
    fi
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Remove existing venv if it was created with incompatible Python version
CURRENT_PYTHON=$(python3 --version 2>&1 | cut -d' ' -f2)
if [ -d "venv" ] && [ "$PYTHON_CMD" != "python3" ]; then
    # Check if we should recreate venv with better Python version
    if [ "$CURRENT_PYTHON" = "3.14.0" ] || [ "$CURRENT_PYTHON" = "3.14"* ]; then
        echo "Removing existing venv (created with Python 3.14) to use $PYTHON_CMD..."
        rm -rf venv
    fi
fi

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "Creating virtual environment with $PYTHON_CMD..."
    $PYTHON_CMD -m venv venv
fi

# Activate virtual environment
echo "Activating virtual environment..."
source venv/bin/activate

# Upgrade pip
echo "Upgrading pip..."
pip install --upgrade pip setuptools wheel

# Install Python dependencies
echo "Installing Python dependencies..."
echo "Note: This may take several minutes, especially for PyTorch and Whisper..."

# Install onnxruntime first (required by openwakeword)
echo "Installing onnxruntime (required for openwakeword)..."
pip install "onnxruntime>=1.10.0,<2.0.0" || {
    echo "Warning: onnxruntime installation failed. Trying alternative..."
    pip install onnxruntime || echo "onnxruntime may need to be installed manually"
}

# Install remaining dependencies
pip install -r requirements.txt
INSTALL_STATUS=$?

if [ $INSTALL_STATUS -ne 0 ]; then
    echo ""
    echo "Error: Some packages failed to install."
    CURRENT_PYTHON=$(python3 --version 2>&1 | cut -d' ' -f2)
    if [[ "$CURRENT_PYTHON" == "3.14"* ]]; then
        echo "Python 3.14 has compatibility issues with some packages."
        echo "Installing Python 3.12 for better compatibility..."
        brew install python@3.12 || {
            echo "Failed to install Python 3.12. Please install manually:"
            echo "  brew install python@3.12"
            echo "Then run this script again."
            exit 1
        }
        echo "Please run this script again to use Python 3.12"
        exit 1
    else
        echo "Please check the error messages above and try again."
        exit 1
    fi
fi

# Create directories
mkdir -p models voices data

# Run
echo ""
echo "Starting Wendy Voice Assistant..."
echo "Press Ctrl+C to stop"
echo ""
python3 wendy_assistant.py