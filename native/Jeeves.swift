// Jeeves native helper — the parts of macOS that have no good CLI.
//
//   ocr <image> [--fast]        on-device text recognition (Vision)
//   listen [--timeout N]        on-device speech to text (Speech + AVAudioEngine)
//   brightness <0.0-1.0>        built-in display brightness
//   events <days>               calendar events, JSON (EventKit)
//   add-event <json>            create a calendar event (EventKit)
//   reminders [list]            open reminders, JSON (EventKit)
//   add-reminder <json>         create a reminder (EventKit)
//   complete-reminder <id>      mark a reminder done (EventKit)
//   contacts <query>            look up people, JSON (Contacts)
//   bar <jeeves-cli-path>       menu-bar app with a global hotkey
//
// Built by scripts/build_native.sh, which embeds an Info.plist so macOS can show
// the right permission prompts, then ad-hoc signs the binary so those grants stick.

import AVFoundation
import AppKit
import Contacts
import EventKit
import Foundation
import Speech
import Vision

// MARK: - Plumbing

func fail(_ message: String, code: Int32 = 1) -> Never {
    FileHandle.standardError.write(("jeeves-native: " + message + "\n").data(using: .utf8)!)
    exit(code)
}

func emit(_ value: Any) {
    guard JSONSerialization.isValidJSONObject(value) || value is [Any] else {
        print(value)
        return
    }
    if let data = try? JSONSerialization.data(withJSONObject: value, options: [.prettyPrinted, .sortedKeys]),
       let text = String(data: data, encoding: .utf8) {
        print(text)
    }
}

let iso: ISO8601DateFormatter = {
    let f = ISO8601DateFormatter()
    f.formatOptions = [.withInternetDateTime]
    f.timeZone = TimeZone.current
    return f
}()

/// Block on an async permission request without spinning the CPU.
func awaitAuth(_ request: @escaping (@escaping (Bool, Error?) -> Void) -> Void) -> (Bool, Error?) {
    let sem = DispatchSemaphore(value: 0)
    var granted = false
    var failure: Error?
    request { ok, err in
        granted = ok
        failure = err
        sem.signal()
    }
    sem.wait()
    return (granted, failure)
}

// MARK: - OCR (Vision)

/// Redraw an image onto opaque white.
///
/// Vision's text recogniser silently returns zero observations for images whose
/// alpha is non-premultiplied (`CGImageAlphaInfo.last`) — which is exactly what
/// `screencapture` and `sips` emit. Flattening first makes OCR reliable.
func flatten(_ image: CGImage) -> CGImage {
    guard let ctx = CGContext(
        data: nil,
        width: image.width,
        height: image.height,
        bitsPerComponent: 8,
        bytesPerRow: 0,
        space: CGColorSpaceCreateDeviceRGB(),
        bitmapInfo: CGImageAlphaInfo.noneSkipLast.rawValue
    ) else { return image }
    let rect = CGRect(x: 0, y: 0, width: image.width, height: image.height)
    ctx.setFillColor(CGColor(red: 1, green: 1, blue: 1, alpha: 1))
    ctx.fill(rect)
    ctx.draw(image, in: rect)
    return ctx.makeImage() ?? image
}

func runOCR(path: String, fast: Bool) -> Never {
    let url = URL(fileURLWithPath: path)
    guard let source = CGImageSourceCreateWithURL(url as CFURL, nil),
          let decoded = CGImageSourceCreateImageAtIndex(source, 0, nil)
    else {
        fail("could not read image at \(path)")
    }
    let image = flatten(decoded)

    let request = VNRecognizeTextRequest()
    request.recognitionLevel = fast ? .fast : .accurate
    request.usesLanguageCorrection = !fast
    request.automaticallyDetectsLanguage = true

    do {
        try VNImageRequestHandler(cgImage: image, options: [:]).perform([request])
    } catch {
        fail("OCR failed: \(error.localizedDescription)")
    }

    let observations = request.results ?? []
    // Group into visual lines top-to-bottom, then left-to-right, so the output
    // reads like the screen instead of like Vision's internal ordering. The
    // tolerance scales with glyph height so it works on both a dense screenshot
    // and a sparse full-page scan.
    func tolerance(_ obs: VNRecognizedTextObservation) -> CGFloat {
        max(0.004, obs.boundingBox.height * 0.6)
    }

    let sorted = observations.sorted { a, b in
        let dy = (1 - a.boundingBox.midY) - (1 - b.boundingBox.midY)
        if abs(dy) > min(tolerance(a), tolerance(b)) { return dy < 0 }
        return a.boundingBox.minX < b.boundingBox.minX
    }

    var lines: [String] = []
    var buffer: [String] = []
    var currentY: CGFloat = -1
    var currentTol: CGFloat = 0
    for obs in sorted {
        guard let text = obs.topCandidates(1).first?.string else { continue }
        let y = 1 - obs.boundingBox.midY
        if currentY < 0 {
            buffer = [text]
            currentY = y
            currentTol = tolerance(obs)
        } else if abs(y - currentY) <= min(currentTol, tolerance(obs)) {
            buffer.append(text)
        } else {
            lines.append(buffer.joined(separator: "  "))
            buffer = [text]
            currentY = y
            currentTol = tolerance(obs)
        }
    }
    if !buffer.isEmpty { lines.append(buffer.joined(separator: "  ")) }

    print(lines.joined(separator: "\n"))
    exit(0)
}

// MARK: - Speech to text

final class Listener: NSObject {
    private let engine = AVAudioEngine()
    private let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US"))
    private var request: SFSpeechAudioBufferRecognitionRequest?
    private var task: SFSpeechRecognitionTask?
    private var transcript = ""
    private var lastChange = Date()
    private let silenceLimit: TimeInterval
    private let hardLimit: TimeInterval
    private let done = DispatchSemaphore(value: 0)

    init(silence: TimeInterval, timeout: TimeInterval) {
        self.silenceLimit = silence
        self.hardLimit = timeout
        super.init()
    }

    func listen() -> String {
        let (speechOK, _) = awaitAuth { done in
            SFSpeechRecognizer.requestAuthorization { status in
                done(status == .authorized, nil)
            }
        }
        guard speechOK else {
            fail("speech recognition was not authorised. Grant Speech Recognition to your terminal in System Settings → Privacy & Security.", code: 77)
        }

        let (micOK, _) = awaitAuth { done in
            AVCaptureDevice.requestAccess(for: .audio) { ok in done(ok, nil) }
        }
        guard micOK else {
            fail("microphone access was denied. Grant Microphone to your terminal in System Settings → Privacy & Security.", code: 77)
        }

        guard let recognizer, recognizer.isAvailable else {
            fail("no speech recogniser is available for en-US", code: 78)
        }

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        // Keep audio on the device; never send it to a server.
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        self.request = request

        let input = engine.inputNode
        let format = input.outputFormat(forBus: 0)
        guard format.sampleRate > 0 else { fail("no usable audio input device", code: 79) }

        input.installTap(onBus: 0, bufferSize: 1024, format: format) { buffer, _ in
            request.append(buffer)
        }

        task = recognizer.recognitionTask(with: request) { [weak self] result, error in
            guard let self else { return }
            if let result {
                let text = result.bestTranscription.formattedString
                if text != self.transcript {
                    self.transcript = text
                    self.lastChange = Date()
                }
                if result.isFinal { self.done.signal() }
            }
            if error != nil { self.done.signal() }
        }

        engine.prepare()
        do { try engine.start() } catch {
            fail("could not start audio engine: \(error.localizedDescription)", code: 79)
        }

        // Watchdog: end the utterance after a gap of silence, or at the hard cap.
        let started = Date()
        let watchdog = DispatchQueue(label: "jeeves.listen.watchdog")
        watchdog.async { [weak self] in
            while let self {
                Thread.sleep(forTimeInterval: 0.1)
                let idle = Date().timeIntervalSince(self.lastChange)
                let total = Date().timeIntervalSince(started)
                if (!self.transcript.isEmpty && idle > self.silenceLimit) || total > self.hardLimit {
                    self.done.signal()
                    return
                }
            }
        }

        done.wait()
        engine.inputNode.removeTap(onBus: 0)
        engine.stop()
        request.endAudio()
        task?.cancel()
        return transcript.trimmingCharacters(in: .whitespacesAndNewlines)
    }
}

// MARK: - Brightness

func setBrightness(_ level: Double) -> Never {
    let clamped = max(0.0, min(1.0, level))
    // DisplayServices is private but is the only thing that works on Apple silicon.
    typealias SetBrightness = @convention(c) (CGDirectDisplayID, Float) -> Int32
    let candidates = [
        "/System/Library/PrivateFrameworks/DisplayServices.framework/DisplayServices",
        "/System/Library/Frameworks/CoreDisplay.framework/CoreDisplay",
    ]
    for path in candidates {
        guard let handle = dlopen(path, RTLD_LAZY) else { continue }
        defer { dlclose(handle) }
        for symbol in ["DisplayServicesSetBrightness", "CoreDisplay_Display_SetUserBrightness"] {
            guard let sym = dlsym(handle, symbol) else { continue }
            let fn = unsafeBitCast(sym, to: SetBrightness.self)
            if fn(CGMainDisplayID(), Float(clamped)) == 0 {
                print("brightness set to \(Int(clamped * 100))%")
                exit(0)
            }
        }
    }
    fail("this Mac does not expose a writable brightness control", code: 80)
}

// MARK: - EventKit

let store = EKEventStore()

func requireCalendar() {
    let (ok, err) = awaitAuth { done in
        store.requestFullAccessToEvents { granted, error in done(granted, error) }
    }
    guard ok else {
        fail("calendar access denied\(err.map { ": \($0.localizedDescription)" } ?? ""). Grant Calendars to your terminal in System Settings → Privacy & Security.", code: 77)
    }
}

func requireReminders() {
    let (ok, err) = awaitAuth { done in
        store.requestFullAccessToReminders { granted, error in done(granted, error) }
    }
    guard ok else {
        fail("reminders access denied\(err.map { ": \($0.localizedDescription)" } ?? ""). Grant Reminders to your terminal in System Settings → Privacy & Security.", code: 77)
    }
}

func listEvents(days: Int) -> Never {
    requireCalendar()
    let start = Date()
    guard let end = Calendar.current.date(byAdding: .day, value: max(1, days), to: start) else {
        fail("bad day range")
    }
    let predicate = store.predicateForEvents(withStart: start, end: end, calendars: nil)
    let events = store.events(matching: predicate).sorted { $0.startDate < $1.startDate }
    emit(events.map { event -> [String: Any] in
        [
            "id": event.eventIdentifier ?? "",
            "title": event.title ?? "(untitled)",
            "calendar": event.calendar?.title ?? "",
            "start": iso.string(from: event.startDate),
            "end": iso.string(from: event.endDate),
            "all_day": event.isAllDay,
            "location": event.location ?? "",
            "notes": String((event.notes ?? "").prefix(500)),
            "url": event.url?.absoluteString ?? "",
        ]
    })
    exit(0)
}

func decodePayload(_ raw: String) -> [String: Any] {
    guard let data = raw.data(using: .utf8),
          let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any]
    else { fail("payload must be a JSON object") }
    return obj
}

func addEvent(_ raw: String) -> Never {
    requireCalendar()
    let payload = decodePayload(raw)
    guard let title = payload["title"] as? String, !title.isEmpty else {
        fail("title is required")
    }
    guard let startText = payload["start"] as? String, let start = iso.date(from: startText) else {
        fail("start must be an ISO-8601 datetime")
    }
    let minutes = (payload["duration_minutes"] as? Int) ?? 60
    let end = (payload["end"] as? String).flatMap { iso.date(from: $0) }
        ?? start.addingTimeInterval(TimeInterval(minutes * 60))

    let event = EKEvent(eventStore: store)
    event.title = title
    event.startDate = start
    event.endDate = end
    event.isAllDay = (payload["all_day"] as? Bool) ?? false
    event.location = payload["location"] as? String
    event.notes = payload["notes"] as? String

    if let name = payload["calendar"] as? String, !name.isEmpty {
        event.calendar = store.calendars(for: .event).first { $0.title == name }
            ?? store.defaultCalendarForNewEvents
    } else {
        event.calendar = store.defaultCalendarForNewEvents
    }
    guard event.calendar != nil else { fail("no writable calendar is available") }

    if let alarm = payload["alarm_minutes_before"] as? Int {
        event.addAlarm(EKAlarm(relativeOffset: TimeInterval(-alarm * 60)))
    }

    do { try store.save(event, span: .thisEvent, commit: true) } catch {
        fail("could not save event: \(error.localizedDescription)")
    }
    emit([
        "id": event.eventIdentifier ?? "",
        "title": event.title ?? "",
        "start": iso.string(from: event.startDate),
        "end": iso.string(from: event.endDate),
        "calendar": event.calendar?.title ?? "",
    ])
    exit(0)
}

func listReminders() -> Never {
    requireReminders()
    let predicate = store.predicateForIncompleteReminders(
        withDueDateStarting: nil, ending: nil, calendars: nil)
    let sem = DispatchSemaphore(value: 0)
    var out: [[String: Any]] = []
    store.fetchReminders(matching: predicate) { reminders in
        out = (reminders ?? []).map { r -> [String: Any] in
            [
                "id": r.calendarItemIdentifier,
                "title": r.title ?? "(untitled)",
                "list": r.calendar?.title ?? "",
                "due": r.dueDateComponents?.date.map { iso.string(from: $0) } ?? "",
                "priority": r.priority,
                "notes": String((r.notes ?? "").prefix(300)),
            ]
        }
        sem.signal()
    }
    sem.wait()
    emit(out)
    exit(0)
}

func addReminder(_ raw: String) -> Never {
    requireReminders()
    let payload = decodePayload(raw)
    guard let title = payload["title"] as? String, !title.isEmpty else {
        fail("title is required")
    }
    let reminder = EKReminder(eventStore: store)
    reminder.title = title
    reminder.notes = payload["notes"] as? String
    if let listName = payload["list"] as? String, !listName.isEmpty {
        reminder.calendar = store.calendars(for: .reminder).first { $0.title == listName }
            ?? store.defaultCalendarForNewReminders()
    } else {
        reminder.calendar = store.defaultCalendarForNewReminders()
    }
    guard reminder.calendar != nil else { fail("no writable reminder list is available") }

    if let dueText = payload["due"] as? String, let due = iso.date(from: dueText) {
        reminder.dueDateComponents = Calendar.current.dateComponents(
            [.year, .month, .day, .hour, .minute], from: due)
        reminder.addAlarm(EKAlarm(absoluteDate: due))
    }
    do { try store.save(reminder, commit: true) } catch {
        fail("could not save reminder: \(error.localizedDescription)")
    }
    emit([
        "id": reminder.calendarItemIdentifier,
        "title": reminder.title ?? "",
        "list": reminder.calendar?.title ?? "",
        "due": reminder.dueDateComponents?.date.map { iso.string(from: $0) } ?? "",
    ])
    exit(0)
}

func completeReminder(_ id: String) -> Never {
    requireReminders()
    guard let item = store.calendarItem(withIdentifier: id) as? EKReminder else {
        fail("no reminder with id \(id)")
    }
    item.isCompleted = true
    do { try store.save(item, commit: true) } catch {
        fail("could not update reminder: \(error.localizedDescription)")
    }
    print("completed: \(item.title ?? id)")
    exit(0)
}

// MARK: - Contacts

func findContacts(_ query: String) -> Never {
    let store = CNContactStore()
    let (ok, _) = awaitAuth { done in
        store.requestAccess(for: .contacts) { granted, error in done(granted, error) }
    }
    guard ok else {
        fail("contacts access denied. Grant Contacts to your terminal in System Settings → Privacy & Security.", code: 77)
    }
    let keys: [CNKeyDescriptor] = [
        CNContactGivenNameKey, CNContactFamilyNameKey, CNContactOrganizationNameKey,
        CNContactPhoneNumbersKey, CNContactEmailAddressesKey,
    ].map { $0 as CNKeyDescriptor }

    var found: [[String: Any]] = []
    do {
        let matches = try store.unifiedContacts(
            matching: CNContact.predicateForContacts(matchingName: query), keysToFetch: keys)
        found = matches.map { c in
            [
                "name": "\(c.givenName) \(c.familyName)".trimmingCharacters(in: .whitespaces),
                "organization": c.organizationName,
                "phones": c.phoneNumbers.map { $0.value.stringValue },
                "emails": c.emailAddresses.map { $0.value as String },
            ]
        }
    } catch {
        fail("contact lookup failed: \(error.localizedDescription)")
    }
    emit(found)
    exit(0)
}

// MARK: - Menu bar

final class MenuBar: NSObject, NSApplicationDelegate {
    private var item: NSStatusItem!
    private let cli: String

    init(cli: String) {
        self.cli = cli
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        item = NSStatusBar.system.statusItem(withLength: NSStatusItem.variableLength)
        item.button?.title = "🤵"
        item.button?.toolTip = "Jeeves"

        let menu = NSMenu()
        menu.addItem(withTitle: "Ask Jeeves…", action: #selector(ask), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Listen (voice)", action: #selector(listenOnce), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Open Terminal Chat", action: #selector(openChat), keyEquivalent: "").target = self
        menu.addItem(withTitle: "Reveal Audit Log", action: #selector(revealLog), keyEquivalent: "").target = self
        menu.addItem(.separator())
        menu.addItem(withTitle: "Quit Jeeves", action: #selector(quit), keyEquivalent: "q").target = self
        item.menu = menu
    }

    private func jeeves(_ args: [String], capture: Bool) -> String {
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: cli)
        proc.arguments = args
        let pipe = Pipe()
        if capture { proc.standardOutput = pipe }
        do { try proc.run() } catch { return "could not launch jeeves: \(error)" }
        if !capture { return "" }
        proc.waitUntilExit()
        let data = pipe.fileHandleForReading.readDataToEndOfFile()
        return String(data: data, encoding: .utf8) ?? ""
    }

    @objc private func ask() {
        let alert = NSAlert()
        alert.messageText = "Ask Jeeves"
        alert.informativeText = "What would you like me to do?"
        let field = NSTextField(frame: NSRect(x: 0, y: 0, width: 320, height: 24))
        alert.accessoryView = field
        alert.addButton(withTitle: "Send")
        alert.addButton(withTitle: "Cancel")
        NSApp.activate(ignoringOtherApps: true)
        guard alert.runModal() == .alertFirstButtonReturn else { return }
        let prompt = field.stringValue.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !prompt.isEmpty else { return }

        DispatchQueue.global().async {
            let reply = self.jeeves(["ask", "--notify", prompt], capture: true)
            DispatchQueue.main.async {
                let done = NSAlert()
                done.messageText = "Jeeves"
                done.informativeText = reply.isEmpty ? "(no reply)" : reply
                done.addButton(withTitle: "OK")
                NSApp.activate(ignoringOtherApps: true)
                done.runModal()
            }
        }
    }

    @objc private func listenOnce() {
        DispatchQueue.global().async { _ = self.jeeves(["voice", "--once", "--notify"], capture: true) }
    }

    @objc private func openChat() {
        let script = "tell application \"Terminal\" to do script \"\(cli) chat\"\n"
            + "tell application \"Terminal\" to activate"
        let proc = Process()
        proc.executableURL = URL(fileURLWithPath: "/usr/bin/osascript")
        proc.arguments = ["-e", script]
        try? proc.run()
    }

    @objc private func revealLog() {
        let log = NSString(string: "~/.local/state/jeeves/audit.jsonl").expandingTildeInPath
        NSWorkspace.shared.selectFile(log, inFileViewerRootedAtPath: "")
    }

    @objc private func quit() { NSApp.terminate(nil) }
}

func runMenuBar(cli: String) -> Never {
    let app = NSApplication.shared
    let delegate = MenuBar(cli: cli)
    app.delegate = delegate
    app.setActivationPolicy(.accessory)  // menu-bar only, no Dock icon
    app.run()
    exit(0)
}

// MARK: - Entry point

let args = Array(CommandLine.arguments.dropFirst())
guard let command = args.first else {
    fail("usage: jeeves-native <ocr|listen|brightness|events|add-event|reminders|add-reminder|complete-reminder|contacts|bar> [args]")
}
let rest = Array(args.dropFirst())

switch command {
case "ocr":
    guard let path = rest.first else { fail("usage: ocr <image> [--fast]") }
    runOCR(path: path, fast: rest.contains("--fast"))

case "listen":
    var silence = 1.4
    var timeout = 30.0
    for (i, a) in rest.enumerated() {
        if a == "--silence", i + 1 < rest.count { silence = Double(rest[i + 1]) ?? silence }
        if a == "--timeout", i + 1 < rest.count { timeout = Double(rest[i + 1]) ?? timeout }
    }
    // Held in a local so ARC keeps the watchdog's `weak self` alive for the call.
    let listener = Listener(silence: silence, timeout: timeout)
    let text = listener.listen()
    print(text)
    exit(text.isEmpty ? 3 : 0)

case "brightness":
    guard let raw = rest.first, let level = Double(raw) else {
        fail("usage: brightness <0.0-1.0>")
    }
    setBrightness(level)

case "events":
    listEvents(days: Int(rest.first ?? "1") ?? 1)

case "add-event":
    guard let payload = rest.first else { fail("usage: add-event <json>") }
    addEvent(payload)

case "reminders":
    listReminders()

case "add-reminder":
    guard let payload = rest.first else { fail("usage: add-reminder <json>") }
    addReminder(payload)

case "complete-reminder":
    guard let id = rest.first else { fail("usage: complete-reminder <id>") }
    completeReminder(id)

case "contacts":
    guard let query = rest.first else { fail("usage: contacts <name>") }
    findContacts(query)

case "bar":
    runMenuBar(cli: rest.first ?? NSString(string: "~/Documents/jeeves/bin/jeeves").expandingTildeInPath)

default:
    fail("unknown command: \(command)")
}
