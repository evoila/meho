#!/bin/bash
# Swap prompt variant for testing
# Usage: ./scripts/swap_prompt.sh [a|b|c|d|e|current]

VARIANT=${1:-help}
PROMPTS_DIR="config/prompts"
VARIANTS_DIR="$PROMPTS_DIR/variants"
BASE_PROMPT="$PROMPTS_DIR/base_system_prompt.md"
BACKUP="$PROMPTS_DIR/base_system_prompt.md.original"

case $VARIANT in
    a|A)
        VARIANT_FILE="$VARIANTS_DIR/variant_a_minimal.md"
        VARIANT_NAME="Minimal (31 lines)"
        ;;
    b|B)
        VARIANT_FILE="$VARIANTS_DIR/variant_b_structured.md"
        VARIANT_NAME="Structured (95 lines)"
        ;;
    c|C)
        VARIANT_FILE="$VARIANTS_DIR/variant_c_conversational.md"
        VARIANT_NAME="Conversational (55 lines)"
        ;;
    d|D)
        VARIANT_FILE="$VARIANTS_DIR/variant_d_rules.md"
        VARIANT_NAME="Rule-Based (101 lines)"
        ;;
    e|E)
        VARIANT_FILE="$VARIANTS_DIR/variant_e_tool_centric.md"
        VARIANT_NAME="Tool-Centric (176 lines)"
        ;;
    current|original|restore)
        if [ -f "$BACKUP" ]; then
            cp "$BACKUP" "$BASE_PROMPT"
            echo "✅ Restored original prompt (324 lines)"
            exit 0
        else
            echo "❌ No backup found. Cannot restore."
            exit 1
        fi
        ;;
    help|-h|--help|*)
        echo "📝 Prompt Variant Swapper"
        echo ""
        echo "Usage: ./scripts/swap_prompt.sh [variant]"
        echo ""
        echo "Variants:"
        echo "  a        - Minimal (31 lines, -90%)"
        echo "  b        - Structured (95 lines, -71%)"
        echo "  c        - Conversational (55 lines, -83%)"
        echo "  d        - Rule-Based (101 lines, -69%)"
        echo "  e        - Tool-Centric (176 lines, -46%)"
        echo "  current  - Restore original (324 lines)"
        echo ""
        echo "After swapping, rebuild the API:"
        echo "  docker-compose -f docker-compose.dev.yml up -d --build meho-api"
        exit 0
        ;;
esac

# Create backup if doesn't exist
if [ ! -f "$BACKUP" ]; then
    cp "$BASE_PROMPT" "$BACKUP"
    echo "📦 Created backup: $BACKUP"
fi

# Check variant exists
if [ ! -f "$VARIANT_FILE" ]; then
    echo "❌ Variant file not found: $VARIANT_FILE"
    exit 1
fi

# Swap prompt
cp "$VARIANT_FILE" "$BASE_PROMPT"
LINES=$(wc -l < "$BASE_PROMPT")
echo "✅ Swapped to Variant ${VARIANT^^}: $VARIANT_NAME"
echo "   Lines: $LINES"
echo ""
echo "🔄 Rebuild to apply:"
echo "   docker-compose -f docker-compose.dev.yml up -d --build meho-api"

