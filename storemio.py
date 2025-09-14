import os
import sys
import json
import shutil
import time
import webbrowser
import threading
import multiprocessing
import importlib
import textwrap
import locale
import logging
from datetime import datetime
from queue import Queue, Empty

# --- Dependencies Check ---
try:
    import curses
    import curses.textpad
except ImportError:
    if os.name == 'nt':
        print("Dependency required for Windows: windows-curses.")
        print("Please install it: pip install windows-curses")
        sys.exit(1)
    else:
        print("The standard 'curses' library is missing from your Python installation.")
        sys.exit(1)

try:
    importlib.util.find_spec('webview')
    PYWEBVIEW_AVAILABLE = True
except (ImportError, AttributeError):
    PYWEBVIEW_AVAILABLE = False

try:
    import pyperclip
    PYPERCLIP_AVAILABLE = True
except ImportError:
    PYPERCLIP_AVAILABLE = False

try:
    import requests
except ImportError:
    print("Dependency required: requests. Please install it: pip install requests")
    sys.exit(1)


# --- Constants ---
APP_NAME = "Storemio"
CONFIG_FILE_NAME = "storemio_config.json"
API_BASE_URL = "https://api.strem.io/api/"
GET_ADDONS_ENDPOINT = "addonCollectionGet"
SET_ADDONS_ENDPOINT = "addonCollectionSet"


# --- Curses Color Pair Definitions ---
C_DEFAULT, C_LOGO, C_SELECTED, C_HEADER, C_SUCCESS, C_WARNING, C_ERROR, C_DIM, C_BORDER, C_POPUP_BORDER, C_POPUP_BG = range(1, 12)


# --- Core Helper Functions (Non-UI) ---

def get_default_data_dir():
    if sys.platform == "win32": return os.path.join(os.environ["APPDATA"], APP_NAME)
    elif sys.platform == "darwin": return os.path.join(os.path.expanduser("~/Library/Application Support/"), APP_NAME)
    else: return os.path.join(os.path.expanduser("~/.config/"), APP_NAME)

def get_data_dir():
    config_path = os.path.join(get_default_data_dir(), CONFIG_FILE_NAME)
    if os.path.exists(config_path):
        try:
            with open(config_path, "r") as f: config = json.load(f)
            data_dir = config.get("data_dir")
            if data_dir and os.path.isdir(data_dir): return os.path.abspath(data_dir)
        except Exception: pass
    return os.path.abspath(get_default_data_dir())

def setup_logging():
    """Sets up a basic file logger for the application."""
    log_file = os.path.join(get_data_dir(), "storemio.log")
    logging.basicConfig(
        filename=log_file,
        level=logging.ERROR,
        format='%(asctime)s - %(levelname)s - %(message)s'
    )

def get_accounts_file(): return os.path.join(get_data_dir(), "stremio_accounts.json")
def get_snapshots_dir(): return os.path.join(get_data_dir(), "snapshots")

def save_config(config):
    config_path = os.path.join(get_default_data_dir(), CONFIG_FILE_NAME)
    os.makedirs(os.path.dirname(config_path), exist_ok=True)
    with open(config_path, "w") as f: json.dump(config, f, indent=4)

def load_accounts():
    accounts_file = get_accounts_file()
    if not os.path.exists(accounts_file): return {}
    try:
        with open(accounts_file, "r") as f:
            accounts = json.load(f)
        needs_save = False
        for nickname, data in accounts.items():
            if isinstance(data, str): accounts[nickname] = {"path": data, "authKey": None, "mirrors": None}; needs_save = True
            if "mirrors" not in data: accounts[nickname]["mirrors"] = None; needs_save = True
        if needs_save: save_accounts(accounts)
        return accounts
    except Exception as e:
        logging.error(f"Failed to load accounts: {e}")
        return {}

def save_accounts(accounts):
    with open(get_accounts_file(), "w") as f: json.dump(accounts, f, indent=4)

def ensure_data_dirs():
    data_dir = get_data_dir()
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(os.path.join(data_dir, "pywebview_profiles"), exist_ok=True)
    os.makedirs(get_snapshots_dir(), exist_ok=True)


# --- API Interaction ---
def get_original_manifest(transport_url):
    if not transport_url: return None, "No transport URL provided."
    try:
        r = requests.get(transport_url, timeout=10)
        r.raise_for_status()
        return r.json(), None
    except (requests.RequestException, json.JSONDecodeError) as e:
        logging.error(f"get_original_manifest failed for {transport_url}: {e}")
        return None, f"Could not fetch manifest: {e}"

def get_user_addons(profile):
    auth_key = profile.get('authKey')
    if not auth_key: return None, "No AuthKey found. Please launch Stremio to log in and generate one."
    try:
        r = requests.post(f"{API_BASE_URL}{GET_ADDONS_ENDPOINT}", json={"authKey": auth_key}, timeout=10)
        r.raise_for_status()
        data = r.json()
        if 'result' in data and 'addons' in data['result']:
            return data['result']['addons'], None
        return None, "API response was valid but did not contain addons."
    except requests.exceptions.RequestException as e:
        logging.error(f"get_user_addons failed for {profile.get('name')}: {e}")
        return None, f"Network error: {e}"

def sync_user_addons(ui, profile, addons, silent=False):
    if not silent: ui.set_status(f"Syncing addons to '{profile['name']}'...")
    payload_addons = json.loads(json.dumps(addons))
    for addon in payload_addons:
        if 'manifest' in addon and '_master_catalog_list' in addon['manifest']:
            del addon['manifest']['_master_catalog_list']
    payload = {"authKey": profile['authKey'], "addons": payload_addons}
    try:
        r = requests.post(f"{API_BASE_URL}{SET_ADDONS_ENDPOINT}", json=payload, timeout=10)
        r.raise_for_status()
        if r.json().get('result', {}).get('success'):
            if not silent: ui.set_status("Success: Addons synced!", C_SUCCESS, 2)
            return True
        else:
            error_text = r.text
            logging.error(f"Syncing failed for {profile.get('name')}: {error_text}")
            if not silent: ui.set_status(f"Error: Syncing failed: {error_text}", C_ERROR, 3)
            return False
    except requests.exceptions.RequestException as e:
        logging.error(f"Syncing network error for {profile.get('name')}: {e}")
        if not silent: ui.set_status(f"Error: An error occurred: {e}", C_ERROR, 3)
        return False

def sync_slaves_of_master(ui, master_name, master_addons):
    accounts, slaves_found = load_accounts(), False
    for name, data in accounts.items():
        if data.get('mirrors') == master_name:
            if not slaves_found:
                ui.set_status("Master profile updated, syncing slaves...", C_SUCCESS, 1)
                slaves_found = True
            sync_user_addons(ui, {'name': name, **data}, master_addons)
            ui.sync_status[name] = "SYNCED"


# --- PyWebView Functions ---
def update_auth_key_periodically(window, nickname, exit_event):
    while not exit_event.is_set():
        try:
            js_code = "(function() { try { return JSON.parse(localStorage.getItem('profile')).auth.key; } catch (e) { return null; } })()"
            current_key = window.evaluate_js(js_code)
            if current_key:
                accounts = load_accounts()
                if accounts.get(nickname, {}).get('authKey') != current_key:
                    accounts[nickname]['authKey'] = current_key; save_accounts(accounts)
        except Exception: pass
        exit_event.wait(15)

def launch_pywebview_with_profile(nickname, profile_path, url="https://web.stremio.com/#/"):
    if not PYWEBVIEW_AVAILABLE: return
    import webview

    # Determine the OS-specific null device to discard cache data
    dev_null = 'nul' if sys.platform == "win32" else '/dev/null'

    # These arguments aggressively disable caching to reduce storage footprint.
    browser_args = [
        # Point disk and media cache to a null device, effectively disabling them.
        f'--disk-cache-dir={dev_null}',
        f'--media-cache-dir={dev_null}',

        # Set cache sizes to the absolute minimum (1 byte).
        '--disk-cache-size=1',
        '--media-cache-size=1',

        # Disable other forms of caching and storage mechanisms.
        '--disable-application-cache',
        '--disable-cache',
        '--disable-gpu-shader-disk-cache',
        '--disable-offline-load-stale-cache',
        '--disable-session-storage', # May require more frequent logins if session cookies are not persisted.
    ]
    
    os.environ['WEBVIEW_ADDITIONAL_BROWSER_ARGS'] = ' '.join(browser_args)
    os.makedirs(profile_path, exist_ok=True)
    exit_event = threading.Event()
    window = webview.create_window(f"Stremio - {nickname}", url, width=1200, height=800)
    window.events.closing += lambda: exit_event.set()
    def on_loaded():
        threading.Thread(target=update_auth_key_periodically, args=(window, nickname, exit_event), daemon=True).start()
    webview.start(on_loaded, private_mode=False, storage_path=os.path.abspath(profile_path))


# --- Modern UI Manager & Components ---

class UIManager:
    def __init__(self, stdscr):
        self.stdscr = stdscr; self.init_colors()
        self.status_message, self.status_color, self.status_timer = "", C_DEFAULT, 0
        self.breadcrumb = ["Storemio"]
        self.sync_status = {}

    def init_colors(self):
        curses.start_color(); curses.use_default_colors()
        # Main App Colors
        curses.init_pair(C_DEFAULT, curses.COLOR_WHITE, -1); curses.init_pair(C_LOGO, curses.COLOR_MAGENTA, -1)
        curses.init_pair(C_SELECTED, curses.COLOR_BLACK, curses.COLOR_CYAN); curses.init_pair(C_HEADER, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_SUCCESS, curses.COLOR_GREEN, -1); curses.init_pair(C_WARNING, curses.COLOR_YELLOW, -1)
        curses.init_pair(C_ERROR, curses.COLOR_RED, -1); curses.init_pair(C_DIM, 242, -1)
        curses.init_pair(C_BORDER, curses.COLOR_CYAN, -1)

        # RaspiBlitz-style Popup Colors
        curses.init_pair(C_POPUP_BG, curses.COLOR_WHITE, -1)
        curses.init_pair(C_POPUP_BORDER, curses.COLOR_CYAN, -1)
        curses.init_pair(C_POPUP_BORDER + 1, 250, -1)

    def safe_addstr(self, window, y, x, text, attr=0):
        h, w = window.getmaxyx(); text = text.replace('\n', '')
        if y >= h or x >= w or (w-x) <= 0: return
        window.addstr(y, x, text[:w-x], attr)

    def update_status_bar(self):
        """Only updates the status bar line for timers and messages."""
        h, w = self.stdscr.getmaxyx()
        if self.status_timer > 0: self.status_timer -= 1
        else: self.status_message, self.status_color = "", C_DEFAULT
        
        self.stdscr.move(h - 1, 2)
        self.stdscr.clrtoeol()
        self.safe_addstr(self.stdscr, h - 1, 2, self.status_message[:w-4], curses.color_pair(self.status_color))
        self.stdscr.refresh()

    def draw_chrome(self, title, key_map):
        h, w = self.stdscr.getmaxyx(); self.stdscr.clear()
        self.stdscr.attron(curses.color_pair(C_BORDER)); self.stdscr.border()
        self.stdscr.addch(0, w // 2, curses.ACS_TTEE); self.stdscr.addch(2, 0, curses.ACS_LTEE)
        self.stdscr.hline(2, 1, curses.ACS_HLINE, w - 2); self.stdscr.addch(2, w - 1, curses.ACS_RTEE)
        self.stdscr.attroff(curses.color_pair(C_BORDER))
        header_text = f" {APP_NAME} | {title} "; self.safe_addstr(self.stdscr, 0, (w - len(header_text)) // 2, header_text, curses.A_BOLD)
        self.safe_addstr(self.stdscr, 1, 2, " > ".join(self.breadcrumb), curses.color_pair(C_DIM))
        footer_y = h - 3
        self.stdscr.attron(curses.color_pair(C_BORDER)); self.stdscr.addch(footer_y, 0, curses.ACS_LTEE)
        self.stdscr.hline(footer_y, 1, curses.ACS_HLINE, w - 2); self.stdscr.addch(footer_y, w - 1, curses.ACS_RTEE)
        self.stdscr.attroff(curses.color_pair(C_BORDER))
        key_chunks, current_chunk, chunk_len = [], "", 0
        for key, desc in key_map.items():
            part = f" {key.upper()}: {desc} |"
            if chunk_len + len(part) > w - 4: key_chunks.append(current_chunk); current_chunk, chunk_len = "", 0
            current_chunk += part; chunk_len += len(part)
        key_chunks.append(current_chunk.strip().rstrip('|').strip())
        self.safe_addstr(self.stdscr, h - 2, 2, key_chunks[0])
        if len(key_chunks) > 1: self.safe_addstr(self.stdscr, h - 1, 2, key_chunks[1])
        
        self.update_status_bar()

    def get_content_win(self): return self.stdscr.derwin(self.stdscr.getmaxyx()[0] - 6, self.stdscr.getmaxyx()[1] - 4, 3, 2)
    def set_status(self, message, color=C_DEFAULT, duration_s=3): self.status_message, self.status_color, self.status_timer = message, color, duration_s * 10

    def run_threaded_task(self, target_func, args=(), message="Loading..."):
        q = Queue()
        
        def wrapper():
            result = target_func(*args)
            q.put(result)

        thread = threading.Thread(target=wrapper, daemon=True)
        thread.start()

        win = self.get_content_win()
        animation = "|/-\\"
        i = 0
        while thread.is_alive():
            win.clear()
            loading_text = f"{message} {animation[i % len(animation)]}"
            self.safe_addstr(win, win.getmaxyx()[0] // 2, (win.getmaxyx()[1] - len(loading_text)) // 2, loading_text, curses.color_pair(C_DIM))
            win.refresh()
            time.sleep(0.1)
            i += 1
        
        try:
            return q.get_nowait()
        except Empty:
            return None

    def popup(self, title, text_lines, color=C_POPUP_BORDER):
        h, w = self.stdscr.getmaxyx()
        p_h = len(text_lines) + 4
        p_w = max(len(line) for line in text_lines) + 4 if text_lines else 20
        p_w = max(p_w, len(title) + 4)
        p_y, p_x = (h - p_h) // 2, (w - p_w) // 2
        popup_win = curses.newwin(p_h, p_w, p_y, p_x)
        popup_win.bkgd(' ', curses.color_pair(C_POPUP_BG)); popup_win.attron(curses.color_pair(color)); popup_win.border()
        popup_win.attroff(curses.color_pair(color)); self.safe_addstr(popup_win, 0, (p_w - len(title) - 2) // 2, f" {title} ")
        for i, line in enumerate(text_lines): self.safe_addstr(popup_win, i + 2, 2, line)
        popup_win.refresh(); return popup_win

    def confirm(self, title, question):
        popup_win = self.popup(title, [question, ""], C_WARNING)
        options, selected = ["Yes", "No"], 1
        while True:
            for i, opt in enumerate(options):
                attr = curses.A_REVERSE if i == selected else 0
                popup_win.addstr(3, (popup_win.getmaxyx()[1] // 3) * (i + 1) - (len(opt)//2), opt, attr)
            popup_win.refresh()
            key = self.stdscr.getch()
            if key in [curses.KEY_LEFT, ord('h')]: selected = 0
            elif key in [curses.KEY_RIGHT, ord('l')]: selected = 1
            elif key in [curses.KEY_ENTER, 10, 13]: return selected == 0
            elif key == 27: return False

    def prompt(self, title, prompt_text):
        popup_win = self.popup(title, [prompt_text, " " * (len(prompt_text)+5)])
        curses.curs_set(1); curses.echo()
        input_win = popup_win.derwin(1, popup_win.getmaxyx()[1] - 4, 3, 2)
        content = input_win.getstr(0, 0).decode('utf-8').strip()
        curses.noecho(); curses.curs_set(0)
        return content

    def context_menu(self, title, options, parent_win, y, x):
        """
        Displays a robust RaspiBlitz-style context menu that prevents screen overflow.
        Returns the selected option string or None if cancelled.
        """
        if not options: return None

        # --- Prepare Menu Content ---
        keyword_width = 12
        menu_items = []
        for opt in options:
            description = opt
            # Special case for the "Enable/Disable Catalogs" option
            if opt == "Enable/Disable Catalogs":
                keyword = "TOGGLE"
            else:
                # Default behavior for all other options
                keyword = opt.split(' ')[0].upper()
                if "/" in keyword:
                    keyword = keyword.split('/')[0]
            
            menu_items.append({'keyword': keyword, 'desc': description})

        ideal_p_h = len(options) + 4
        desc_width = max(len(item['desc']) for item in menu_items)
        ideal_p_w = keyword_width + desc_width + 4

        # --- Robust Size & Position Calculation to Prevent Crashing ---
        max_h, max_w = parent_win.getmaxyx()
        p_h = min(ideal_p_h, max_h)
        p_w = min(ideal_p_w, max_w)
        
        # Recalculate position based on final, safe dimensions
        if y + p_h >= max_h: y = max_h - p_h
        if x + p_w >= max_w: x = max_w - p_w
        y = max(0, y); x = max(0, x)

        # --- Draw the Menu Structure ---
        outer_win = parent_win.derwin(p_h, p_w, y, x)
        outer_win.bkgd(' ', curses.color_pair(C_POPUP_BG))
        outer_win.attron(curses.color_pair(C_POPUP_BORDER + 1))
        outer_win.border()
        outer_win.attroff(curses.color_pair(C_POPUP_BORDER + 1))

        menu_win = outer_win.derwin(p_h - 2, p_w - 2, 1, 1)
        menu_win.bkgd(' ', curses.color_pair(C_POPUP_BG))
        menu_win.attron(curses.color_pair(C_POPUP_BORDER))
        menu_win.border()
        self.safe_addstr(menu_win, 0, (p_w - 2 - len(title) - 2) // 2, f" {title} ")
        menu_win.attroff(curses.color_pair(C_POPUP_BORDER))

        # --- Interaction Loop ---
        cursor_pos = 0
        while True:
            for i, item in enumerate(menu_items):
                is_selected = (i == cursor_pos)
                keyword_text = item['keyword']
                desc_text = item['desc']
                line_y = i + 1
                
                if is_selected:
                    highlight_attr = curses.color_pair(C_SELECTED)
                    self.safe_addstr(menu_win, line_y, 2, ' ' * (keyword_width - 1), highlight_attr)
                    self.safe_addstr(menu_win, line_y, keyword_width + 2, ' ' * len(desc_text), highlight_attr)
                    self.safe_addstr(menu_win, line_y, 2 + (keyword_width - 2 - len(keyword_text)), keyword_text, highlight_attr)
                    self.safe_addstr(menu_win, line_y, keyword_width + 2, desc_text, highlight_attr)
                else:
                    self.safe_addstr(menu_win, line_y, 1, ' ' * (p_w - 4), curses.color_pair(C_POPUP_BG))
                    self.safe_addstr(menu_win, line_y, 2 + (keyword_width - 2 - len(keyword_text)), keyword_text, curses.color_pair(C_POPUP_BORDER))
                    self.safe_addstr(menu_win, line_y, keyword_width + 2, desc_text, curses.color_pair(C_POPUP_BORDER))
            
            outer_win.refresh()
            menu_win.refresh()

            key = self.stdscr.getch()
            if key in [curses.KEY_UP, ord('k')]:
                cursor_pos = max(0, cursor_pos - 1)
            elif key in [curses.KEY_DOWN, ord('j')]:
                cursor_pos = min(len(options) - 1, cursor_pos + 1)
            elif key in [27, ord('q')]:
                return None
            elif key in [curses.KEY_ENTER, 10, 13]:
                return options[cursor_pos]

class Menu:
    def __init__(self, ui, items, item_renderer_func):
        self.ui, self.items, self.render_item = ui, items, item_renderer_func
        self.cursor_pos, self.scroll_offset = 0, 0

    def handle_key(self, key):
        h = self.ui.get_content_win().getmaxyx()[0]
        if key in [curses.KEY_UP, ord('k')]: self.cursor_pos = max(0, self.cursor_pos - 1)
        elif key in [curses.KEY_DOWN, ord('j')]: self.cursor_pos = min(len(self.items) - 1, self.cursor_pos + 1)
        elif key == curses.KEY_PPAGE: self.cursor_pos = max(0, self.cursor_pos - h)
        elif key == curses.KEY_NPAGE: self.cursor_pos = min(len(self.items) - 1, self.cursor_pos + h)

    def draw(self, win):
        win.clear(); h, w = win.getmaxyx()
        if self.cursor_pos < self.scroll_offset: self.scroll_offset = self.cursor_pos
        if self.cursor_pos >= self.scroll_offset + h: self.scroll_offset = self.cursor_pos - h + 1
        for i in range(h):
            item_index = self.scroll_offset + i
            if item_index >= len(self.items): break
            self.render_item(win, i, 0, self.items[item_index], item_index == self.cursor_pos)
        
        win.move(0, 0)
        win.refresh()
        
    def get_selected_item(self):
        return self.items[self.cursor_pos] if 0 <= self.cursor_pos < len(self.items) else None


# --- Application Screens ---

def run_profile_list_screen(ui):
    """
    Main screen of the application. Displays a list of Stremio profiles.
    """
    cursor_pos = 0
    needs_redraw = True
    last_sync_status_copy = {}

    while True:
        if ui.sync_status != last_sync_status_copy:
            needs_redraw = True
            last_sync_status_copy = ui.sync_status.copy()

        if needs_redraw:
            accounts = load_accounts(); account_list = list(accounts.keys())
            def render_profile(win, y, x, name, is_selected):
                max_name_len = max(len(n) for n in account_list) if account_list else 0
                display_line = f"{name:<{max_name_len}}"
                mirror_source = accounts[name].get('mirrors')
                is_master = any(d.get('mirrors') == name for d in accounts.values())
                
                attr = C_DEFAULT

                if mirror_source: 
                    display_line += f"  (Mirrors: {mirror_source})"
                    attr = C_DIM
                    status = ui.sync_status.get(name)
                    if status == "CHECKING":
                        display_line += " (Checking...)"
                    elif status == "SYNCING":
                        display_line += " (Syncing...)"
                    elif status == "AUTO_SYNCED":
                        display_line += " (Synced)"
                        attr = C_SUCCESS
                    elif status == "SYNC_FAILED":
                        display_line += " (Sync Failed!)"
                        attr = C_ERROR
                elif is_master: 
                    display_line += f"  (Master)"
                    attr = C_WARNING

                ui.safe_addstr(win, y, x, f" {display_line} ", curses.color_pair(C_SELECTED if is_selected else attr))
            
            menu = Menu(ui, account_list, render_profile)
            menu.cursor_pos = min(cursor_pos, len(account_list) - 1) if account_list else 0
            key_map = {"↑↓/PgUp/PgDn": "Select", "q": "Quit", "s": "Settings"}
            if account_list: key_map["enter"] = "Actions"
            else: key_map["a"] = "Add New"
            ui.draw_chrome("Profiles", key_map)
            content_win = ui.get_content_win()
            if not account_list:
                ui.safe_addstr(content_win, 2, 2, "No profiles found. Press 'a' to add one.", curses.color_pair(C_DIM))
                content_win.refresh()
            else: menu.draw(content_win)
            needs_redraw = False
        else:
            ui.update_status_bar()

        key = ui.stdscr.getch()
        key_char = chr(key).lower() if 32 <= key <= 126 else ''
        
        action_taken = False
        if key == curses.KEY_RESIZE: action_taken = True
        elif key_char == 'q':
            if ui.confirm("Quit?", "Are you sure you want to exit Storemio?"):
                return
            action_taken = True
        elif key_char == 's': run_settings_screen(ui); action_taken = True
        elif key_char == 'a' and not account_list: add_account(ui); action_taken = True
        elif key in [curses.KEY_ENTER, 10, 13] and account_list:
            selected_profile = menu.get_selected_item()
            content_win = ui.get_content_win()
            
            # --- UPDATED MENU WITH SUBMENU LOGIC ---
            options = ["Manage Addons", "Launch Stremio", "Backups", "Configure Mirroring", "Copy AuthKey", "Delete Profile"]
            action = ui.context_menu(
                f"Actions for '{selected_profile}'",
                options,
                content_win,
                menu.cursor_pos - menu.scroll_offset,
                5
            )
            
            final_action_to_run = None
            if action == "Backups":
                # Open the submenu for backups
                backup_action = ui.context_menu(
                    "Backup Options",
                    ["Create Backup", "Load Backup"],
                    content_win,
                    menu.cursor_pos - menu.scroll_offset + 2, # Position it slightly offset
                    15
                )
                if backup_action:
                    final_action_to_run = backup_action
            elif action:
                # This is a direct action from the first menu
                final_action_to_run = action

            if final_action_to_run:
                if handle_profile_action(ui, selected_profile, final_action_to_run) == "DELETED":
                    cursor_pos = menu.cursor_pos
            
            action_taken = True
        else:
            original_pos = menu.cursor_pos
            menu.handle_key(key)
            cursor_pos = menu.cursor_pos
            if original_pos != cursor_pos:
                action_taken = True
        
        if action_taken:
            needs_redraw = True


def handle_profile_action(ui, nickname, selected):
    """
    Handles the action selected for a profile from the context menu.
    """
    profile_data = load_accounts().get(nickname)
    full_profile = {'name': nickname, **profile_data}
    if not profile_data: 
        ui.set_status("Error: Profile data not found!", C_ERROR)
        return

    if selected == "Launch Stremio":
        p = multiprocessing.Process(target=launch_pywebview_with_profile, args=(nickname, profile_data['path'])); p.start()
        ui.set_status(f"Launching '{nickname}'...", C_SUCCESS)
    elif selected == "Manage Addons": run_addon_manager_screen(ui, full_profile)
    elif selected == "Create Backup":
        addons, error = ui.run_threaded_task(get_user_addons, args=(full_profile,), message="Fetching addons for backup...")
        if error:
            ui.set_status(f"Backup failed: {error}", C_ERROR, 5)
            return
        
        desc = ui.prompt("Create Backup", "Optional description (e.g., 'Clean Install'):")
        safe_desc = "".join(c for c in desc if c.isalnum() or c in (' ', '_', '-')).rstrip()
        
        timestamp = datetime.now().strftime('%Y-%m-%d_%H-%M-%S')
        filename = f"{nickname}_{timestamp}{'_' + safe_desc if safe_desc else ''}.json"
        
        try:
            filepath = os.path.join(get_snapshots_dir(), filename)
            with open(filepath, 'w') as f:
                json.dump(addons, f, indent=4)
            ui.set_status("Success: Backup created!", C_SUCCESS)
        except Exception as e:
            logging.error(f"Failed to save backup {filename}: {e}")
            ui.set_status(f"Error saving backup: {e}", C_ERROR, 5)
            
    elif selected == "Load Backup": run_backup_loader_screen(ui, full_profile)
    elif selected == "Configure Mirroring": run_mirror_config_screen(ui, nickname)
    elif selected == "Copy AuthKey":
        if key_val := profile_data.get('authKey'):
            if PYPERCLIP_AVAILABLE: pyperclip.copy(key_val); ui.set_status("Success: AuthKey copied!", C_SUCCESS)
            else: ui.popup("AuthKey", [f"Pyperclip not found. Key is:", key_val]); ui.stdscr.getch()
        else: ui.set_status("Error: No AuthKey found.", C_ERROR)
    elif selected == "Delete Profile":
        if ui.confirm("Delete Profile?", f"Permanently delete '{nickname}'?"):
            accounts = load_accounts(); shutil.rmtree(accounts[nickname]['path'], ignore_errors=True)
            del accounts[nickname]; save_accounts(accounts)
            ui.set_status(f"Success: Profile '{nickname}' deleted.", C_SUCCESS, 2)
            return "DELETED"
    return None

def run_settings_screen(ui):
    ui.breadcrumb.append("Settings"); options = ["Add New Profile", "Change Data Directory"]
    def render_option(win, y, x, item, is_selected):
        ui.safe_addstr(win, y, x, f" {item} ", curses.color_pair(C_SELECTED if is_selected else C_DEFAULT))
    menu = Menu(ui, options, render_option)
    while True:
        ui.draw_chrome("Settings", {"↑↓": "Select", "enter": "Confirm", "esc": "Back"})
        menu.draw(ui.get_content_win())
        key = ui.stdscr.getch()
        if key == curses.KEY_RESIZE: continue
        if key in [curses.KEY_ENTER, 10, 13]:
            selected = menu.get_selected_item()
            if selected == "Add New Profile": add_account(ui)
            elif selected == "Change Data Directory": change_data_directory(ui)
            break
        elif key in [27, ord('q')]: break
        else: menu.handle_key(key)
    ui.breadcrumb.pop()

def run_addon_manager_screen(ui, profile):
    """
    Displays and manages addons for a profile using a two-pane layout.
    """
    ui.breadcrumb.append("Addons")
    
    addons, error = ui.run_threaded_task(get_user_addons, args=(profile,), message="Fetching addon list...")
    
    if error:
        ui.set_status(error, C_ERROR, 5)
        ui.breadcrumb.pop()
        return
    
    initial_addons_json = json.dumps(addons, sort_keys=True)
    is_holding = False
    needs_redraw = True
    
    def render_addon(win, y, x, addon, is_selected):
        manifest = addon.get('manifest', {}); name = manifest.get('name', 'Unknown')
        is_mod = json.dumps(addon, sort_keys=True) not in initial_addons_json
        
        name_attr = C_WARNING if is_mod else C_DEFAULT
        suffix = " (MOVING)" if is_holding and is_selected else (" *" if is_mod else "")
        if is_selected: name_attr = C_SELECTED
        
        prefix = "> " if is_selected else "  "
        has_catalogs_text = "[C]" if manifest.get('catalogs') else "   "
        line_attr = curses.color_pair(C_SELECTED) if is_selected else curses.color_pair(C_DEFAULT)
        catalog_attr = curses.color_pair(C_SELECTED) if is_selected else curses.color_pair(C_SUCCESS)

        ui.safe_addstr(win, y, x, prefix, line_attr)
        ui.safe_addstr(win, y, x + len(prefix), has_catalogs_text, catalog_attr)
        ui.safe_addstr(win, y, x + len(prefix) + len(has_catalogs_text), f" {name}{suffix}", curses.color_pair(name_attr))

    menu = Menu(ui, addons, render_addon)
    
    while True:
        if needs_redraw:
            has_changes = json.dumps(addons, sort_keys=True) != initial_addons_json
            title = f"Addons for '{profile['name']}'" + (" *" if has_changes else "")
            key_map = { "↑↓": "Select", "space": "Move/Drop", "enter": "Actions", "i": "Install", "x": "Delete", "esc": "Back", "s": "Save" }
            if not addons: key_map = {"i": "Install", "esc": "Back"}
            ui.draw_chrome(title, key_map)
            
            content_win = ui.get_content_win()
            h, w = content_win.getmaxyx()
            list_pane_width = w // 2
            list_pane = content_win.derwin(h, list_pane_width, 0, 0)
            details_pane = content_win.derwin(h, w - list_pane_width, 0, list_pane_width)
            
            menu.draw(list_pane)
            _draw_addon_details_pane(ui, details_pane, menu.get_selected_item())
            needs_redraw = False
        else:
            ui.update_status_bar()

        key = ui.stdscr.getch()
        action_result = _handle_addon_manager_input(ui, key, menu, is_holding, profile, initial_addons_json)
        
        if action_result:
            needs_redraw = True
            if action_result == "EXIT": break
            if action_result == "HOLD_TOGGLE": is_holding = not is_holding
            if action_result == "SYNCED":
                initial_addons_json = json.dumps(addons, sort_keys=True)
            
    ui.breadcrumb.pop()

def _draw_addon_details_pane(ui, win, addon):
    """Draws the right-hand details pane for the selected addon."""
    win.clear()
    win.attron(curses.color_pair(C_BORDER)); win.vline(0, 0, curses.ACS_VLINE, win.getmaxyx()[0]); win.attroff(curses.color_pair(C_BORDER))
    if not addon:
        ui.safe_addstr(win, 2, 3, "No addon selected.", curses.color_pair(C_DIM))
        win.refresh()
        return

    manifest = addon.get('manifest', {})
    y = 1
    ui.safe_addstr(win, y, 3, manifest.get('name', 'Unknown'), curses.color_pair(C_HEADER) | curses.A_BOLD); y += 2
    
    details = {
        "Version": manifest.get('version', 'N/A'),
        "Configurable": "Yes" if manifest.get('behaviorHints', {}).get('configurable') else "No",
        "Catalogs": f"{len(manifest.get('catalogs', []))} enabled"
    }
    for label, value in details.items():
        if value:
            ui.safe_addstr(win, y, 3, f"{label}:", curses.A_BOLD); y += 1
            ui.safe_addstr(win, y, 5, value); y += 2
    
    y += 1
    ui.safe_addstr(win, y, 3, "Description:", curses.A_BOLD); y += 1
    description_text = manifest.get('description') or 'No description available.'
    desc_lines = textwrap.wrap(description_text, win.getmaxyx()[1] - 8)
    for line in desc_lines:
        ui.safe_addstr(win, y, 5, line, curses.color_pair(C_DIM)); y += 1
    
    win.refresh()

def _handle_addon_manager_input(ui, key, menu, is_holding, profile, initial_addons_json):
    """Processes a single key press in the addon manager screen."""
    key_char = chr(key).lower() if 32 <= key <= 126 else ''
    addons = menu.items
    has_changes = json.dumps(addons, sort_keys=True) != initial_addons_json

    if key == curses.KEY_RESIZE: return "REDRAW"

    if key in [27, ord('q')]:
        if is_holding: return "HOLD_TOGGLE"
        if has_changes:
            if ui.confirm("Unsaved Changes", "Discard all changes?"):
                return "EXIT"
            else:
                return "REDRAW"
        else:
            return "EXIT"
    elif key in [curses.KEY_UP, curses.KEY_DOWN, ord('k'), ord('j'), curses.KEY_PPAGE, curses.KEY_NPAGE]:
        original_pos = menu.cursor_pos
        menu.handle_key(key)
        if is_holding:
            pos = menu.cursor_pos
            new_pos = original_pos
            if key in [curses.KEY_UP, ord('k')]: new_pos = max(0, original_pos - 1)
            elif key in [curses.KEY_DOWN, ord('j')]: new_pos = min(len(addons)-1, original_pos + 1)
            
            if new_pos != original_pos:
                addons.insert(new_pos, addons.pop(original_pos))
                menu.cursor_pos = new_pos

        if original_pos != menu.cursor_pos: return "REDRAW"
    elif key_char == 's' and not is_holding:
        if sync_user_addons(ui, profile, addons):
            sync_slaves_of_master(ui, profile['name'], addons)
            return "SYNCED"
    elif key_char == 'i' and not is_holding:
        url = ui.prompt("Install Addon", "Enter addon manifest URL:")
        if url:
            manifest, err = get_original_manifest(url)
            if err: ui.set_status(err, C_ERROR); return "REDRAW"
            addons.append({"transportUrl": url, "manifest": manifest})
            menu.cursor_pos = len(addons) - 1
            return "REDRAW"
    elif key_char == 'x' and not is_holding and addons:
        addon = menu.get_selected_item()
        if ui.confirm("Delete Addon?", f"Delete '{addon['manifest']['name']}'?"):
            addons.pop(menu.cursor_pos)
            menu.cursor_pos = max(0, menu.cursor_pos - 1)
            return "REDRAW"
    elif key == 32 and addons: return "HOLD_TOGGLE"
    elif key in [curses.KEY_ENTER, 10, 13] and not is_holding and addons:
        addon = menu.get_selected_item()
        if not addon: return None

        manifest = addon.get('manifest', {})
        options = []
        if 'catalogs' in manifest and manifest['catalogs']:
            options.extend(["Enable/Disable Catalogs", "Reorder Catalogs"])
        options.extend(["Rename Addon", "Clone to other Profile(s)", "Reset to Default"])
        
        content_win = ui.get_content_win()
        action = ui.context_menu(
            f"Actions for '{addon['manifest'].get('name', 'Addon')[:20]}'",
            options,
            content_win,
            menu.cursor_pos - menu.scroll_offset,
            5
        )
        if action:
            _handle_addon_action(ui, profile, addon, action)
        return "REDRAW"
    
    return None

def _handle_addon_action(ui, profile, addon, selected):
    """
    Processes the logic for a selected addon action.
    """
    if selected == "Rename Addon":
        new_name = ui.prompt("Rename Addon", f"Enter new name for '{addon['manifest']['name']}':")
        if new_name is not None: 
            addon['manifest']['name'] = new_name
            ui.set_status(f"Renamed to '{new_name}'. Press 's' to save.", C_WARNING)
    elif selected == "Enable/Disable Catalogs": 
        manage_catalogs(ui, addon)
    elif selected == "Reorder Catalogs": 
        reorder_catalogs(ui, addon)
    elif selected == "Reset to Default":
        if ui.confirm("Reset Addon?", "Reset to its original manifest settings?"):
            manifest, err = get_original_manifest(addon.get('transportUrl'))
            if err: 
                ui.set_status(err, C_ERROR)
                return
            addon['manifest'] = manifest
            ui.set_status("Addon reset. Press 's' to save.", C_WARNING)
    elif selected == "Clone to other Profile(s)":
        run_clone_addon_screen(ui, profile, addon)

def manage_catalogs(ui, addon):
    ui.breadcrumb.append("Catalogs")
    manifest_copy = json.loads(json.dumps(addon.get('manifest', {})))
    
    if '_master_catalog_list' not in manifest_copy:
        original_manifest, err = ui.run_threaded_task(get_original_manifest, args=(addon.get('transportUrl'),), message="Fetching full catalog list...")
        if err: ui.set_status(err, C_ERROR); ui.breadcrumb.pop(); return
        manifest_copy['_master_catalog_list'] = original_manifest.get('catalogs', []) if original_manifest else manifest_copy.get('catalogs', [])
    
    all_catalogs = manifest_copy.get('_master_catalog_list', [])
    if not all_catalogs: ui.set_status("This addon has no configurable catalogs."); ui.breadcrumb.pop(); return
    
    enabled_ids = {c.get('id') for c in manifest_copy.get('catalogs', [])}
    initial_enabled_ids = enabled_ids.copy()

    def render(win, y, x, cat, is_selected):
        status = "[ON] " if cat.get('id') in enabled_ids else "[OFF]"
        color = C_SUCCESS if cat.get('id') in enabled_ids else C_DEFAULT
        display = f"{status} {cat.get('name', 'N/A')} ({cat.get('type', 'N/A')})"
        ui.safe_addstr(win, y, x, f" {display} ", curses.color_pair(C_SELECTED if is_selected else color))
    menu = Menu(ui, all_catalogs, render)
    
    while True:
        ui.draw_chrome("Enable/Disable Catalogs", {"↑↓": "Select", "space": "Toggle", "enter": "Save", "esc": "Cancel"})
        menu.draw(ui.get_content_win())
        key = ui.stdscr.getch()
        if key == curses.KEY_RESIZE: continue
        if key in [27, ord('q')]:
            has_changes = (enabled_ids != initial_enabled_ids)
            if not has_changes or ui.confirm("Unsaved Changes", "Discard catalog changes?"):
                break
        elif key == 32:
            toggled_id = menu.get_selected_item().get('id')
            if toggled_id in enabled_ids: enabled_ids.remove(toggled_id)
            else: enabled_ids.add(toggled_id)
        elif key in [curses.KEY_ENTER, 10, 13]:
            manifest_copy['catalogs'] = [c for c in all_catalogs if c.get('id') in enabled_ids]
            addon['manifest'] = manifest_copy
            ui.set_status("Catalog changes applied. Press 's' to save.", C_WARNING); break
        else: menu.handle_key(key)
    ui.breadcrumb.pop()


def reorder_catalogs(ui, addon):
    ui.breadcrumb.append("Reorder")
    addon_copy = json.loads(json.dumps(addon))
    catalogs = addon_copy.get('manifest', {}).get('catalogs', [])
    if len(catalogs) < 2: ui.set_status("At least two enabled catalogs needed to reorder."); ui.breadcrumb.pop(); return
    
    initial_catalogs_order = list(catalogs)
    is_holding = False
    def render(win, y, x, cat, is_selected):
        suffix = " (MOVING)" if is_holding and is_selected else ""
        attr = C_WARNING if is_holding and is_selected else (C_SELECTED if is_selected else C_DEFAULT)
        ui.safe_addstr(win, y, x, f" {cat.get('name', 'N/A')}{suffix} ", curses.color_pair(attr))
    menu = Menu(ui, catalogs, render)
    
    while True:
        keys = {"↑↓": "Move", "space": "Drop", "esc": "Cancel"} if is_holding else {"↑↓": "Select", "space": "Pick Up", "enter": "Save"}
        ui.draw_chrome("Reorder Catalogs", keys); menu.draw(ui.get_content_win())
        key = ui.stdscr.getch()
        if key == curses.KEY_RESIZE: continue
        if key in [27, ord('q')]:
            has_changes = (catalogs != initial_catalogs_order)
            if not has_changes or ui.confirm("Unsaved Changes", "Discard reordering?"):
                break
        elif key == 32: is_holding = not is_holding
        elif key in [curses.KEY_ENTER, 10, 13] and not is_holding:
            addon_copy['manifest']['catalogs'] = catalogs
            addon.clear(); addon.update(addon_copy)
            ui.set_status("Order changed. Press 's' to save.", C_WARNING); break
        else:
            if is_holding:
                pos = menu.cursor_pos
                if key in [curses.KEY_UP, ord('k')] and pos > 0: catalogs[pos], catalogs[pos-1] = catalogs[pos-1], catalogs[pos]
                elif key in [curses.KEY_DOWN, ord('j')] and pos < len(catalogs)-1: catalogs[pos], catalogs[pos+1] = catalogs[pos+1], catalogs[pos]
            menu.handle_key(key)
    ui.breadcrumb.pop()


def run_backup_loader_screen(ui, profile):
    ui.breadcrumb.append("Load Backup")
    snapshots_dir = get_snapshots_dir()
    def get_files(): return sorted([f for f in os.listdir(snapshots_dir) if f.startswith(profile['name']) and f.endswith('.json')], reverse=True)
    files = get_files()
    if not files: ui.set_status("No backups found for this profile."); ui.breadcrumb.pop(); return
    def render(win, y, x, f, is_sel):
        base_name = f.replace(f"{profile['name']}_", "").replace(".json", "")
        try: display_name = datetime.strptime(base_name, '%Y-%m-%d_%H-%M-%S').strftime('%Y-%m-%d %I:%M %p')
        except ValueError: display_name = base_name
        ui.safe_addstr(win, y, x, f" {display_name} ", curses.color_pair(C_SELECTED if is_sel else C_DEFAULT))
    menu = Menu(ui, files, render)
    while True:
        menu.items = get_files();
        if not menu.items: break
        menu.cursor_pos = min(menu.cursor_pos, len(menu.items)-1)
        ui.draw_chrome("Load Backup", {"↑↓": "Select", "enter": "Load", "r": "Rename", "d": "Delete", "esc": "Back"})
        menu.draw(ui.get_content_win())
        key = ui.stdscr.getch(); key_char = chr(key).lower() if 32 <= key <= 126 else ''
        if key == curses.KEY_RESIZE: continue
        if key in [27, ord('q')]: break
        elif key in [curses.KEY_ENTER, 10, 13]:
            if ui.confirm("Load Backup?", "This will overwrite current addons. Continue?"):
                with open(os.path.join(snapshots_dir, menu.get_selected_item()), 'r') as f: snapshot_addons = json.load(f)
                sync_user_addons(ui, profile, snapshot_addons); break
        elif key_char == 'd':
            if ui.confirm("Delete Backup?", "Permanently delete this backup?"):
                os.remove(os.path.join(snapshots_dir, menu.get_selected_item()))
                ui.set_status("Backup deleted.", C_SUCCESS)
        elif key_char == 'r':
            new_desc = ui.prompt("Rename Backup", "Enter new backup name (no extension):")
            if new_desc:
                safe_desc = "".join(c for c in new_desc if c.isalnum() or c in (' ', '_', '-')).rstrip()
                if safe_desc:
                    new_filename = f"{profile['name']}_{safe_desc}.json"
                    os.rename(os.path.join(snapshots_dir, menu.get_selected_item()), os.path.join(snapshots_dir, new_filename))
                    ui.set_status("Backup renamed.", C_SUCCESS)
        else: menu.handle_key(key)
    ui.breadcrumb.pop()


def run_mirror_config_screen(ui, nickname):
    ui.breadcrumb.append("Mirroring")
    accounts = load_accounts(); current_mirror = accounts[nickname].get('mirrors')
    options = ["Disable Mirroring"] + [name for name in accounts if name != nickname]
    def render(win, y, x, item, is_sel):
        marker = " (Current)" if item == current_mirror else ""
        ui.safe_addstr(win, y, x, f" {item}{marker} ", curses.color_pair(C_SELECTED if is_sel else C_DEFAULT))
    menu = Menu(ui, options, render)
    if current_mirror in options: menu.cursor_pos = options.index(current_mirror)
    selection = None
    while True:
        ui.draw_chrome(f"Configure Mirror for '{nickname}'", {"↑↓": "Select", "enter": "Select", "esc": "Back"})
        menu.draw(ui.get_content_win())
        key = ui.stdscr.getch()
        if key == curses.KEY_RESIZE: continue
        if key in [27, ord('q')]: break
        elif key in [curses.KEY_ENTER, 10, 13]: selection = menu.get_selected_item(); break
        else: menu.handle_key(key)
    if selection == "Disable Mirroring":
        accounts[nickname]['mirrors'] = None; save_accounts(accounts)
        ui.set_status(f"Mirroring disabled for '{nickname}'.", C_SUCCESS)
    elif selection is not None:
        master_name = selection
        if ui.confirm("Confirm Mirror", f"Set '{master_name}' as master for '{nickname}'? This will sync now and in the future."):
            source_profile = {'name': master_name, **accounts[master_name]}
            mirror_profile = {'name': nickname, **accounts[nickname]}
            
            source_addons, err = ui.run_threaded_task(get_user_addons, args=(source_profile,), message=f"Syncing from '{master_name}'...")
            
            if err: ui.set_status(err, C_ERROR); ui.breadcrumb.pop(); return
            if sync_user_addons(ui, mirror_profile, source_addons):
                accounts[nickname]['mirrors'] = master_name; save_accounts(accounts)
                ui.set_status(f"'{nickname}' now mirrors '{master_name}'.", C_SUCCESS)
    ui.breadcrumb.pop()


def run_clone_addon_screen(ui, source_profile, source_addon):
    ui.breadcrumb.append("Clone")
    accounts = load_accounts()
    target_profiles = [name for name in accounts if name != source_profile['name']]
    if not target_profiles:
        ui.set_status("No other profiles to clone to."); ui.breadcrumb.pop(); return
    
    selected = set()
    def render(win, y, x, name, is_sel):
        status = "[X]" if name in selected else "[ ]"
        color = C_SUCCESS if name in selected else C_DEFAULT
        ui.safe_addstr(win, y, x, f" {status} {name}", curses.color_pair(C_SELECTED if is_sel else color))
    menu = Menu(ui, target_profiles, render)

    while True:
        ui.draw_chrome("Clone Addon", {"↑↓": "Select", "space": "Toggle", "enter": "Confirm", "esc": "Cancel"})
        menu.draw(ui.get_content_win())
        key = ui.stdscr.getch()
        if key == curses.KEY_RESIZE: continue
        if key in [27, ord('q')]: break
        elif key == 32:
            toggled = menu.get_selected_item()
            if toggled in selected: selected.remove(toggled)
            else: selected.add(toggled)
        elif key in [curses.KEY_ENTER, 10, 13]:
            if not selected:
                ui.set_status("No profiles selected.", C_WARNING); break
            if ui.confirm("Confirm Cloning", f"Copy '{source_addon['manifest']['name']}' to {len(selected)} profile(s)?"):
                for name in selected:
                    dest_profile = {'name': name, **accounts[name]}
                    dest_addons, err = get_user_addons(dest_profile)
                    if err:
                        ui.set_status(f"Skipping {name}: {err}", C_ERROR, 4)
                        time.sleep(1)
                        continue
                    if not any(a.get('transportUrl') == source_addon.get('transportUrl') for a in dest_addons):
                        dest_addons.append(source_addon)
                        sync_user_addons(ui, dest_profile, dest_addons)
                ui.set_status("Cloning complete!", C_SUCCESS)
            break
        else: menu.handle_key(key)
    ui.breadcrumb.pop()


# --- Standalone actions ---
def add_account(ui):
    nickname = ui.prompt("Add New Profile", "Enter a nickname for this profile:")
    if not nickname: ui.set_status("Error: Nickname cannot be empty.", C_ERROR); return
    accounts = load_accounts()
    if nickname in accounts: ui.set_status(f"Error: Profile '{nickname}' already exists.", C_ERROR); return
    profile_folder_name = "".join(c for c in nickname if c.isalnum() or c in (' ', '_')).rstrip()
    profile_path = os.path.abspath(os.path.join(get_data_dir(), "pywebview_profiles", profile_folder_name))
    os.makedirs(profile_path, exist_ok=True)
    accounts[nickname] = {"path": profile_path, "authKey": None, "mirrors": None}; save_accounts(accounts)
    ui.set_status(f"Success: Profile '{nickname}' created. Launching...", C_SUCCESS, 2)
    p = multiprocessing.Process(target=launch_pywebview_with_profile, args=(nickname, profile_path, "https://web.stremio.com/#/intro?form=login")); p.start()


def change_data_directory(ui):
    current_dir = get_data_dir()
    if not ui.confirm("Change Data Directory", f"Current: {current_dir}\nThis will NOT move existing profiles. Continue?"):
        ui.set_status("Operation cancelled."); return
    new_dir = ui.prompt("New Data Directory", "Enter the new absolute path:")
    if not new_dir: ui.set_status("Error: Path cannot be empty.", C_ERROR); return
    try:
        os.makedirs(new_dir, exist_ok=True)
        with open(os.path.join(new_dir, ".storemio_write_test"), "w") as f: f.write("test")
        os.remove(os.path.join(new_dir, ".storemio_write_test"))
    except Exception as e: ui.set_status(f"Error: Failed to use directory: {e}", C_ERROR, 4); return
    save_config({"data_dir": new_dir})
    ui.popup("Success", ["Data directory changed.", "Please restart Storemio."]); time.sleep(2); sys.exit()


# --- Main Application Loop ---

def check_mirror_sync_status(ui):
    """
    Checks for discrepancies between masters and slaves and automatically syncs them.
    """
    accounts = load_accounts()
    masters_to_slaves = {}
    for name, data in accounts.items():
        master = data.get('mirrors')
        if master:
            if master not in masters_to_slaves:
                masters_to_slaves[master] = []
            masters_to_slaves[master].append(name)

    for master_name, slaves in masters_to_slaves.items():
        for slave_name in slaves:
            ui.sync_status[slave_name] = "CHECKING"

        master_profile = {'name': master_name, **accounts.get(master_name, {})}
        master_addons, err = get_user_addons(master_profile)
        
        if err:
            for slave_name in slaves:
                ui.sync_status[slave_name] = "SYNC_FAILED"
            continue

        master_addons_json = json.dumps(master_addons, sort_keys=True)

        for slave_name in slaves:
            slave_profile = {'name': slave_name, **accounts.get(slave_name, {})}
            slave_addons, err = get_user_addons(slave_profile)
            if err:
                ui.sync_status[slave_name] = "SYNC_FAILED"
                continue
            
            slave_addons_json = json.dumps(slave_addons, sort_keys=True)
            
            if master_addons_json != slave_addons_json:
                ui.sync_status[slave_name] = "SYNCING"
                sync_successful = sync_user_addons(ui, slave_profile, master_addons, silent=True)
                if sync_successful:
                    ui.sync_status[slave_name] = "AUTO_SYNCED"
                else:
                    ui.sync_status[slave_name] = "SYNC_FAILED"
            else:
                ui.sync_status[slave_name] = "SYNCED"

def main_app_loop(stdscr):
    curses.curs_set(0); stdscr.nodelay(True); stdscr.timeout(100)
    stdscr.keypad(True)
    ui = UIManager(stdscr)

    threading.Thread(target=check_mirror_sync_status, args=(ui,), daemon=True).start()

    run_profile_list_screen(ui)

if __name__ == "__main__":
    locale.setlocale(locale.LC_ALL, '')
    if sys.platform == "win32": multiprocessing.freeze_support()
    if not sys.stdout.isatty(): print("This script must be run in an interactive terminal."); sys.exit(1)
    ensure_data_dirs()
    setup_logging()
    try: 
        curses.wrapper(main_app_loop)
    except curses.error as e: 
        logging.error(f"A curses error occurred: {e}")
        print(f"\nA curses error occurred: {e}\nYour terminal window might be too small or misconfigured.")
    except KeyboardInterrupt: 
        print("\nGoodbye! 👋")