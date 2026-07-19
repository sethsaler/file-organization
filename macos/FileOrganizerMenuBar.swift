import Cocoa
import Foundation

@main
final class FileOrganizerMenuBar: NSObject, NSApplicationDelegate {
    private let statusItem = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
    private var refreshTimer: Timer?
    private var latestStatus: [String: Any] = [:]

    private var projectRoot: String {
        if let configured = ProcessInfo.processInfo.environment["FILE_ORGANIZER_ROOT"], !configured.isEmpty {
            return configured
        }
        if let resource = Bundle.main.resourceURL?.appendingPathComponent("project-root.txt"),
           let saved = try? String(contentsOf: resource, encoding: .utf8).trimmingCharacters(in: .whitespacesAndNewlines),
           !saved.isEmpty {
            return saved
        }
        return URL(fileURLWithPath: CommandLine.arguments[0])
            .deletingLastPathComponent()
            .deletingLastPathComponent()
            .path
    }

    private var quickControlPath: String {
        URL(fileURLWithPath: projectRoot)
            .appendingPathComponent("scripts/quick_controls.py")
            .path
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        NSApp.setActivationPolicy(.accessory)
        if let button = statusItem.button {
            button.title = "FO"
            button.toolTip = "File Organizer"
        }
        rebuildMenu()
        refreshStatus()
        refreshTimer = Timer.scheduledTimer(
            timeInterval: 3.0,
            target: self,
            selector: #selector(refreshStatus),
            userInfo: nil,
            repeats: true
        )
    }

    func applicationWillTerminate(_ notification: Notification) {
        refreshTimer?.invalidate()
    }

    @objc private func refreshStatus() {
        DispatchQueue.global(qos: .utility).async { [weak self] in
            guard let self else { return }
            let result = self.runQuickControl("status")
            guard result.exitCode == 0,
                  let data = result.output.data(using: .utf8),
                  let object = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
            else { return }
            DispatchQueue.main.async {
                self.latestStatus = object
                self.rebuildMenu()
            }
        }
    }

    private func rebuildMenu() {
        let menu = NSMenu()
        let enabled = latestStatus["scheduler_enabled"] as? Bool ?? false
        let folderCount = latestStatus["enabled_folders"] as? Int ?? 0
        let pending = latestStatus["pending"] as? Int ?? 0
        let running = latestStatus["running"] as? Int ?? 0
        let backend = latestStatus["backend"] as? String

        let stateText = enabled ? "Watching \(folderCount) folder\(folderCount == 1 ? "" : "s")" : "Automatic organization paused"
        let state = NSMenuItem(title: stateText, action: nil, keyEquivalent: "")
        state.isEnabled = false
        menu.addItem(state)

        var activityParts: [String] = []
        if running > 0 { activityParts.append("\(running) running") }
        if pending > 0 { activityParts.append("\(pending) waiting") }
        if let backend, !backend.isEmpty { activityParts.append(backend) }
        if !activityParts.isEmpty {
            let activity = NSMenuItem(title: activityParts.joined(separator: " · "), action: nil, keyEquivalent: "")
            activity.isEnabled = false
            menu.addItem(activity)
        }

        menu.addItem(.separator())
        menu.addItem(item("Open File Organizer…", action: #selector(openOrganizer), key: "o"))
        menu.addItem(item(enabled ? "Pause Automatic Organization" : "Resume Automatic Organization", action: #selector(toggleWatching)))
        menu.addItem(item("Run All Enabled Now", action: #selector(runAllNow)))
        menu.addItem(item("Undo Latest Recoverable Run…", action: #selector(undoLatest)))

        if let folders = latestStatus["folders"] as? [String], !folders.isEmpty {
            let foldersItem = NSMenuItem(title: "Open Watched Folder", action: nil, keyEquivalent: "")
            let submenu = NSMenu()
            for path in folders.prefix(12) {
                let folderItem = NSMenuItem(
                    title: URL(fileURLWithPath: path).lastPathComponent,
                    action: #selector(openWatchedFolder(_:)),
                    keyEquivalent: ""
                )
                folderItem.target = self
                folderItem.representedObject = path
                submenu.addItem(folderItem)
            }
            foldersItem.submenu = submenu
            menu.addItem(foldersItem)
        }

        menu.addItem(.separator())
        menu.addItem(item("Quit Menu Bar Helper", action: #selector(quit), key: "q"))
        statusItem.menu = menu
    }

    private func item(_ title: String, action: Selector, key: String = "") -> NSMenuItem {
        let item = NSMenuItem(title: title, action: action, keyEquivalent: key)
        item.target = self
        return item
    }

    @objc private func openOrganizer() {
        runInBackground("open")
    }

    @objc private func toggleWatching() {
        runInBackground("toggle")
    }

    @objc private func runAllNow() {
        runInBackground("run-all")
    }

    @objc private func undoLatest() {
        let alert = NSAlert()
        alert.messageText = "Undo the latest recoverable run?"
        alert.informativeText = "Files will be moved back using the latest available recovery manifest. Existing files will not be overwritten."
        alert.addButton(withTitle: "Undo Latest Run")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        if alert.runModal() == .alertFirstButtonReturn {
            runInBackground("undo-latest")
        }
    }

    @objc private func openWatchedFolder(_ sender: NSMenuItem) {
        guard let path = sender.representedObject as? String else { return }
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    @objc private func quit() {
        NSApp.terminate(nil)
    }

    private func runInBackground(_ command: String) {
        DispatchQueue.global(qos: .userInitiated).async { [weak self] in
            guard let self else { return }
            let result = self.runQuickControl(command)
            DispatchQueue.main.async {
                self.refreshStatus()
                if result.exitCode != 0 {
                    let alert = NSAlert()
                    alert.messageText = "File Organizer"
                    alert.informativeText = result.output.isEmpty ? "The action could not be completed." : result.output
                    NSApp.activate(ignoringOtherApps: true)
                    alert.runModal()
                }
            }
        }
    }

    private func runQuickControl(_ command: String) -> (exitCode: Int32, output: String) {
        let process = Process()
        let pipe = Pipe()
        process.executableURL = URL(fileURLWithPath: "/usr/bin/env")
        process.arguments = ["python3", quickControlPath, command]
        process.currentDirectoryURL = URL(fileURLWithPath: projectRoot)
        process.standardOutput = pipe
        process.standardError = pipe
        do {
            try process.run()
            process.waitUntilExit()
            let data = pipe.fileHandleForReading.readDataToEndOfFile()
            return (process.terminationStatus, String(data: data, encoding: .utf8) ?? "")
        } catch {
            return (1, error.localizedDescription)
        }
    }
}
