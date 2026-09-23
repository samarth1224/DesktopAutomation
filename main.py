from pywinauto import Application

# Connect to the target app (e.g., Notepad)
app = Application(backend="uia").connect(title_re=".*Notepad.*")
window = app.top_window()

# Programmatically capture the tree as a string variable
ui_tree_string = window.dump_window()

# Now you can send 'ui_tree_string' directly to your LLM prompt!
print(ui_tree_string)
