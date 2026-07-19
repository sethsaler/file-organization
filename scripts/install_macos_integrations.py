#!/usr/bin/env python3
"""Install the optional native menu-bar helper and Finder Quick Action."""

from __future__ import annotations

import argparse
import os
import plistlib
import shlex
import shutil
import subprocess
import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parent.parent
SWIFT_SOURCE = PROJECT_ROOT / "macos" / "FileOrganizerMenuBar.swift"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Install File Organizer macOS integrations")
    parser.add_argument("--all", action="store_true", help="Install menu bar app and Finder Quick Action")
    parser.add_argument("--menu-bar", action="store_true", help="Install the menu bar app")
    parser.add_argument("--finder", action="store_true", help="Install the Finder Quick Action")
    parser.add_argument("--launch", action="store_true", help="Launch the menu bar app after installing")
    parser.add_argument(
        "--target-root",
        type=Path,
        default=None,
        help="Testing/portable root; writes Applications and Library/Services below it",
    )
    return parser.parse_args()


def application_root(target_root: Path | None) -> Path:
    return (target_root / "Applications") if target_root else (Path.home() / "Applications")


def services_root(target_root: Path | None) -> Path:
    return (target_root / "Library" / "Services") if target_root else (Path.home() / "Library" / "Services")


def build_menu_bar_app(target_root: Path | None) -> Path:
    if sys.platform != "darwin":
        raise RuntimeError("The menu bar helper requires macOS")
    if not SWIFT_SOURCE.is_file():
        raise RuntimeError(f"Missing Swift source: {SWIFT_SOURCE}")
    swiftc = shutil.which("swiftc")
    if not swiftc:
        xcrun = shutil.which("xcrun")
        if xcrun:
            found = subprocess.run([xcrun, "--find", "swiftc"], capture_output=True, text=True, check=False)
            swiftc = found.stdout.strip() if found.returncode == 0 else None
    if not swiftc:
        raise RuntimeError("Swift compiler not found; install Xcode Command Line Tools")

    app = application_root(target_root) / "File Organizer Menu Bar.app"
    executable = app / "Contents" / "MacOS" / "FileOrganizerMenuBar"
    resources = app / "Contents" / "Resources"
    executable.parent.mkdir(parents=True, exist_ok=True)
    resources.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [swiftc, "-parse-as-library", str(SWIFT_SOURCE), "-framework", "Cocoa", "-o", str(executable)],
        check=True,
    )
    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": "File Organizer Menu Bar",
        "CFBundleExecutable": "FileOrganizerMenuBar",
        "CFBundleIdentifier": "com.sethsaler.file-organizer-menubar",
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": "File Organizer Menu Bar",
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.0",
        "CFBundleVersion": "1",
        "LSMinimumSystemVersion": "12.0",
        "LSUIElement": True,
        "NSHighResolutionCapable": True,
    }
    with (app / "Contents" / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)
    (resources / "project-root.txt").write_text(str(PROJECT_ROOT), encoding="utf-8")
    return app


def install_finder_service(target_root: Path | None) -> Path:
    workflow = services_root(target_root) / "Preview in File Organizer.workflow"
    contents = workflow / "Contents"
    contents.mkdir(parents=True, exist_ok=True)
    action_script = PROJECT_ROOT / "scripts" / "finder_quick_action.py"
    shell_command = f"exec {shlex.quote(sys.executable)} {shlex.quote(str(action_script))} \"$@\""
    document = {
        "AMApplicationBuild": "512",
        "AMApplicationVersion": "2.10",
        "AMDocumentVersion": "2",
        "actions": [
            {
                "action": {
                    "AMAccepts": {"Container": "List", "Optional": True, "Types": ["com.apple.cocoa.path"]},
                    "AMActionVersion": "2.0.3",
                    "AMApplication": ["Automator"],
                    "AMParameterProperties": {},
                    "AMProvides": {"Container": "List", "Types": ["com.apple.cocoa.path"]},
                    "ActionBundlePath": "/System/Library/Automator/Run Shell Script.action",
                    "ActionName": "Run Shell Script",
                    "ActionParameters": {
                        "COMMAND_STRING": shell_command,
                        "CheckedForUserDefaultShell": True,
                        "inputMethod": 1,
                        "shell": "/bin/zsh",
                    },
                    "BundleIdentifier": "com.apple.RunShellScript",
                    "CFBundleVersion": "2.0.3",
                    "CanShowSelectedItemsWhenRun": False,
                    "CanShowWhenRun": True,
                    "Category": ["AMCategoryUtilities"],
                    "Class Name": "RunShellScriptAction",
                    "InputUUID": "FILE-ORGANIZER-INPUT",
                    "OutputUUID": "FILE-ORGANIZER-OUTPUT",
                    "UUID": "FILE-ORGANIZER-ACTION",
                },
                "isViewVisible": True,
            }
        ],
        "connectors": {},
        "workflowMetaData": {
            "serviceApplicationBundleID": "com.apple.finder",
            "serviceApplicationPath": "/System/Library/CoreServices/Finder.app",
            "serviceInputTypeIdentifier": "com.apple.Automator.fileSystemObject",
            "serviceOutputTypeIdentifier": "com.apple.Automator.nothing",
            "serviceProcessesInput": 0,
            "workflowTypeIdentifier": "com.apple.Automator.servicesMenu",
        },
    }
    with (contents / "document.wflow").open("wb") as handle:
        plistlib.dump(document, handle)
    info = {
        "CFBundleIdentifier": "com.sethsaler.file-organizer.preview-service",
        "CFBundleName": "Preview in File Organizer",
        "CFBundlePackageType": "BNDL",
        "NSServices": [
            {
                "NSMenuItem": {"default": "Preview in File Organizer"},
                "NSMessage": "runWorkflowAsService",
                "NSPortName": "Preview in File Organizer",
                "NSSendFileTypes": ["public.item"],
            }
        ],
    }
    with (contents / "Info.plist").open("wb") as handle:
        plistlib.dump(info, handle)
    return workflow


def launch_app(app: Path) -> None:
    env = dict(os.environ)
    env["FILE_ORGANIZER_ROOT"] = str(PROJECT_ROOT)
    executable = app / "Contents" / "MacOS" / "FileOrganizerMenuBar"
    subprocess.Popen([str(executable)], env=env, start_new_session=True)


def main() -> None:
    args = parse_args()
    install_menu = args.all or args.menu_bar or not (args.menu_bar or args.finder)
    install_finder = args.all or args.finder or not (args.menu_bar or args.finder)
    installed = []
    app = None
    if install_menu:
        app = build_menu_bar_app(args.target_root)
        installed.append(str(app))
    if install_finder:
        installed.append(str(install_finder_service(args.target_root)))
    if args.launch:
        if app is None:
            app = build_menu_bar_app(args.target_root)
        launch_app(app)
    print("Installed:")
    for path in installed:
        print(f"  {path}")


if __name__ == "__main__":
    main()
