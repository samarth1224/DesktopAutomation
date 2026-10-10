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

# Disable uiautomation's default file logging (@AutomationLog.txt)
auto.Logger.SetLogFile("")

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


def serialize_tree_json(node: Dict[str, Any]) -> Dict[str, Any]:
    """Strip internal non-serializable fields like _control before JSON output."""
    clean = {k: v for k, v in node.items() if not k.startswith("_")}
    clean["children"] = [serialize_tree_json(c) for c in node.get("children", [])]
    return clean


def is_wrapper_pane(ctrl: auto.Control, root_name: str = "") -> bool:
    """
    Returns True if ctrl is purely an intermediate layout/wrapper container
    with no interactive patterns, no automation ID, and either no name or
    a name that just mirrors the root window title.
    """
    try:
        ctype = ctrl.ControlTypeName
        if ctype not in ("PaneControl", "CustomControl", "GroupControl"):
            return False

        # If it has an automation id, it may be a specific named component
        auto_id = ctrl.AutomationId or ""
        if auto_id:
            return False

        # If it has interactive patterns, it's not a dumb wrapper
        patterns = get_supported_patterns(ctrl)
        interactive_patterns = {
            "InvokePattern", "ValuePattern", "TogglePattern", 
            "SelectionItemPattern", "SelectionPattern", "ExpandCollapsePattern"
        }
        if any(p in interactive_patterns for p in patterns):
            return False

        name = (ctrl.Name or "").strip()
        # If it has a distinct meaningful name (different from window title), keep it
        if name and name != root_name.strip():
            return False

        return True
    except Exception:
        return False


def get_meaningful_children(ctrl: auto.Control, root_name: str = "", max_unwrap: int = 8) -> List[auto.Control]:
    """
    Fetches children of ctrl, transparently bypassing and unwrapping
    pure intermediate wrapper panes (like Chromium/Electron RootView,
    NonClientView, ClientView, etc.).
    """
    try:
        raw_children = ctrl.GetChildren()
    except Exception:
        return []

    if max_unwrap <= 0:
        return raw_children

    meaningful = []
    for child in raw_children:
        try:
            # Check bounding rectangle: skip 0x0 offscreen elements
            rect = child.BoundingRectangle
            if rect and rect.width() == 0 and rect.height() == 0 and child.ControlTypeName != "WindowControl":
                continue

            # If it's a wrapper pane, see if we should unwrap it
            if is_wrapper_pane(child, root_name):
                grand_children = get_meaningful_children(child, root_name, max_unwrap - 1)
                if grand_children:
                    meaningful.extend(grand_children)
                # If it had 0 children and was a pure wrapper pane, it's pruned
            else:
                meaningful.append(child)
        except Exception:
            continue

    return meaningful


def get_component_tree(
    root_control: auto.Control,
    max_depth: Optional[int] = None,
    include_hidden: bool = False
) -> Dict[str, Any]:
    """
    Builds a UI tree starting from root_control up to max_depth.
    
    - Root Desktop -> Top-level application windows (depth 1).
    - Application Window (e.g. Chrome, Antigravity IDE, Notepad) -> Unwraps
      pure layout/wrapper panes and extracts interactive components (tabs,
      toolbars, menu items, address bar, new tab/close buttons) down to depth 5
      without recursing into web page document contents.
    """
    is_desktop_root = (
        root_control.ControlTypeName == "PaneControl" and 
        (root_control.NativeWindowHandle == auto.GetRootControl().NativeWindowHandle or root_control.Name == "Desktop 1")
    )
    root_name = (root_control.Name or "").strip()

    if max_depth is None:
        effective_depth = 1 if is_desktop_root else 5
    else:
        # For non-desktop application windows, allow at least depth 4-5 so controls are reached
        if not is_desktop_root and max_depth <= 1:
            effective_depth = 5
        else:
            effective_depth = max(max_depth, 4) if not is_desktop_root else max_depth

    root_data = extract_node_data(root_control, index=0, depth=0)
    root_data["_control"] = root_control

    if effective_depth < 1:
        return root_data

    idx_counter = [1]

    # Special handling for desktop root: direct children are top-level windows
    if is_desktop_root:
        try:
            children = root_control.GetChildren()
        except Exception:
            children = []

        if not children and HAS_WIN32:
            children = get_top_level_windows_win32()

        child_nodes = []
        for child in children:
            try:
                if not include_hidden:
                    rect = child.BoundingRectangle
                    if rect and rect.width() == 0 and rect.height() == 0 and child.ControlTypeName != "WindowControl":
                        continue
                child_data = extract_node_data(child, index=idx_counter[0], depth=1)
                child_data["_control"] = child
                idx_counter[0] += 1
                child_nodes.append(child_data)
            except Exception:
                continue

        root_data["children"] = child_nodes
        return root_data

    # For application windows: recursive extraction to reach buttons, tabs, toolbars
    def build_subtree(ctrl: auto.Control, current_depth: int) -> Optional[Dict[str, Any]]:
        # Visibility check
        if not include_hidden:
            try:
                rect = ctrl.BoundingRectangle
                if rect and rect.width() == 0 and rect.height() == 0 and ctrl.ControlTypeName != "WindowControl":
                    return None
            except Exception:
                pass

        node_data = extract_node_data(ctrl, index=idx_counter[0], depth=current_depth)
        node_data["_control"] = ctrl
        idx_counter[0] += 1

        ctype = ctrl.ControlTypeName
        # Stop conditions for recursion:
        # 1. Reached max depth
        # 2. DocumentControl (web page contents - do not explode DOM elements)
        # 3. Terminal leaf controls like buttons, edits, etc.
        if (
            current_depth >= effective_depth
            or ctype == "DocumentControl"
            or ctype in ("ButtonControl", "EditControl", "CheckBoxControl", "RadioButtonControl", "HyperlinkControl")
        ):
            return node_data

        meaningful_children = get_meaningful_children(ctrl, root_name=root_name)

        child_list = []
        for c in meaningful_children[:40]:
            try:
                sub_node = build_subtree(c, current_depth + 1)
                if sub_node is not None:
                    child_list.append(sub_node)
            except Exception:
                continue

        node_data["children"] = child_list
        return node_data

    # Fetch top-level meaningful children of the window (bypassing dummy wrappers)
    top_children = get_meaningful_children(root_control, root_name=root_name)

    child_nodes = []
    for c in top_children[:40]:
        try:
            sub = build_subtree(c, current_depth=1)
            if sub is not None:
                child_nodes.append(sub)
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
        result_text = json.dumps(serialize_tree_json(tree_data), indent=2)
    else:
        header = f"=" * 60 + f"\nUI TREE (Max Depth: {args.depth}) - {target_description}\n" + f"=" * 60
        result_text = f"{header}\n{format_tree_text(tree_data)}\n" + f"=" * 60

    print(result_text)


if __name__ == "__main__":
    main()
