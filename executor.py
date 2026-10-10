"""
Executor – Automation Execution Loop
=====================================
Bridges Jev decisions to real UI Automation actions.

Loop:
  1. Capture / receive a UI tree snapshot.
  2. Ask Jev for the next decision (target, action, next_tree, goal_complete).
  3. Execute the chosen action on the chosen target element.
  4. Fetch the next UI tree according to Jev's next_ui_tree choice.
  5. Repeat until is_goal_complete is True or MAX_STEPS is reached.
"""

import os
import sys
import time
import json
import logging
from typing import Dict, Any, Optional

import uiautomation as auto

# Disable uiautomation's default file logging (@AutomationLog.txt)
auto.Logger.SetLogFile("")

# ---------------------------------------------------------------------------
# Local imports
# ---------------------------------------------------------------------------
_current_dir = os.path.dirname(os.path.abspath(__file__))
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

from ui_tree import (
    get_component_tree,
    format_tree_text,
    find_target_window,
)
from jev import JevClient, build_candidate_options

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
MAX_STEPS = 30                # Safety limit – avoid infinite loops
STEP_DELAY_SECONDS = 0.6      # Pause between steps so the UI can settle
UI_SETTLE_DELAY = 0.4          # Extra wait after certain actions (click, set_text, …)

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("executor")


# ============================================================================
# Action Executors
# ============================================================================

def _resolve_control(target_node: Optional[Dict[str, Any]]) -> Optional[auto.Control]:
    """
    Turn a target_node dict (from Jev's decision) back into a live
    UIA Control by matching its cached control object, native window handle,
    or searching by AutomationId/Name.
    """
    if target_node is None:
        return None

    # 1. Cached live control object from tree extraction (fastest & most accurate)
    ctrl = target_node.get("_control")
    if ctrl is not None:
        try:
            if ctrl.Exists(maxSearchSeconds=0.5):
                return ctrl
        except Exception:
            pass

    # 2. Match native window handle (e.g. for top-level windows)
    handle_hex = target_node.get("handle", "0x0")
    ctype = target_node.get("control_type", "")
    if handle_hex and handle_hex != "0x0":
        hwnd = int(handle_hex, 16)
        try:
            ctrl = auto.ControlFromHandle(hwnd)
            if ctrl and ctrl.Exists(maxSearchSeconds=0.5):
                if not ctype or ctrl.ControlTypeName == ctype:
                    return ctrl
        except Exception:
            pass

    # 3. Search by AutomationId
    auto_id = target_node.get("automation_id", "")
    if auto_id:
        try:
            ctrl = auto.Control(searchDepth=6, AutomationId=auto_id)
            if ctrl.Exists(maxSearchSeconds=0.5):
                return ctrl
        except Exception:
            pass

    # 4. Search by Name
    name = target_node.get("name", "")
    if name:
        try:
            ctrl = auto.Control(searchDepth=6, Name=name)
            if ctrl.Exists(maxSearchSeconds=0.5):
                return ctrl
        except Exception:
            pass

    return None


def execute_action(action: str, target_node: Optional[Dict[str, Any]], action_params: Optional[Dict[str, Any]] = None) -> bool:
    """
    Perform *action* on the UI element described by *target_node*.

    Returns True if the action was executed successfully, False otherwise.
    """
    params = action_params or {}
    ctrl = _resolve_control(target_node)

    if action == "finish_task":
        log.info("🏁 Action: finish_task – goal is complete.")
        return True

    if action == "wait_for_ui":
        wait_sec = params.get("wait_seconds", 2)
        log.info(f"⏳ Action: wait_for_ui – sleeping {wait_sec}s")
        time.sleep(wait_sec)
        return True

    if ctrl is None:
        log.warning(f"⚠ Could not resolve control for target: {target_node}")
        return False

    try:
        if action == "focus_window":
            log.info(f"🔲 Action: focus_window → '{ctrl.Name}'")
            # Try SetForeground first; fall back to clicking the control
            try:
                if ctrl.ControlType == auto.ControlType.WindowControl:
                    if auto.IsIconic(ctrl.NativeWindowHandle):
                        ctrl.ShowWindow(auto.SW.ShowNormal)
                        time.sleep(0.3)
                    ctrl.SetActive()
                    time.sleep(0.3)
                    return True
            except Exception as e:
                print(e)

            try:
                ctrl.SetFocus()
            
            except Exception as e:
                print(e)
            
            try:
                ctrl.Click(simulateMove=True)
            
            except Exception as e:
                print(e)
            
            time.sleep(UI_SETTLE_DELAY)
            return True

        elif action == "click":
            log.info(f"🖱 Action: click → '{ctrl.Name}'")
            try:
                invoke = ctrl.GetInvokePattern()
                if invoke:
                    invoke.Invoke()
                    time.sleep(UI_SETTLE_DELAY)
                    return True
            except Exception:
                pass
            ctrl.Click(simulateMove=False)
            time.sleep(UI_SETTLE_DELAY)
            return True

        elif action == "double_click":
            log.info(f"🖱🖱 Action: double_click → '{ctrl.Name}'")
            ctrl.DoubleClick(simulateMove=False)
            time.sleep(UI_SETTLE_DELAY)
            return True

        elif action == "set_text":
            text = params.get("text", "")
            log.info(f"⌨ Action: set_text → '{ctrl.Name}' text='{text}'")
            try:
                value_pattern = ctrl.GetValuePattern()
                if value_pattern:
                    value_pattern.SetValue(text)
                    time.sleep(UI_SETTLE_DELAY)
                    return True
            except Exception:
                pass
            # Fallback: click then type
            ctrl.Click(simulateMove=False)
            time.sleep(0.2)
            auto.SendKeys(text, interval=0.02)
            time.sleep(UI_SETTLE_DELAY)
            return True


        elif action == "toggle":
            log.info(f"☑ Action: toggle → '{ctrl.Name}'")
            try:
                toggle_pattern = ctrl.GetTogglePattern()
                if toggle_pattern:
                    toggle_pattern.Toggle()
                    time.sleep(UI_SETTLE_DELAY)
                    return True
            except Exception:
                pass
            # Fallback to click
            ctrl.Click(simulateMove=False)
            time.sleep(UI_SETTLE_DELAY)
            return True

        else:
            log.warning(f"❓ Unknown action '{action}'. Skipping.")
            return False

    except Exception as exc:
        log.error(f"❌ Action '{action}' on '{ctrl.Name if ctrl else '?'}' failed: {exc}")
        return False


# ============================================================================
# Next UI Tree Resolver
# ============================================================================

def fetch_next_ui_tree(
    next_tree_action: str,
    target_node: Optional[Dict[str, Any]],
    current_root_control: Optional[auto.Control] = None,
    depth: int = 1,
) -> tuple:
    """
    Retrieve the next UI tree based on Jev's next_tree_action choice.

    Returns:
        (ui_tree_dict, root_control)  – the new tree and the live control it
        was captured from.
    """
    if next_tree_action == "inspect_selected_window":
        ctrl = _resolve_control(target_node)
        if ctrl is None:
            log.warning("Cannot inspect selected window – target not found. Falling back to desktop.")
            ctrl = auto.GetRootControl()
        else:
            # If the control is not a top-level window, walk up to find it
            try:
                top = ctrl.GetTopLevelControl()
                if top:
                    ctrl = top
            except Exception:
                pass
        log.info(f"🔍 Next tree: inspect_selected_window → '{ctrl.Name}'")
        tree = get_component_tree(ctrl, max_depth=depth)
        return tree, ctrl

    elif next_tree_action == "refresh_current_window":
        ctrl = current_root_control or auto.GetRootControl()
        log.info(f"🔄 Next tree: refresh_current_window → '{ctrl.Name}'")
        tree = get_component_tree(ctrl, max_depth=depth)
        return tree, ctrl

    elif next_tree_action == "inspect_desktop":
        ctrl = auto.GetRootControl()
        log.info("🖥 Next tree: inspect_desktop → Root Desktop")
        tree = get_component_tree(ctrl, max_depth=depth)
        return tree, ctrl

    else:  # "none" or unknown
        log.info("⏹ Next tree: none – keeping current tree.")
        if current_root_control:
            tree = get_component_tree(current_root_control, max_depth=depth)
            return tree, current_root_control
        ctrl = auto.GetRootControl()
        tree = get_component_tree(ctrl, max_depth=depth)
        return tree, ctrl


# ============================================================================
# Main Execution Loop
# ============================================================================

def run_automation(
    user_prompt: str,
    window_name: Optional[str] = None,
    system_prompt: Optional[str] = None,
    api_key: Optional[str] = None,
    max_steps: int = MAX_STEPS,
    depth: int = 1,
    verbose: bool = True,
) -> Dict[str, Any]:
    """
    Autonomous Desktop Automation loop.

    1. Captures the initial UI tree (optionally scoped to *window_name*).
    2. Sends tree + user_prompt to Jev.
    3. Executes the action Jev selects.
    4. Fetches the next UI tree based on Jev's choice.
    5. Repeats until Jev says is_goal_complete == True or *max_steps* exhausted.

    Returns a summary dict with all steps taken and the final status.
    """
    log.info("=" * 60)
    log.info(f"🚀 Starting automation: \"{user_prompt}\"")
    log.info(f"   Max steps: {max_steps} | Depth: {depth}")
    log.info("=" * 60)

    # --- Jev client ---
    client = JevClient(api_key=api_key, system_prompt=system_prompt)

    # --- Initial UI tree ---
    if window_name:
        root_control = find_target_window(window_name)
        if root_control is None:
            log.warning(f"Window '{window_name}' not found. Using Root Desktop.")
            root_control = auto.GetRootControl()
    else:
        root_control = auto.GetRootControl()

    ui_tree = get_component_tree(root_control, max_depth=depth)

    step_history: list = []
    goal_complete = False

    for step in range(1, max_steps + 1):
        log.info(f"\n{'─' * 50}")
        log.info(f"📍 Step {step}/{max_steps}")
        log.info(f"{'─' * 50}")

        # Structured display of the UI Tree being sent to Jev
        tree_text = format_tree_text(ui_tree) if ui_tree else "(empty tree)"
        log.info(f"🌳 UI Tree (Sending to Jev):\n{tree_text}\n")

        # ----- 1. Ask Jev for a decision -----
        try:
            result = client.decide(user_prompt=user_prompt, ui_tree=ui_tree)
        except Exception as exc:
            log.error(f"Jev decision failed: {exc}")
            step_history.append({"step": step, "error": str(exc)})
            break

        # Handle failed API call
        if isinstance(result, str):
            log.error(f"Jev returned an error string: {result}")
            step_history.append({"step": step, "error": result})
            break

        decision = result.get("decision", {})
        target_node = result.get("target_node")

        target_id = decision.get("target_id", "NONE")
        action = decision.get("action", "wait_for_ui")
        next_tree_action = decision.get("next_ui_tree", "none")
        goal_complete = decision.get("is_goal_complete", False)

        target_name = (target_node.get("name") or "").strip() if isinstance(target_node, dict) else ""
        if target_name:
            target_display = f"{target_id} ('{target_name}')"
        elif isinstance(target_node, dict) and target_node.get("control_type"):
            target_display = f"{target_id} ({target_node.get('control_type')})"
        else:
            target_display = target_id

        log.info(f"  ➤ Target   : {target_display}")
        log.info(f"  ➤ Action   : {action}")
        log.info(f"  ➤ Next Tree: {next_tree_action}")
        log.info(f"  ➤ Goal Done: {goal_complete}  (noul={result.get('noul_probability', '?')})")

        step_record = {
            "step": step,
            "target_id": target_id,
            "target_name": target_name,
            "action": action,
            "next_ui_tree": next_tree_action,
            "is_goal_complete": goal_complete,
            "noul_probability": result.get("noul_probability"),
        }

        # ----- 2. Check for early completion -----
        if goal_complete or action == "finish_task":
            log.info("✅ Goal is complete!")
            step_record["executed"] = True
            step_history.append(step_record)
            goal_complete = True
            break

        # ----- 3. Execute the action -----
        success = execute_action(action, target_node)
        step_record["executed"] = success
        step_history.append(step_record)

        if not success:
            log.warning(f"Action '{action}' was not successful. Continuing anyway…")

        # ----- 4. Brief pause to let the UI settle -----
        time.sleep(STEP_DELAY_SECONDS)

        # ----- 5. Fetch the next UI tree -----
        ui_tree, root_control = fetch_next_ui_tree(
            next_tree_action=next_tree_action,
            target_node=target_node,
            current_root_control=root_control,
            depth=depth,
        )



    # --- End of loop ---
    if not goal_complete:
        log.warning(f"⚠ Reached max steps ({max_steps}) without completing the goal.")

    summary = {
        "user_prompt": user_prompt,
        "total_steps": len(step_history),
        "goal_complete": goal_complete,
        "steps": step_history,
    }

    log.info("\n" + "=" * 60)
    log.info(f"🏁 Automation finished — {len(step_history)} steps, goal_complete={goal_complete}")
    log.info("=" * 60)

    return summary


# ============================================================================
# CLI Entry Point
# ============================================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(
        description="Desktop Automation Executor – runs Jev in a loop until the goal is complete."
    )
    parser.add_argument(
        "-p", "--prompt",
        type=str,
        required=True,
        help="User automation goal (e.g. 'Open Notepad and type Hello World')."
    )
    parser.add_argument(
        "-w", "--window",
        type=str,
        default=None,
        help="Optional initial target window (e.g. 'Notepad'). Defaults to Root Desktop."
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=MAX_STEPS,
        help=f"Maximum automation steps before stopping (default: {MAX_STEPS})."
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Max depth for UI tree snapshots (default: 1)."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output the full step-history as JSON when finished."
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        default=True,
        help="Verbose per-step logging (default: True)."
    )

    args = parser.parse_args()

    summary = run_automation(
        user_prompt=args.prompt,
        window_name=args.window,
        max_steps=args.max_steps,
        depth=args.depth,
        verbose=args.verbose,
    )

    if args.json:
        print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
