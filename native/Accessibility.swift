// Reading and driving other apps' user interfaces through the Accessibility API.
//
// This is how Jeeves handles apps with no AppleScript dictionary — WhatsApp,
// Slack, Electron and Catalyst apps generally. It reads the real string values
// out of the UI tree rather than OCR'ing pixels, so it is exact and fast.
//
// Everything here is local: no network, no model, no API key. It needs only
// Accessibility permission.
//
//   ui-dump <app> [--max N] [--roles]   text (or roles) from an app's UI tree
//   ui-type <text>                      type into whatever has focus
//   wa-chats                            WhatsApp chat list, JSON
//   wa-unread                           WhatsApp unread chats + messages, JSON
//   wa-read <chat> [--max N]            open a chat and read it, JSON
//   wa-send <chat> <text>               open a chat and send a message

import AppKit
import ApplicationServices
import Foundation

// MARK: - Attribute plumbing

/// Read one accessibility attribute, or nil.
func axValue(_ element: AXUIElement, _ attribute: String) -> CFTypeRef? {
    var out: CFTypeRef?
    guard AXUIElementCopyAttributeValue(element, attribute as CFString, &out) == .success else {
        return nil
    }
    return out
}

func axString(_ element: AXUIElement, _ attribute: String) -> String? {
    guard let raw = axValue(element, attribute) else { return nil }
    if let text = raw as? String { return text }
    if let number = raw as? NSNumber { return number.stringValue }
    return nil
}

func axChildren(_ element: AXUIElement) -> [AXUIElement] {
    (axValue(element, kAXChildrenAttribute as String) as? [AXUIElement]) ?? []
}

func axRole(_ element: AXUIElement) -> String {
    axString(element, kAXRoleAttribute as String) ?? "?"
}

/// The human-visible text of a node, from whichever attribute carries it.
func axText(_ element: AXUIElement) -> String? {
    for attribute in [
        kAXValueAttribute as String,
        kAXTitleAttribute as String,
        kAXDescriptionAttribute as String,
    ] {
        if let text = axString(element, attribute)?
            .trimmingCharacters(in: .whitespacesAndNewlines), !text.isEmpty {
            return text
        }
    }
    return nil
}

// MARK: - Locating an application

struct AppTarget {
    let app: NSRunningApplication
    let element: AXUIElement
}

func requireAccessibility() {
    guard AXIsProcessTrusted() else {
        fail(
            "Accessibility permission is required to read other apps' windows. "
            + "Grant it to jeeves-native (or your terminal) under System Settings → "
            + "Privacy & Security → Accessibility, then try again.",
            code: 77
        )
    }
}

/// Find a running app by name or bundle id, launching it if asked.
func findApp(_ name: String, launch: Bool = false, waitSeconds: Double = 12) -> AppTarget {
    requireAccessibility()

    func lookup() -> NSRunningApplication? {
        let running = NSWorkspace.shared.runningApplications
        let wanted = name.lowercased()
        return running.first {
            ($0.localizedName?.lowercased() == wanted)
                || ($0.bundleIdentifier?.lowercased() == wanted)
        } ?? running.first {
            ($0.localizedName?.lowercased().contains(wanted) ?? false)
                && $0.activationPolicy == .regular
        }
    }

    if let found = lookup() {
        return AppTarget(app: found, element: AXUIElementCreateApplication(found.processIdentifier))
    }
    guard launch else {
        fail("\(name) is not running. Open it first, or pass --launch.", code: 4)
    }

    // Launch and wait for it to register with the window server.
    let task = Process()
    task.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    task.arguments = ["-a", name]
    try? task.run()
    task.waitUntilExit()

    let deadline = Date().addingTimeInterval(waitSeconds)
    while Date() < deadline {
        if let found = lookup() {
            Thread.sleep(forTimeInterval: 1.5)  // let the first window draw
            return AppTarget(
                app: found, element: AXUIElementCreateApplication(found.processIdentifier))
        }
        Thread.sleep(forTimeInterval: 0.4)
    }
    fail("\(name) did not start within \(Int(waitSeconds))s.", code: 4)
}

// MARK: - Walking the tree

struct Node {
    let role: String
    let text: String?
    let depth: Int
    let element: AXUIElement
    let identifier: String?
}

/// Depth-first walk with hard caps, because some UI trees are enormous.
func walk(
    _ root: AXUIElement,
    maxNodes: Int = 6000,
    maxDepth: Int = 40,
    visit: (Node) -> Bool
) {
    var stack: [(AXUIElement, Int)] = [(root, 0)]
    var seen = 0
    while let (element, depth) = stack.popLast() {
        seen += 1
        if seen > maxNodes { return }
        let node = Node(
            role: axRole(element),
            text: axText(element),
            depth: depth,
            element: element,
            identifier: axString(element, kAXIdentifierAttribute as String)
        )
        if !visit(node) { return }
        if depth < maxDepth {
            // Reversed so the visit order matches visual order.
            for child in axChildren(element).reversed() {
                stack.append((child, depth + 1))
            }
        }
    }
}

func collectText(_ root: AXUIElement, limit: Int) -> [String] {
    var lines: [String] = []
    var seen = Set<String>()
    walk(root) { node in
        if let text = node.text, text.count > 1, !seen.contains(text) {
            seen.insert(text)
            lines.append(text)
        }
        return lines.count < limit
    }
    return lines
}

/// First element whose role is in `roles` and which passes `where`.
func firstElement(
    _ root: AXUIElement,
    roles: Set<String>,
    where predicate: (Node) -> Bool = { _ in true }
) -> AXUIElement? {
    var hit: AXUIElement?
    walk(root) { node in
        if roles.contains(node.role), predicate(node) {
            hit = node.element
            return false
        }
        return true
    }
    return hit
}

// MARK: - Typing

/// Type a string into whatever currently has keyboard focus.
///
/// Synthesised key events work in Catalyst and Electron apps where setting
/// AXValue on a rich text field silently does nothing.
func typeText(_ text: String) {
    guard let source = CGEventSource(stateID: .combinedSessionState) else { return }
    // Send in small chunks: very long unicode payloads get dropped.
    for chunk in Array(text).chunked(into: 18) {
        let piece = String(chunk)
        guard let down = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: true),
              let up = CGEvent(keyboardEventSource: source, virtualKey: 0, keyDown: false)
        else { continue }
        var utf16 = Array(piece.utf16)
        down.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: &utf16)
        up.keyboardSetUnicodeString(stringLength: utf16.count, unicodeString: &utf16)
        down.post(tap: .cghidEventTap)
        up.post(tap: .cghidEventTap)
        Thread.sleep(forTimeInterval: 0.012)
    }
}

func pressKey(_ virtualKey: CGKeyCode, flags: CGEventFlags = []) {
    guard let source = CGEventSource(stateID: .combinedSessionState) else { return }
    let down = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: true)
    let up = CGEvent(keyboardEventSource: source, virtualKey: virtualKey, keyDown: false)
    down?.flags = flags
    up?.flags = flags
    down?.post(tap: .cghidEventTap)
    up?.post(tap: .cghidEventTap)
    Thread.sleep(forTimeInterval: 0.06)
}

let KEY_RETURN: CGKeyCode = 36
let KEY_A: CGKeyCode = 0
let KEY_DELETE: CGKeyCode = 51
let KEY_ESCAPE: CGKeyCode = 53

extension Array {
    func chunked(into size: Int) -> [[Element]] {
        stride(from: 0, to: count, by: size).map {
            Array(self[$0..<Swift.min($0 + size, count)])
        }
    }
}

// MARK: - Generic commands

func uiDump(app: String, limit: Int, showRoles: Bool) -> Never {
    let target = findApp(app)
    if showRoles {
        var rows: [String] = []
        walk(target.element) { node in
            let text = node.text.map { " \"\($0.prefix(70))\"" } ?? ""
            let ident = node.identifier.map { " #\($0)" } ?? ""
            rows.append(String(repeating: "  ", count: node.depth) + node.role + ident + text)
            return rows.count < limit
        }
        print(rows.joined(separator: "\n"))
    } else {
        print(collectText(target.element, limit: limit).joined(separator: "\n"))
    }
    exit(0)
}

func uiType(_ text: String) -> Never {
    requireAccessibility()
    typeText(text)
    print("typed \(text.count) character(s)")
    exit(0)
}

// MARK: - WhatsApp

// WhatsApp Desktop ships no AppleScript dictionary, so everything below reads
// and drives its Accessibility tree. Roles are matched loosely because the
// layout differs between the Catalyst and Electron builds, and changes between
// releases. `ui-dump WhatsApp --roles` is the debugging tool when it drifts.

let WA_NAME = "WhatsApp"
let CHAT_CONTAINER_ROLES: Set<String> = [
    kAXTableRole as String, kAXOutlineRole as String, kAXListRole as String,
    "AXList", "AXTable",
]
let TEXT_INPUT_ROLES: Set<String> = [
    kAXTextAreaRole as String, kAXTextFieldRole as String,
]

func waApp(launch: Bool = true) -> AppTarget {
    let target = findApp(WA_NAME, launch: launch)
    target.app.activate()
    Thread.sleep(forTimeInterval: 0.5)
    return target
}

/// Rows in the sidebar. Each row's text usually reads:
/// name, timestamp, message preview, and "N unread messages" when unread.
func waChatRows(_ target: AppTarget) -> [[String: Any]] {
    var rows: [[String: Any]] = []
    walk(target.element) { node in
        guard node.role == (kAXRowRole as String) || node.role == (kAXCellRole as String)
        else { return true }
        var parts: [String] = []
        walk(node.element, maxNodes: 60, maxDepth: 6) { child in
            if let text = child.text, text.count > 0, !parts.contains(text) {
                parts.append(text)
            }
            return true
        }
        guard let name = parts.first, !name.isEmpty else { return true }
        let joined = parts.joined(separator: " │ ")
        let unread = waUnreadCount(from: parts)
        rows.append([
            "name": name,
            "preview": parts.count > 1 ? parts[1...].joined(separator: " ") : "",
            "unread": unread,
            "raw": joined,
        ])
        return rows.count < 120
    }
    return rows
}

/// WhatsApp exposes unread state as a label such as "3 unread messages".
func waUnreadCount(from parts: [String]) -> Int {
    for part in parts {
        let lower = part.lowercased()
        guard lower.contains("unread") else { continue }
        let digits = lower.components(separatedBy: CharacterSet.decimalDigits.inverted)
            .filter { !$0.isEmpty }
        if let first = digits.first, let value = Int(first) { return value }
        return 1  // "unread message" with no number
    }
    return 0
}

func waChats() -> Never {
    let target = waApp(launch: false)
    emit(waChatRows(target))
    exit(0)
}

func waUnread() -> Never {
    let target = waApp(launch: false)
    let unread = waChatRows(target).filter { ($0["unread"] as? Int ?? 0) > 0 }
    emit(unread)
    exit(0)
}

/// Focus the sidebar search box, type a name, and open the first result.
func waOpenChat(_ target: AppTarget, _ chat: String) -> Bool {
    let search = firstElement(target.element, roles: TEXT_INPUT_ROLES.union([
        "AXSearchField",
    ])) { node in
        let hint = (
            (axString(node.element, kAXPlaceholderValueAttribute as String) ?? "")
            + " " + (node.identifier ?? "")
            + " " + (axString(node.element, kAXDescriptionAttribute as String) ?? "")
        ).lowercased()
        return hint.contains("search") || hint.contains("find")
    }
    guard let search else { return false }

    AXUIElementSetAttributeValue(search, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    Thread.sleep(forTimeInterval: 0.35)
    pressKey(KEY_A, flags: .maskCommand)   // select any existing query
    pressKey(KEY_DELETE)
    typeText(chat)
    Thread.sleep(forTimeInterval: 1.4)     // let results filter
    pressKey(KEY_RETURN)                   // open the top hit
    Thread.sleep(forTimeInterval: 1.2)
    return true
}

/// Messages in the open conversation, oldest of the visible window first.
func waMessages(_ target: AppTarget, limit: Int) -> [String] {
    // The transcript is the largest scroll area / list of static text.
    var best: [String] = []
    walk(target.element) { node in
        guard CHAT_CONTAINER_ROLES.contains(node.role)
            || node.role == (kAXScrollAreaRole as String) else { return true }
        var texts: [String] = []
        walk(node.element, maxNodes: 3000, maxDepth: 25) { child in
            if child.role == (kAXStaticTextRole as String) || child.role == "AXStaticText",
               let text = child.text, text.count > 1 {
                texts.append(text)
            }
            return true
        }
        if texts.count > best.count { best = texts }
        return true
    }
    return best.suffix(limit)
}

func waRead(chat: String, limit: Int) -> Never {
    let target = waApp()
    if !chat.isEmpty, !waOpenChat(target, chat) {
        fail("could not find WhatsApp's search box — run `ui-dump WhatsApp --roles` to inspect", code: 5)
    }
    emit(["chat": chat, "messages": waMessages(target, limit: limit)])
    exit(0)
}

func waSend(chat: String, text: String) -> Never {
    guard !text.isEmpty else { fail("message text is empty") }
    let target = waApp()
    if !waOpenChat(target, chat) {
        fail("could not find WhatsApp's search box — run `ui-dump WhatsApp --roles` to inspect", code: 5)
    }

    // The compose box is a text input whose placeholder mentions a message.
    let compose = firstElement(target.element, roles: TEXT_INPUT_ROLES) { node in
        let hint = (
            (axString(node.element, kAXPlaceholderValueAttribute as String) ?? "")
            + " " + (node.identifier ?? "")
            + " " + (axString(node.element, kAXDescriptionAttribute as String) ?? "")
        ).lowercased()
        return hint.contains("message") || hint.contains("type")
    } ?? firstElement(target.element, roles: [kAXTextAreaRole as String])

    guard let compose else {
        fail(
            "could not find WhatsApp's message box. The layout may have changed — "
            + "run `ui-dump WhatsApp --roles` and send me the output.",
            code: 5
        )
    }
    AXUIElementSetAttributeValue(compose, kAXFocusedAttribute as CFString, kCFBooleanTrue)
    Thread.sleep(forTimeInterval: 0.35)
    typeText(text)
    Thread.sleep(forTimeInterval: 0.35)
    pressKey(KEY_RETURN)
    Thread.sleep(forTimeInterval: 0.4)
    emit(["sent_to": chat, "text": text])
    exit(0)
}
