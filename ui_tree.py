"""
UI Tree Inspector for Desktop Automation
=========================================
Extracts and serializes UI Automation trees strictly up to depth 1:
- Root Desktop -> All open & minimized application windows (depth 1) without going inside them.
- Target Window (e.g. Chrome, Notepad) -> All top-level UI components/tabs/toolbars (depth 1) without going inside tab contents.

Designed for TypeSafe AI's Jev model & Desktop Automation.
"""

import sys
import json
import argparse
from typing import List, Dict, Any, Optional
import uiautomation as auto

try:
    import win32gui
    import win32process
    import win32con
    HAS_WIN32 = True
except ImportError:
    HAS_WIN32 = False


def get_supported_patterns(control: auto.Control) -> List[str]:
    """Inspect and return names of supported patterns for a control."""
    pattern_names = []
    # Common UIAutomation patterns
    patterns = [
        ("InvokePattern", auto.PatternId.InvokePattern),
        ("ValuePattern", auto.PatternId.ValuePattern),
        ("TextPattern", auto.PatternId.TextPattern),
        ("TogglePattern", auto.PatternId.TogglePattern),
        ("SelectionItemPattern", auto.PatternId.SelectionItemPattern),
        ("SelectionPattern", auto.PatternId.SelectionPattern),
        ("ExpandCollapsePattern", auto.PatternId.ExpandCollapsePattern),
        ("ScrollPattern", auto.PatternId.ScrollPattern),
        ("WindowPattern", auto.PatternId.WindowPattern),
        ("TransformPattern", auto.PatternId.TransformPattern),
    ]
    for name, pid in patterns:
        try:
            pat = control.GetPattern(pid)
            if pat is not None:
                pattern_names.append(name)
        except Exception:
            pass
    return pattern_names


def extract_node_data(control: auto.Control, index: int = 0, depth: int = 0) -> Dict[str, Any]:
    """Extract structured metadata from a UIAutomation control."""
    rect = control.BoundingRectangle
    rect_dict = {
        "left": rect.left if rect else 0,
        "top": rect.top if rect else 0,
        "right": rect.right if rect else 0,
        "bottom": rect.bottom if rect else 0,
        "width": rect.width() if rect else 0,
        "height": rect.height() if rect else 0,
    }
    
    # State flags
    try:
        is_offscreen = control.IsOffscreen
    except Exception:
        is_offscreen = False

    try:
        is_enabled = control.IsEnabled
    except Exception:
        is_enabled = True

    try:
        patterns = get_supported_patterns(control)
    except Exception:
        patterns = []

    return {
        "id": index,
        "depth": depth,
        "control_type": control.ControlTypeName,
        "name": control.Name or "",
        "automation_id": control.AutomationId or "",
        "class_name": control.ClassName or "",
        "handle": hex(control.NativeWindowHandle) if control.NativeWindowHandle else "0x0",
        "rect": rect_dict,
        "is_offscreen": is_offscreen,
        "is_enabled": is_enabled,
        "patterns": patterns,
        "children": []
    }


def get_top_level_windows_win32() -> List[auto.Control]:
    """Fallback using Win32 API to find all visible and minimized top-level windows."""
    if not HAS_WIN32:
        return []

    windows = []
    def enum_handler(hwnd, _):
        if not win32gui.IsWindow(hwnd):
            return
        # Filter invisible windows or zero-sized helper windows
        is_visible = win32gui.IsWindowVisible(hwnd)
        title = win32gui.GetWindowText(hwnd).strip()
        
        # Keep windows with a title or visible non-tool windows
        if is_visible and title:
            # Skip Program Manager / task switcher shell windows if desired
            if title not in ["Program Manager"]:
                try:
                    ctrl = auto.ControlFromHandle(hwnd)
                    if ctrl:
                        windows.append(ctrl)
                except Exception:
                    pass

    win32gui.EnumWindows(enum_handler, None)
    return windows


def get_component_tree(
    root_control: auto.Control,
    max_depth: int = 1,
    include_hidden: bool = False
) -> Dict[str, Any]:
    """
    Builds a UI tree starting from root_control up to max_depth (default 1).
    
    - Depth 0: The root_control itself.
    - Depth 1: The direct children of root_control (e.g. apps for Desktop, or tabs/bars for Chrome).
    Does NOT descend deeper than max_depth.
    """
    root_data = extract_node_data(root_control, index=0, depth=0)
    
    if max_depth < 1:
        return root_data

    # Check if target is desktop root
    is_desktop_root = (
        root_control.ControlTypeName == "PaneControl" and 
        (root_control.NativeWindowHandle == auto.GetRootControl().NativeWindowHandle or root_control.Name == "Desktop 1")
    )

    children = []
    try:
        children = root_control.GetChildren()
    except Exception:
        children = []

    # If it's desktop root and GetChildren() returned empty, use Win32 fallback
    if is_desktop_root and not children and HAS_WIN32:
        children = get_top_level_windows_win32()

    child_nodes = []
    idx = 1
    for child in children:
        try:
            # Check visibility filtering if requested
            if not include_hidden:
                rect = child.BoundingRectangle
                # Ignore controls with 0x0 size unless they are valid minimized windows
                if rect and rect.width() == 0 and rect.height() == 0 and not child.ControlTypeName == "WindowControl":
                    continue

            node_data = extract_node_data(child, index=idx, depth=1)
            child_nodes.append(node_data)
            idx += 1
        except Exception:
            continue

    root_data["children"] = child_nodes
    return root_data


def format_tree_text(tree: Dict[str, Any]) -> str:
    """Format the UI tree into a human-readable and Jev-friendly micro-tree format."""
    lines = []
    
    def format_node(node: Dict[str, Any], prefix: str = "", is_last: bool = True):
        depth = node.get("depth", 0)
        idx = node.get("id", 0)
        ctype = node.get("control_type", "Control")
        name = node.get("name", "")
        name_str = f'"{name}"' if name else '""'
        auto_id = node.get("automation_id", "")
        auto_id_str = f" auto_id: '{auto_id}'" if auto_id else ""
        offscreen_str = " [Minimized/Offscreen]" if node.get("is_offscreen") else ""
        
        rect = node.get("rect", {})
        rect_str = f" [{rect.get('width', 0)}x{rect.get('height', 0)}]" if rect else ""
        
        patterns = node.get("patterns", [])
        patterns_str = f" patterns: [{', '.join(patterns[:3])}]" if patterns else ""

        if depth == 0:
            lines.append(f"[#{idx}] (ROOT) {ctype} {name_str}{auto_id_str}{rect_str}{offscreen_str}")
        else:
            branch = "└── " if is_last else "├── "
            lines.append(f"{prefix}{branch}[#{idx}] {ctype} {name_str}{auto_id_str}{rect_str}{patterns_str}{offscreen_str}")

        children = node.get("children", [])
        count = len(children)
        for i, child in enumerate(children):
            new_prefix = prefix + ("    " if is_last else "│   ")
            format_node(child, prefix=new_prefix, is_last=(i == count - 1))

    format_node(tree)
    return "\n".join(lines)


def find_target_window(window_query: str) -> Optional[auto.Control]:
    """Find a window by exact or partial title match."""
    # First attempt: search via UIAutomation WindowControl
    w = auto.WindowControl(searchDepth=1, SubName=window_query)
    if w.Exists(maxSearchSeconds=1):
        return w

    # Second attempt: Search top-level win32 windows
    if HAS_WIN32:
        found_hwnd = None
        def enum_win(hwnd, _):
            nonlocal found_hwnd
            if win32gui.IsWindowVisible(hwnd) or win32gui.IsIconic(hwnd):
                title = win32gui.GetWindowText(hwnd)
                if window_query.lower() in title.lower():
                    found_hwnd = hwnd
        win32gui.EnumWindows(enum_win, None)
        if found_hwnd:
            return auto.ControlFromHandle(found_hwnd)

    return None


def main():
    parser = argparse.ArgumentParser(
        description="Extract UI Tree for Desktop Automation (Depth <= 1)"
    )
    parser.add_argument(
        "-w", "--window", 
        type=str, 
        help="Target window title or substring (e.g. 'Chrome', 'Notepad'). If omitted, defaults to Root Desktop."
    )
    parser.add_argument(
        "-a", "--active", 
        action="store_true", 
        help="Target the currently active / focused foreground window."
    )
    parser.add_argument(
        "--depth", 
        type=int, 
        default=1, 
        help="Max depth of the tree (default: 1)."
    )
    parser.add_argument(
        "--json", 
        action="store_true", 
        help="Output raw JSON instead of formatted text."
    )
    parser.add_argument(
        "--include-hidden", 
        action="store_true", 
        help="Include zero-area / hidden controls."
    )
    parser.add_argument(
        "-o", "--output", 
        type=str, 
        help="Save output to the specified file path."
    )

    args = parser.parse_args()

    # Determine target control
    target_control = None
    target_description = "Root Desktop (All Applications)"

    if args.active:
        focused = auto.GetFocusedControl()
        if focused:
            target_control = focused.GetTopLevelControl()
            target_description = f"Active Window: '{target_control.Name}'"
        else:
            print("Error: Could not determine active window.", file=sys.stderr)
            sys.exit(1)
    elif args.window:
        target_control = find_target_window(args.window)
        if not target_control:
            print(f"Error: Window matching '{args.window}' was not found.", file=sys.stderr)
            sys.exit(1)
        target_description = f"Target Window: '{target_control.Name}'"
    else:
        target_control = auto.GetRootControl()

    # Extract Depth-1 Tree
    tree_data = get_component_tree(
        root_control=target_control,
        max_depth=args.depth,
        include_hidden=args.include_hidden
    )

    # Format output
    if args.json:
        result_text = json.dumps(tree_data, indent=2)
    else:
        header = f"=" * 60 + f"\nUI TREE (Max Depth: {args.depth}) - {target_description}\n" + f"=" * 60
        result_text = f"{header}\n{format_tree_text(tree_data)}\n" + f"=" * 60

    print(result_text)

    # Optionally save to file
    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            f.write(result_text)
        print(f"\nTree saved to: {args.output}")


if __name__ == "__main__":
    main()
