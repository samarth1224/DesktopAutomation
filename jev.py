"""
Jev Decision Engine for Desktop Automation
===========================================
Integrates TypeSafe AI's Jev model with the Desktop Automation UI Tree.

Responsibilities:
1. Accepts the UI Tree (compact depth <= 1 micro-tree) along with the user prompt
   and a specialized desktop automation system prompt.
2. Directs Jev to select probabilities for:
   - Target Window / Element Selection (which window to focus or control to interact with)
   - Action to Execute (focus, click, type/set_text, toggle, wait, finish)
   - Next UI Tree Decision (which UI tree representation Jev wants next: drill into window, refresh, return to desktop, or none)
   - Goal Completion Probability (Noul: whether the task is complete)
3. Supports the live TypeSafe AI SDK and reports typed failures when it is unavailable.
"""

import os
import sys
import json
import argparse
from enum import Enum
from typing import Dict, Any, Optional, Tuple
from dotenv import load_dotenv

load_dotenv()

from pydantic import BaseModel, Field, ConfigDict, ValidationError

# Ensure current script directory is in sys.path for local module imports
_current_dir = os.path.dirname(os.path.abspath(__file__))
if _current_dir not in sys.path:
    sys.path.insert(0, _current_dir)

try:
    from ui_tree import (
        get_component_tree,
        format_tree_text,
        find_target_window,
        auto,
        HAS_WIN32
    )
except ImportError:
    try:
        from .ui_tree import (
            get_component_tree,
            format_tree_text,
            find_target_window,
            auto,
            HAS_WIN32
        )
    except Exception as e:
        raise ImportError(f"Could not import ui_tree module: {e}")

# Try importing official TypeSafe SDK
from typesafe_sdk import TypeSafeClient, Choice, Noul


# ============================================================================
# System Prompts & Instructions
# ============================================================================

DEFAULT_SYSTEM_PROMPT = """You are Jev, an ultra-fast System One decision engine for Windows Desktop Automation.
You are responsible for automating the Windows Desktop by:
1. Analyzing the provided UI Tree snapshot (at depth <= 1) and the user's high-level goal.
2. Outputting probabilities for WHICH WINDOW OR UI ELEMENT to select or interact with next (by predefined candidate ID).
3. Outputting probabilities for WHAT ACTION to execute on the selected target from predefined action choices (focus_window, click, double_click, set_text, toggle, wait_for_ui, finish_task).
4. Deciding WHAT UI TREE representation you want next from predefined tree choices (inspect_selected_window, refresh_current_window, inspect_desktop, none).
5. Estimating the goal completion probability (Noul) indicating whether the user's goal has been fully achieved.
6. Using the ACTION HISTORY (if provided) to understand what has already been done, avoid repeating failed actions, and decide the next logical step toward completing the goal.

All outputs are strictly predefined discrete choices and probabilities. You do NOT generate free text."""


# ============================================================================
# Predefined Choice Enums (Jev Decision Primitives)
# ============================================================================

class ActionType(str, Enum):
    """Predefined discrete action choices evaluated by Jev."""
    FOCUS_WINDOW = "focus_window"
    CLICK = "click"
    DOUBLE_CLICK = "double_click"
    SET_TEXT = "set_text"
    TOGGLE = "toggle"
    WAIT_FOR_UI = "wait_for_ui"
    FINISH_TASK = "finish_task"


class NextTreeAction(str, Enum):
    """Predefined UI tree representations evaluated by Jev for the next step."""
    INSPECT_SELECTED_WINDOW = "inspect_selected_window"
    REFRESH_CURRENT_WINDOW = "refresh_current_window"
    INSPECT_DESKTOP = "inspect_desktop"
    NONE = "none"




# ============================================================================
# Helper Functions: State & Criteria Extraction
# ============================================================================

def build_candidate_options(ui_tree: Dict[str, Any]) -> Tuple[Dict[str, str], Dict[str, Dict[str, Any]]]:
    """
    Extracts candidate windows or interactive controls from the UI tree
    into criteria suitable for Jev's Choice question.
    Traverses all candidate descendant nodes so sub-elements (tabs, buttons,
    toolbars, text inputs) are selectable by Jev.
    
    Returns:
        (criteria_dict, node_lookup_dict)
    """
    criteria = {}
    node_lookup = {}

    def collect_nodes(node: Dict[str, Any]):
        for child in node.get("children", []):
            idx = child.get("id", 0)
            cid = f"#{idx}"
            name = child.get("name", "").strip()
            ctype = child.get("control_type", "Control")
            auto_id = child.get("automation_id", "")
            handle = child.get("handle", "0x0")
            patterns = child.get("patterns", [])

            # Build clean description
            desc_parts = [ctype]
            if name:
                desc_parts.append(f"'{name}'")
            if auto_id:
                desc_parts.append(f"(id: {auto_id})")
            if handle and handle != "0x0":
                desc_parts.append(f"(hwnd: {handle})")
            if patterns:
                desc_parts.append(f"[{patterns[0]}]")

            label = " ".join(desc_parts)
            criteria[cid] = label
            node_lookup[cid] = child

            # Recurse into child's children
            collect_nodes(child)

    collect_nodes(ui_tree)

    # Add option for fallback/none
    criteria["NONE"] = "None of the above / desired element or window is not visible"
    node_lookup["NONE"] = None

    return criteria, node_lookup


def build_jev_state(
    user_prompt: str,
    ui_tree: Dict[str, Any],
    system_prompt: str = DEFAULT_SYSTEM_PROMPT,
    action_history: str = ""
) -> str:
    """
    Assembles the full atomic state string provided to Jev.
    Combines system prompt instructions, user prompt, formatted micro-tree,
    and recent action history so Jev knows what has already been done.
    """
    formatted_tree = format_tree_text(ui_tree)
    root_name = ui_tree.get("name") or "Root Desktop"
    root_type = ui_tree.get("control_type", "PaneControl")
    candidates_count = len(ui_tree.get("children", []))

    state = f"""[SYSTEM INSTRUCTIONS]
{system_prompt.strip()}

[USER PROMPT]
{user_prompt.strip()}

[CURRENT UI TREE SCOPE]
Root: {root_type} "{root_name}" | UI Candidates: {candidates_count}

[CURRENT UI MICRO-TREE]
{formatted_tree}
"""
    if action_history:
        state += f"\n[ACTION HISTORY (most recent last)] {action_history.strip()}"


    return state.strip()



# ============================================================================
# Jev Decision Client
# ============================================================================

class JevClient:
    """
    Client for TypeSafe AI's Jev model specialized for Desktop Automation.
    Manages questions and validated probability extraction.
    """

    def __init__(
        self,
        api_key: Optional[str] = None,
        base_url: Optional[str] = None,
        system_prompt: Optional[str] = None
    ):
        self.api_key = api_key if api_key is not None else os.getenv("TYPESAFE_API_KEY")
        self.base_url = base_url or os.getenv("TYPESAFE_API_BASE", "https://api.typesafe.ai/v1")
        self.system_prompt = system_prompt or DEFAULT_SYSTEM_PROMPT

    def decide(
        self,
        user_prompt: str,
        ui_tree: Dict[str, Any],
        system_prompt: Optional[str] = None,
        action_history: str = ""
    ) :
        """
        Submits the UI tree, user input, and system prompt to Jev.
        Returns a JevResult containing the typed JevDecision and Jev's evaluated probabilities.
        """
        active_system_prompt = system_prompt or self.system_prompt
        state_str = build_jev_state(
            user_prompt=user_prompt,
            ui_tree=ui_tree,
            system_prompt=active_system_prompt,
            action_history=action_history
        )

        candidates_criteria, node_lookup = build_candidate_options(ui_tree)

        # 1. Try official TypeSafe SDK if available and API key present
        if self.api_key:
            try:
                print("I run")
                response = self._call_typesafe_sdk(
                    state=state_str,
                    user_prompt=user_prompt,
                    ui_tree=ui_tree,
                    candidates_criteria=candidates_criteria,
                    node_lookup=node_lookup
                )
                return response
            except Exception as e:
                print(f"[!] TypeSafe SDK call failed ({e}). Attempting REST API fallback...", file=sys.stderr)

        return "Failed to contact jev"



    def _call_typesafe_sdk(
        self,
        state: str,
        user_prompt: str,
        ui_tree: Dict[str, Any],
        candidates_criteria: Dict[str, str],
        node_lookup: Dict[str, Any]
    ):
        """Execute decision via the official TypeSafe SDK."""
        with TypeSafeClient(api_key=self.api_key) as client:
            questions = {
                "target_selection": Choice(
                    instructions="Select which window or UI element candidate should be interacted with or focused next.",
                    criteria=candidates_criteria
                ),
                "action_to_take": Choice(
                    instructions="What action should be executed on the selected target?",
                    criteria={
                        "focus_window": "Focus and bring the target window to the foreground",
                        "click": "Click on the selected UI element or invoke primary pattern",
                        "double_click": "Double-click the selected UI element",
                        "set_text": "Enter or replace text into the selected control",
                        "toggle": "Toggle checkbox or switch state",
                        "wait_for_ui": "Wait for application to respond or dialog to appear",
                        "finish_task": "The user prompt is completely fulfilled"
                    }
                ),
                "next_ui_tree": Choice(
                    instructions="Decide which UI tree representation to retrieve for the next automation step.",
                    criteria={
                        "inspect_selected_window": "Drill into the selected window and retrieve its Depth-1 UI component tree",
                        "refresh_current_window": "Re-inspect the current active window at Depth-1 to check state updates",
                        "inspect_desktop": "Return to the Root Desktop to select among all open applications",
                        "none": "No next UI tree needed because the task is finished"
                    }
                ),
                "is_goal_complete": Noul(
                    instructions="Is the user prompt/goal completely fulfilled based on the current UI state?"
                )
            }

            response = client.system_one(state=state, questions=questions)
            answers = response.answers

            # Extract Target Selection
            target_ans = answers.get("target_selection")
            target_id = max(target_ans.probabilities, key=target_ans.probabilities.get)

            # Extract Action
            action_ans = answers.get("action_to_take")
            action = max(action_ans.probabilities,key = action_ans.probabilities.get)

            # Extract Next UI Tree
            tree_ans = answers.get("next_ui_tree")
            next_tree = max(tree_ans.probabilities, key=tree_ans.probabilities.get)

            # Extract Noul Goal Completion
            noul_ans = answers.get("is_goal_complete")
            noul_score = noul_ans.noul

            target_node = node_lookup.get(target_id)
            noul_val = float(noul_score)

            decision = {
                "target_id" : target_id,
                "action" : action,
                "next_ui_tree" : next_tree,
                "is_goal_complete" : noul_score >= 0.60
            }

            return {
                "decision" : decision,
                "probabilities" : {
                    "target_id": target_id,
                    "action": action,
                    "next_ui_tree": next_tree
                },
                "noul_probability": noul_val,
                "target_node": target_node,
                "decision_mode": "typesafe_sdk",
                "user_prompt": user_prompt,
                "raw_response": getattr(response, "__dict__", str(response))
            }

   

# ============================================================================
# High-Level Conveniences
# ============================================================================

def decide_desktop_action(
    user_prompt: str,
    ui_tree: Optional[Dict[str, Any]] = None,
    system_prompt: Optional[str] = None,
    window_name: Optional[str] = None,
    depth: int = 1,
    api_key: Optional[str] = None
):
    """
    Convenience function that inspects the current UI tree (if not provided)
    and queries Jev for the next desktop automation decision.
    """
    # If no tree was provided, capture it live using ui_tree.py
    if ui_tree is None:
        target_control = None
        if window_name:
            target_control = find_target_window(window_name)
        if not target_control:
            target_control = auto.GetRootControl()

        ui_tree = get_component_tree(target_control, max_depth=depth)

    client = JevClient(api_key=api_key, system_prompt=system_prompt)
    return client.decide(user_prompt=user_prompt, ui_tree=ui_tree)


# ============================================================================
# Command Line Interface (CLI)
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Jev System One Decision Engine for Desktop Automation"
    )
    parser.add_argument(
        "-p", "--prompt",
        type=str,
        required=True,
        help="User automation prompt/goal (e.g. 'Open Notepad and type Hello World')"
    )
    parser.add_argument(
        "-w", "--window",
        type=str,
        default=None,
        help="Optional target window to inspect (e.g. 'Notepad', 'Chrome'). Defaults to Root Desktop."
    )
    parser.add_argument(
        "--tree-file",
        type=str,
        default=None,
        help="Path to an existing JSON UI tree file instead of live inspection."
    )
    parser.add_argument(
        "--depth",
        type=int,
        default=1,
        help="Max depth for UI tree extraction (default: 1)."
    )
    parser.add_argument(
        "--system-prompt",
        type=str,
        default=None,
        help="Optional custom system prompt to override DEFAULT_SYSTEM_PROMPT."
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output raw JSON decision instead of formatted summary."
    )
    parser.add_argument(
        "--api-key",
        type=str,
        default=None,
        help="Optional TypeSafe API key override (or use TYPESAFE_API_KEY environment variable)."
    )

    args = parser.parse_args()

    # 1. Obtain UI Tree
    if args.tree_file:
        try:
            with open(args.tree_file, "r", encoding="utf-8") as f:
                ui_tree = json.load(f)
            print(f"[+] Loaded UI Tree from file: {args.tree_file}")
        except Exception as e:
            print(f"[-] Error loading tree file '{args.tree_file}': {e}", file=sys.stderr)
            sys.exit(1)
    else:
        # Live inspection
        if args.window:
            target = find_target_window(args.window)
            if not target:
                print(f"[-] Window matching '{args.window}' not found. Falling back to Root Desktop.", file=sys.stderr)
                target = auto.GetRootControl()
        else:
            target = auto.GetRootControl()

        print(f"[+] Inspecting live UI Tree for: {target.Name or 'Root Desktop'} (Depth: {args.depth})")
        ui_tree = get_component_tree(target, max_depth=args.depth)
     

    # 2. Query Jev
    client = JevClient(api_key=args.api_key, system_prompt=args.system_prompt)
    decision = client.decide(user_prompt=args.prompt, ui_tree=ui_tree)

    # 3. Output Decision
    if args.json: 
        print(json.dumps(decision.to_dict(), indent=2))
    else:
        print(decision)


if __name__ == "__main__":
    main()
