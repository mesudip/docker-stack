# -*- coding: utf-8 -*-

markers = {
    "Color": {
        "Circles": {
            "Red": "🔴",
            "Orange": "🟠",
            "Yellow": "🟡",
            "Green": "🟢",
            "Blue": "🔵",
            "Purple": "🟣",
            "Black": "⚫",
            "White": "⚪",
        },
        "Squares": {
            "Red": "🟥",
            "Orange": "🟧",
            "Yellow": "🟨",
            "Green": "🟩",
            "Blue": "🟦",
            "Purple": "🟪",
            "Black": "⬛",
            "White": "⬜",
        },
        "Stars": {
            "Yellow": "⭐",
            "Glowing": "🌟",
            "Sparkles": "✨",
            "Shooting": "💫",
        },
        "Misc": {
            "Check": "✅",
            "Cross": "❌",
            "Warning": "⚠️",
            "Fire": "🔥",
            "Explosion": "💥",
            "Heart Red": "❤️",
            "Heart Blue": "💙",
            "Heart Green": "💚",
            "Heart Yellow": "💛",
            "Heart Purple": "💜",
        }
    },
    "CombiningMarks": {
        "Underline": "\u0332",          # a̲
        "DoubleUnderline": "\u0333",    # a̳
        "Overline": "\u0305",            # a̅
        "DoubleOverline": "\u033F",      # a̿
        "StrikeThrough": "\u0336",       # a̶
        "ShortStrike": "\u0335",         # a̵
        "DotAbove": "\u0307",            # ȧ
        "DotBelow": "\u0323",            # ạ
        "CircleEnclose": "\u20DD",       # a⃝
        "SquareEnclose": "\u20DE",       # a⃞
        "DiamondEnclose": "\u20DF",      # a⃟
        "KeycapEnclose": "\u20E3",       # a⃣
        "SlashOverlay": "\u0338",        # a̸
    }
}

# Example usage
# Function to apply a combining mark to each character in a string
def apply_mark(text, mark):
    return ''.join(ch + mark for ch in text)

# Print full table
print("=== Color Markers ===")
for group, items in markers["Color"].items():
    print(f"\n{group}:")
    for name, symbol in items.items():
        print(f"  {name:<10} {symbol}")

print("\n=== Combining Marks (example applied to 'A') ===")
for name, mark in markers["CombiningMarks"].items():
    example = "A" + mark
    print(f"  {name:<15} {example}")

print("\n=== Combining Marks Applied to Word 'TEST' ===")
for name, mark in markers["CombiningMarks"].items():
    marked_word = apply_mark("TEST", mark)
    print(f"  {name:<15} {marked_word}")