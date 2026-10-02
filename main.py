"""
Desktop Automation Entry Point
==============================
Step 1: UI Tree Inspector with depth limit (Depth <= 1).
"""

import sys
from ui_tree import get_component_tree, format_tree_text, find_target_window, auto


def inspect_component(target_name: str = None, depth: int = 1):
    """
    Inspects a UI component up to depth 1.
    If target_name is None, inspects the Root Desktop (all open/minimized apps).
    Otherwise, inspects the window matching target_name (e.g. 'Chrome', 'Notepad').
    """
    if target_name:
        control = find_target_window(target_name)
        if not control:
            print(f"[-] Could not find window matching '{target_name}'. Falling back to Root.")
            control = auto.GetRootControl()
            target_name = "Root Desktop"
    else:
        control = auto.GetRootControl()
        target_name = "Root Desktop (All Applications)"

    print(f"\n[+] Extracting UI Tree for: {target_name} (Max Depth: {depth})")
    tree = get_component_tree(control, max_depth=depth)
    print(format_tree_text(tree))
    return tree


if __name__ == "__main__":
    # If a window name is passed as command-line argument, use it; otherwise inspect Root Desktop
    target = sys.argv[1] if len(sys.argv) > 1 else None
    inspect_component(target, depth=1)
