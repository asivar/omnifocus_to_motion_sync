#!/bin/bash
# Setup script for OmniFocus to Motion Sync

set -e  # Exit on error

echo "🔧 Setting up OmniFocus to Motion Sync..."

# Check Python version
echo "📍 Checking Python version..."
python3 --version

# Create virtual environment if it doesn't exist
if [ ! -d "venv" ]; then
    echo "📦 Creating virtual environment..."
    python3 -m venv venv
else
    echo "✅ Virtual environment already exists"
fi

# Activate virtual environment
echo "🔌 Activating virtual environment..."
source venv/bin/activate

# Install dependencies
echo "📦 Installing Python dependencies..."
pip install -r requirements.txt

# Check if MOTION_API_KEY is set
if [ -z "$MOTION_API_KEY" ]; then
    echo ""
    echo "⚠️  MOTION_API_KEY environment variable not set!"
    echo "   Add this to your ~/.zshrc or ~/.bash_profile:"
    echo "   export MOTION_API_KEY='your_api_key_here'"
    echo "   Then run: source ~/.zshrc"
else
    echo "✅ MOTION_API_KEY is configured"
fi

echo ""
echo "✅ Setup complete!"
echo ""
echo "To run the sync:"
echo "  1. Activate virtual environment: source venv/bin/activate"
echo "  2. Run script: python3 sync_of_to_motion.py --refresh-mapping"
echo "  3. Deactivate when done: deactivate"
