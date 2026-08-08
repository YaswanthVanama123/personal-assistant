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

// MARK: - TCC responsibility

// The privacy system attributes a request to the *responsible* process, not the
// one that made the call. A binary exec'd from a shell inherits the terminal as
// its responsible process, so TCC looks for the usage-description string in
// Terminal's Info.plist — never finds NSSpeechRecognitionUsageDescription there —
// and kills the process with a message claiming *our* plist lacks the key:
//
//   my pid: 37545, responsible pid: 1529
//   responsible process: /System/Applications/Utilities/Terminal.app/…/Terminal
//
// The fix is to re-exec ourselves having disclaimed the parent's responsibility.
// The new image is its own responsible process, so TCC reads Jeeves.app's
// Info.plist and prompts properly.

let DISCLAIM_ENV = "JEEVES_TCC_DISCLAIMED"

func responsiblePID(_ pid: pid_t = getpid()) -> pid_t {
    guard let handle = dlopen(nil, RTLD_LAZY),
          let symbol = dlsym(handle, "responsibility_get_pid_responsible_for_pid")
    else { return pid }
    typealias Fn = @convention(c) (pid_t) -> pid_t
    return unsafeBitCast(symbol, to: Fn.self)(pid)
}

func processPath(_ pid: pid_t) -> String {
    var buffer = [CChar](repeating: 0, count: 4096)
    guard proc_pidpath(pid, &buffer, 4096) > 0 else { return "unknown" }
    return String(cString: buffer)
}

private func withCStrings<R>(
    _ strings: [String],
    _ body: (UnsafeMutablePointer<UnsafeMutablePointer<CChar>?>) -> R
) -> R {
    var pointers: [UnsafeMutablePointer<CChar>?] = strings.map { strdup($0) }
    pointers.append(nil)
    defer { for pointer in pointers where pointer != nil { free(pointer) } }
    return pointers.withUnsafeMutableBufferPointer { body($0.baseAddress!) }
}

/// Re-run ourselves so that TCC treats this process, not the terminal, as
/// responsible for privacy requests.
///
/// A child is spawned with the parent's responsibility disclaimed. File
/// descriptors are inherited, so the child writes straight to our stdout and
/// stderr; we simply wait and exit with its status. (An earlier version used
/// POSIX_SPAWN_SETEXEC to replace the image in place, but the disclaim call and
/// `posix_spawnattr_setflags` fought over the same flags field, leaving both
/// parent and child running and printing everything twice.)
///
/// Returns only if the respawn could not be attempted, in which case the caller
/// carries on inline and the normal error path reports whatever happens.
func becomeOwnResponsibleProcess() {
    if ProcessInfo.processInfo.environment[DISCLAIM_ENV] != nil { return }
    if responsiblePID() == getpid() { return }  // already responsible, e.g. via `open`

    var attributes: posix_spawnattr_t?
    guard posix_spawnattr_init(&attributes) == 0 else { return }
    defer { posix_spawnattr_destroy(&attributes) }

    guard let handle = dlopen(nil, RTLD_LAZY),
          let symbol = dlsym(handle, "responsibility_spawnattrs_setdisclaim")
    else { return }
    typealias Disclaim = @convention(c) (UnsafeMutablePointer<posix_spawnattr_t?>, Int32) -> Int32
    guard unsafeBitCast(symbol, to: Disclaim.self)(&attributes, 1) == 0 else { return }

    // Must be the real path inside the bundle, or the child loses the bundle
    // identity that carries the usage strings.
    let executable = Bundle.main.executablePath ?? CommandLine.arguments[0]
    var environment = ProcessInfo.processInfo.environment
    environment[DISCLAIM_ENV] = "1"

    var child: pid_t = 0
    let spawned = withCStrings([executable] + CommandLine.arguments.dropFirst()) { argv in
        withCStrings(environment.map { "\($0.key)=\($0.value)" }) { envp in
            posix_spawn(&child, executable, &attributes, nil, argv, envp)
        }
    }
    guard spawned == 0, child > 0 else { return }  // fall back to running inline

    var status: Int32 = 0
    while waitpid(child, &status, 0) == -1 && errno == EINTR { continue }
    if status & 0x7f == 0 {
        exit((status >> 8) & 0xff)          // exited normally
    }
    exit(128 + (status & 0x7f))             // killed by a signal
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
    private var failure: Error?
    private var lastChange = Date()
    private let silenceLimit: TimeInterval
    private let hardLimit: TimeInterval
    private let done = DispatchSemaphore(value: 0)

    /// When reportTo is set, failures are written there as JSON rather than to
    /// stderr, because a LaunchServices-launched instance has nowhere to print.
    private let reportTo: String?

    init(silence: TimeInterval, timeout: TimeInterval, reportTo: String? = nil) {
        self.silenceLimit = silence
        self.hardLimit = timeout
        self.reportTo = reportTo
        super.init()
    }

    private func giveUp(_ message: String, code: Int32) -> Never {
        if let reportTo {
            writeListenResult(reportTo, ["error": message, "code": Int(code)])
            exit(0)
        }
        fail(message, code: code)
    }

    func listen() -> String {
        let (speechOK, _) = awaitAuth { done in
            SFSpeechRecognizer.requestAuthorization { status in
                done(status == .authorized, nil)
            }
        }
        guard speechOK, SFSpeechRecognizer.authorizationStatus() == .authorized else {
            giveUp("speech recognition was not authorised. Approve the prompt, or grant Speech Recognition to Jeeves under System Settings → Privacy & Security.", code: 77)
        }

        let (micOK, _) = awaitAuth { done in
            AVCaptureDevice.requestAccess(for: .audio) { ok in done(ok, nil) }
        }
        // Re-read the status: requestAccess reports the answer, but on a fresh
        // machine the prompt may have been dismissed or denied.
        guard micOK, AVCaptureDevice.authorizationStatus(for: .audio) == .authorized else {
            giveUp("microphone access was denied. Approve the prompt, or grant Microphone to Jeeves under System Settings → Privacy & Security.", code: 77)
        }

        guard let recognizer, recognizer.isAvailable else {
            giveUp("no speech recogniser is available for en-US", code: 78)
        }

        // There must actually be an input device. Without this check the engine
        // hands back a zero format below.
        guard AVCaptureDevice.default(for: .audio) != nil else {
            giveUp("no audio input device is available on this Mac", code: 79)
        }

        let request = SFSpeechAudioBufferRecognitionRequest()
        request.shouldReportPartialResults = true
        // Keep audio on the device; never send it to a server.
        request.requiresOnDeviceRecognition = recognizer.supportsOnDeviceRecognition
        self.request = request

        // AVAudioEngine aborts the whole process — SIGABRT, via an Objective-C
        // exception Swift cannot catch — if installTap gets a format that does
        // not match the hardware. So validate rather than try to recover.
        // inputFormat is the hardware's own format, which is what the tap wants.
        let input = engine.inputNode
        let format = input.inputFormat(forBus: 0)
        guard format.sampleRate > 0, format.channelCount > 0 else {
            giveUp(
                "the audio input is not usable (sample rate \(format.sampleRate), "
                + "\(format.channelCount) channel(s)). Check System Settings → Sound → "
                + "Input, and that no other app has exclusive use of the microphone.",
                code: 79
            )
        }

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
            if let error {
                // Keep the reason: a silent early exit here is impossible to debug.
                self.failure = error
                self.done.signal()
            }
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

        let heard = transcript.trimmingCharacters(in: .whitespacesAndNewlines)
        if heard.isEmpty, let failure {
            let ns = failure as NSError
            // 1110 is "no speech detected", which is a normal empty result.
            if ns.code == 1110 {
                return ""
            }
            // kLSRErrorDomain 201 means Dictation is switched off. It is by far
            // the most common reason speech fails on an otherwise healthy Mac, and
            // the generic message ("Siri and Dictation are disabled") does not say
            // where to fix it.
            if ns.domain == "kLSRErrorDomain", ns.code == 201 {
                giveUp(
                    "Dictation is turned off, so on-device speech recognition cannot "
                    + "run. Turn it on: System Settings → Keyboard → Dictation. "
                    + "(Siri itself is not required.) Then try again.",
                    code: 80
                )
            }
            giveUp(
                "speech recognition failed: \(failure.localizedDescription) "
                + "[\(ns.domain) \(ns.code)]. Run `jeeves-native audio-check` for "
                + "the audio and permission state.",
                code: 78
            )
        }
        return heard
    }
}

/// Report the audio and speech setup without recording anything.
/// Safe to run when `listen` misbehaves — it touches no engine and no tap.
func audioCheck() -> Never {
    func describe(_ status: Int) -> String {
        ["not determined", "restricted", "denied", "authorized"][safe: status] ?? "unknown"
    }
    var report: [String: Any] = [
        "microphone_permission": describe(Int(AVCaptureDevice.authorizationStatus(for: .audio).rawValue)),
        "speech_permission": describe(Int(SFSpeechRecognizer.authorizationStatus().rawValue)),
        "usage_strings_visible": (Bundle.main.infoDictionary?["NSMicrophoneUsageDescription"] != nil)
            && (Bundle.main.infoDictionary?["NSSpeechRecognitionUsageDescription"] != nil),
        "bundle_identifier": Bundle.main.bundleIdentifier ?? "none",
        // If this is not the .app, TCC will not read the usage strings and
        // Speech Recognition will kill the process instead of prompting.
        "bundle_path": Bundle.main.bundlePath,
        // If this is not our own pid, TCC reads the *other* process's
        // Info.plist and will kill us for a "missing" usage string.
        "responsible_pid_is_self": responsiblePID() == getpid(),
        "responsible_process": processPath(responsiblePID()),
        "inside_app_bundle": Bundle.main.bundlePath.hasSuffix(".app"),
    ]

    if let device = AVCaptureDevice.default(for: .audio) {
        report["input_device"] = device.localizedName
    } else {
        report["input_device"] = "NONE — this is why listen fails"
    }

    if let recognizer = SFSpeechRecognizer(locale: Locale(identifier: "en-US")) {
        report["recognizer_available"] = recognizer.isAvailable
        report["on_device_supported"] = recognizer.supportsOnDeviceRecognition
    } else {
        report["recognizer_available"] = false
    }

    // Dictation must be enabled for on-device recognition; when it is off the
    // recogniser reports itself available and then fails on first use.
    let hiToolbox = UserDefaults(suiteName: "com.apple.HIToolbox")
    report["dictation_enabled"] = hiToolbox?.object(forKey: "AppleDictationAutoEnable") as? Int == 1
    report["dictation_hint"] = "if false: System Settings → Keyboard → Dictation"

    // Reading the format is safe; installing a tap with a bad one is not.
    // The engine must be held in a local — reading inputNode off a temporary
    // crashes once ARC releases the engine underneath it.
    let engine = AVAudioEngine()
    let format = engine.inputNode.inputFormat(forBus: 0)
    report["hardware_sample_rate"] = format.sampleRate
    report["hardware_channels"] = Int(format.channelCount)
    report["format_usable"] = format.sampleRate > 0 && format.channelCount > 0

    emit(report)
    exit(0)
}

extension Array {
    subscript(safe index: Int) -> Element? {
        indices.contains(index) ? self[index] : nil
    }
}

/// Host for `listen`.
///
/// Two things are required before Speech Recognition can be requested, and a
/// terminal-launched process has neither:
///
///  1. **Its own TCC responsibility.** macOS attributes a privacy request to the
///     *responsible* process. A binary exec'd from a shell inherits the terminal,
///     so TCC looks for NSSpeechRecognitionUsageDescription in Terminal's
///     Info.plist, does not find it, and kills us — while reporting that *our*
///     plist lacks the key. `responsibility_spawnattrs_setdisclaim` returns
///     success but does not actually move responsibility here, so the reliable
///     route is to relaunch through LaunchServices: an app opened by launchd is
///     its own responsible process.
///
///  2. **A real NSApplication.** A prompt can only be presented to a process the
///     window server knows about; `RunLoop.main.run()` is not enough.
///
/// So `listen` from a terminal relaunches Jeeves.app via `open`, and that
/// instance writes its result to a JSON file the CLI polls for.
final class ListenApp: NSObject, NSApplicationDelegate {
    private let silence: TimeInterval
    private let timeout: TimeInterval
    private let outputPath: String?

    init(silence: TimeInterval, timeout: TimeInterval, outputPath: String?) {
        self.silence = silence
        self.timeout = timeout
        self.outputPath = outputPath
        super.init()
    }

    func applicationDidFinishLaunching(_ notification: Notification) {
        DispatchQueue.global(qos: .userInitiated).async { [silence, timeout, outputPath] in
            guard let outputPath else {
                let listener = Listener(silence: silence, timeout: timeout)
                let text = listener.listen()
                print(text)
                exit(text.isEmpty ? 3 : 0)
            }
            // Detached instance: report through the file, never stdout.
            let listener = Listener(silence: silence, timeout: timeout, reportTo: outputPath)
            let text = listener.listen()
            writeListenResult(outputPath, ["text": text, "code": text.isEmpty ? 3 : 0])
            exit(0)
        }
    }
}

func writeListenResult(_ path: String, _ payload: [String: Any]) {
    var body = payload
    body["responsible_pid_is_self"] = responsiblePID() == getpid()
    body["responsible_process"] = processPath(responsiblePID())
    guard let data = try? JSONSerialization.data(withJSONObject: body) else { return }
    // Write then rename, so the poller never sees a half-written file.
    let temporary = path + ".partial"
    try? data.write(to: URL(fileURLWithPath: temporary))
    try? FileManager.default.moveItem(
        at: URL(fileURLWithPath: temporary), to: URL(fileURLWithPath: path))
}

/// Run the listener inside a proper application process.
func runListenAsApp(silence: TimeInterval, timeout: TimeInterval, outputPath: String?) -> Never {
    let app = NSApplication.shared
    let delegate = ListenApp(silence: silence, timeout: timeout, outputPath: outputPath)
    app.delegate = delegate
    app.setActivationPolicy(.accessory)
    app.run()
    exit(0)
}

/// Where the CLI leaves a request for the app instance to pick up.
///
/// LaunchServices does not forward `open --args` to the process in a way we can
/// rely on — argv arrives containing only the executable path — so the request
/// travels through a file instead. That also keeps the app instance completely
/// independent of how it was started.
let listenRequestPath: String = {
    let dir = NSHomeDirectory() + "/Library/Caches/jeeves"
    try? FileManager.default.createDirectory(
        atPath: dir, withIntermediateDirectories: true)
    return dir + "/listen-request.json"
}()

/// Relaunch through LaunchServices and wait for the transcript.
///
/// An app opened by launchd is its own TCC-responsible process, which is the
/// whole point: a terminal-launched binary inherits the terminal's identity and
/// gets killed for a usage string it does not own.
func listenViaLaunchServices(silence: TimeInterval, timeout: TimeInterval) -> Never {
    let bundle = Bundle.main.bundlePath
    guard bundle.hasSuffix(".app") else {
        fail(
            "the helper is not running from Jeeves.app, so macOS cannot attribute "
            + "the microphone prompt to it. Re-run scripts/build_native.sh.",
            code: 79
        )
    }

    let output = NSTemporaryDirectory()
        + "jeeves-listen-\(getpid())-\(Int(Date().timeIntervalSince1970)).json"
    try? FileManager.default.removeItem(atPath: output)

    let request: [String: Any] = ["out": output, "silence": silence, "timeout": timeout]
    guard let data = try? JSONSerialization.data(withJSONObject: request) else {
        fail("could not build the listen request", code: 79)
    }
    try? data.write(to: URL(fileURLWithPath: listenRequestPath))

    let open = Process()
    open.executableURL = URL(fileURLWithPath: "/usr/bin/open")
    open.arguments = ["-n", "-a", bundle]
    do { try open.run() } catch {
        fail("could not launch \(bundle): \(error.localizedDescription)", code: 79)
    }
    open.waitUntilExit()
    if open.terminationStatus != 0 {
        fail("`open` refused to launch \(bundle) (status \(open.terminationStatus))", code: 79)
    }

    // Generous: the app instance may be showing a permission dialog.
    let deadline = Date().addingTimeInterval(timeout + 90)
    while Date() < deadline {
        if let data = FileManager.default.contents(atPath: output),
           let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any] {
            try? FileManager.default.removeItem(atPath: output)
            if let error = parsed["error"] as? String {
                fail(error, code: Int32(parsed["code"] as? Int ?? 78))
            }
            let text = parsed["text"] as? String ?? ""
            print(text)
            exit(Int32(parsed["code"] as? Int ?? (text.isEmpty ? 3 : 0)))
        }
        Thread.sleep(forTimeInterval: 0.15)
    }
    try? FileManager.default.removeItem(atPath: output)
    fail(
        "the speech helper did not report back within \(Int(timeout) + 90)s. If a "
        + "microphone or speech permission dialog appeared, approve it and run this "
        + "again — the grant is remembered.",
        code: 78
    )
}

/// Read the request the CLI left for us. Nil when launched without one.
func pendingListenRequest() -> (out: String, silence: TimeInterval, timeout: TimeInterval)? {
    guard let data = FileManager.default.contents(atPath: listenRequestPath),
          let parsed = try? JSONSerialization.jsonObject(with: data) as? [String: Any],
          let out = parsed["out"] as? String
    else { return nil }
    try? FileManager.default.removeItem(atPath: listenRequestPath)  // one-shot
    return (out, parsed["silence"] as? Double ?? 1.4, parsed["timeout"] as? Double ?? 30)
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

// Launched by LaunchServices with no arguments: pick up the request the CLI left.
if args.isEmpty, let pending = pendingListenRequest() {
    runListenAsApp(silence: pending.silence, timeout: pending.timeout, outputPath: pending.out)
}

guard let command = args.first else {
    fail("usage: jeeves-native <ocr|listen|audio-check|brightness|events|add-event|reminders|add-reminder|complete-reminder|contacts|ui-dump|ui-type|wa-chats|wa-unread|wa-read|wa-send|bar> [args]")
}
let rest = Array(args.dropFirst())

/// Run one command. Every branch ends in exit(), so this never returns.
func dispatch(_ command: String, _ rest: [String]) -> Never {
    switch command {
    case "ocr":
        guard let path = rest.first else { fail("usage: ocr <image> [--fast]") }
        runOCR(path: path, fast: rest.contains("--fast"))

    case "listen":
        // Handled before dispatch, because it needs its own NSApplication.
        fail("internal: listen should have been handled earlier")

    case "audio-check":
        // --disclaim proves the responsibility fix works: run it with and
        // without the flag and compare responsible_pid_is_self.
        if rest.contains("--disclaim") { becomeOwnResponsibleProcess() }
        audioCheck()

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

    // Accessibility: reading and driving apps that have no AppleScript API.
    case "ui-dump":
        guard let app = rest.first else { fail("usage: ui-dump <app> [--max N] [--roles]") }
        var limit = 400
        for (i, a) in rest.enumerated() where a == "--max" && i + 1 < rest.count {
            limit = Int(rest[i + 1]) ?? limit
        }
        uiDump(app: app, limit: limit, showRoles: rest.contains("--roles"))

    case "ui-type":
        guard let text = rest.first else { fail("usage: ui-type <text>") }
        uiType(text)

    case "wa-chats":
        waChats()

    case "wa-unread":
        waUnread()

    case "wa-read":
        var limit = 40
        for (i, a) in rest.enumerated() where a == "--max" && i + 1 < rest.count {
            limit = Int(rest[i + 1]) ?? limit
        }
        waRead(chat: rest.first.map { $0.hasPrefix("--") ? "" : $0 } ?? "", limit: limit)

    case "wa-send":
        guard rest.count >= 2 else { fail("usage: wa-send <chat> <text>") }
        waSend(chat: rest[0], text: rest[1])

    default:
        fail("unknown command: \(command)")
    }
}

// Speech Recognition is TCC-protected, so this command has to be its own
// responsible process and needs a real NSApplication. Both are set up here,
// before the generic background dispatch below.
if command == "listen" {
    var silence = 1.4
    var timeout = 30.0
    var output: String?
    for (i, a) in rest.enumerated() {
        if a == "--silence", i + 1 < rest.count { silence = Double(rest[i + 1]) ?? silence }
        if a == "--timeout", i + 1 < rest.count { timeout = Double(rest[i + 1]) ?? timeout }
        if a == "--out", i + 1 < rest.count { output = rest[i + 1] }
    }

    if output != nil {
        // Launched by LaunchServices: we are the application instance.
        runListenAsApp(silence: silence, timeout: timeout, outputPath: output)
    }
    if responsiblePID() == getpid() {
        // Already our own responsible process, so no relaunch is needed.
        runListenAsApp(silence: silence, timeout: timeout, outputPath: nil)
    }
    listenViaLaunchServices(silence: silence, timeout: timeout)
}

if command == "bar" {
    // The menu bar owns the main thread and runs its own NSApplication loop.
    runMenuBar(cli: rest.first ?? NSString(string: "~/Documents/jeeves/bin/jeeves").expandingTildeInPath)
}

// Every other command runs on a background thread while the main thread services
// the run loop.
//
// This matters more than it looks. Several commands request a privacy permission,
// and macOS can only present that prompt from a live main run loop. An earlier
// version blocked the main thread on a semaphore waiting for the answer, so the
// prompt could never appear: on a Mac where permission was already granted it
// worked, and on a fresh one TCC killed the process with SIGABRT.
DispatchQueue.global(qos: .userInitiated).async {
    dispatch(command, rest)
}
RunLoop.main.run()
