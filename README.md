Storemio

A powerful, terminal-based manager for your Stremio addon collections.

Storemio is a feature-rich, cross-platform TUI (Text-based User Interface) application built with Python. It's designed to give you complete control over your Stremio addon configurations across multiple accounts, offering features like profile mirroring, backups, and detailed addon management that go beyond the official Stremio app.
✨ Key Features

    👤 Multi-Profile Management: Add and manage multiple Stremio accounts, each with its own isolated settings and addon collection.

    🔧 Granular Addon Control: Go beyond simple installation with fine-tuned management:

        Reorder Addons: Change the priority of your entire addon list to control how content appears in Stremio.

        Advanced Catalog Management:

            Selectively enable or disable specific catalogs within any addon.

            Reorder the catalogs inside an addon to prioritize your preferred content sources.

        Rename & Clone: Give addons custom names for clarity and quickly copy any addon's configuration between your profiles.

    🔄 Profile Mirroring: Designate a "master" profile. Storemio will automatically keep other "mirrored" profiles in perfect sync with the master's addon list, ideal for managing family accounts or multiple devices.

    💾 Backup & Restore: Create timestamped snapshots of a profile's addon setup. You can easily restore a previous configuration at any time, protecting you from bad addon updates or accidental changes.

    🚀 Integrated Stremio Launcher: Launch the Stremio Web UI in a dedicated window for any of your managed profiles directly from the app. Storemio automatically handles creating isolated browser profiles to keep your accounts separate.

    💻 Modern Terminal UI: A fast, responsive, and keyboard-driven interface built with curses that works great in modern terminals on Windows, macOS, and Linux.

🚀 Getting Started

There are two ways to get Storemio running on your system.
Option 1: From a Pre-built Executable (Recommended)

You can find pre-built, single-file executables for Windows on the Releases page of this repository. Just download the latest version and run it.
Option 2: Running from Source

If you have Python installed, you can run Storemio directly from the source code.

    Clone the repository:

    git clone [https://github.com/CmdPrmpt/Storemio.git](https://github.com/CmdPrmpt/Storemio.git)
    cd Storemio

    Create a virtual environment (recommended):

    python -m venv venv
    # On Windows
    .\venv\Scripts\activate
    # On macOS/Linux
    source venv/bin/activate

    Install the required dependencies:
    The application requires a few Python packages to run.

    pip install requests pywebview pyperclip

    For Windows Users: You also need to install the windows-curses library.

    pip install windows-curses

    Run the application:

    python storemio.py

🛠️ Building from Source

This project uses PyInstaller to create a single-file executable.

    Run the build script (Windows):
    The provided build.bat script automates the process of checking for dependencies and building the .exe. Simply run it:

    .\build.bat

    Build Manually (All Platforms):
    You can also run the PyInstaller command directly. This is useful for building on macOS or Linux.

    pyinstaller --onefile --icon=icon.ico storemio.py

The final executable will be located in the dist directory.
📄 License

This project is licensed under the MIT License. See the LICENSE file for details.
