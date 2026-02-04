#!/usr/bin/env bash

set -e

# Create temporary directory
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

# Clone the repository
echo "Cloning waystt repository..."
git clone https://github.com/sevos/waystt "$TEMP_DIR"

# Build the application
echo "Building waystt..."
cd "$TEMP_DIR"
if [[ "$LIBVA_DRIVER_NAME" == "nvidia" ]]; then
  echo "Building with CUDA support..."
  cargo build --release --features whisper-rs/cuda
else
  echo "Building with Vulkan support..."
  cargo build --release --features whisper-rs/vulkan
fi

# Move the binary to ~/.local/bin
echo "Installing waystt to ~/.local/bin..."
mv target/release/waystt ~/.local/bin/

echo "Installation complete! waystt is now available at ~/.local/bin/waystt"

echo "Downloading default model..."
waystt --download-model
