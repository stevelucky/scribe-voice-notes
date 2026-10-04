import Foundation
import SwiftUI
import UserNotifications

/// Thread-safe holder for a running subprocess, so the processing pipeline's
/// Process (created on a background thread) can be terminated from the main actor.
final class ProcessBox: @unchecked Sendable {
    private let lock = NSLock()
    private var process: Process?
    func set(_ p: Process?) { lock.lock(); process = p; lock.unlock() }
    func terminate() { lock.lock(); process?.terminate(); process = nil; lock.unlock() }
}

/// Thread-safe line accumulator for streamed pipe output. Carries a partial last
/// line across reads so a stage-marker JSON object split across a pipe-read
/// boundary still parses, and collects the full log for error reporting.
final class LineAccumulator: @unchecked Sendable {
    private let lock = NSLock()
    private var carry = ""
    private var allLines: [String] = []

    /// Feed a chunk; returns the newly completed (trimmed, non-empty) lines.
    func feed(_ chunk: String) -> [String] {
        lock.lock(); defer { lock.unlock() }
        carry += chunk
        let parts = carry.components(separatedBy: "\n")
        carry = parts.last ?? ""  // last element is the (possibly partial) remainder
        var completed: [String] = []
        for part in parts.dropLast() {
            let t = part.trimmingCharacters(in: .whitespacesAndNewlines)
            if !t.isEmpty { completed.append(t); allLines.append(t) }
        }
        return completed
    }

    /// Flush any buffered remainder as a final line.
    func flush() {
        lock.lock(); defer { lock.unlock() }
        let t = carry.trimmingCharacters(in: .whitespacesAndNewlines)
        carry = ""
        if !t.isEmpty { allLines.append(t) }
    }

    var joined: String {
        lock.lock(); defer { lock.unlock() }
        return allLines.joined(separator: "\n")
    }
}

/// The menu-bar icon image, kept in its own observable so the per-second icon
/// refresh updates ONLY the menu-bar label — never the dropdown menu. Mutating a
/// @Published on RecordingManager while the dropdown is open re-bridges the whole
/// NSMenu and scrambles AppKit's hover highlighting (items "hop").
@MainActor final class StatusIconModel: ObservableObject {
    @Published var image: NSImage
    init(image: NSImage) { self.image = image }
}

/// What the current recording is for. A meeting is processed normally (to-dos →
/// Action Center); an idea session is processed with `--type idea` so it's
/// captured idea-shaped and its rare to-dos are flagged for review, not auto-filed.
enum CaptureMode {
    case meeting, idea
}

@MainActor
class RecordingManager: ObservableObject {
    static let shared = RecordingManager()

    @Published var isRecording = false
    // Only changes on start/stop (a real state change), so it's safe on the
    // dropdown's @Published graph alongside isRecording.
    @Published var captureMode: CaptureMode = .meeting
    // Paused state (silence auto-pause or a manual pause). Changes infrequently,
    // so it's fine on the dropdown's @Published graph.
    @Published var isPaused = false
    private var pauseReason = ""    // "silent" | "manual" | "recording"
    var pauseStatusText: String { pauseReason == "silent" ? "Paused (silent)" : "Paused" }
    @Published var lastNote: String?
    @Published var isProcessing = false
    @Published var processingStage: String = ""  // raw stage key from Python
    @Published var processingDetail: String = ""
    @Published var completedStages: Set<String> = []
    @Published var transcribeProgress: Double = -1  // 0…1 during transcription; -1 = indeterminate
    @Published var stageElapsed: Int = 0            // seconds spent in the current processing stage
    private var stageStartedAt: Date?
    private var processingTimer: Timer?
    var stageElapsedText: String {
        String(format: "%d:%02d", stageElapsed / 60, stageElapsed % 60)
    }
    @Published var projectDir: String
    @Published var llmProvider: String = "LLM"
    @Published var lastError: String?
    @Published var lastNoteFile: String?       // basename of the most recent note (for corrections)
    @Published var isCorrecting = false

    // Per-second timer state lives OFF the @Published graph the dropdown observes.
    let statusIcon = StatusIconModel(image: ScribeApp.idleMicIcon)
    private var elapsedSeconds: Int = 0

    // Live-meter state, kept OFF the dropdown's @Published graph (same split as
    // elapsedSeconds/statusIcon) so ~12 Hz updates only re-render the menu-bar
    // icon, never the dropdown (which would scramble hover highlighting).
    private var currentBands: [Float] = Array(repeating: 0, count: 5)
    private var lastMeterRender = Date.distantPast
    private var silentSince: Date?
    private static let silenceWarnAfter: TimeInterval = 3.0

    private var recordProcess: Process?
    private let dashboardPort = 8765
    private var timer: Timer?
    private var watchTimer: Timer?
    private var sleepAssertion: NSObjectProtocol?
    private let pipelineProcessBox = ProcessBox()
    // Snapshot of recordings/*.wav at the moment recording started, so cancel
    // only deletes a file this session actually created.
    private var recordingsAtStart: Set<String> = []

    private static let projectDirKey = "projectDir"
    nonisolated static let condaEnvKey = "condaEnv"
    nonisolated static let defaultCondaEnv = "scribe"

    private init() {
        if let saved = UserDefaults.standard.string(forKey: Self.projectDirKey),
           FileManager.default.fileExists(atPath: saved + "/config.yaml") {
            projectDir = saved
        } else {
            let resolved = Self.resolveProjectDir()
            projectDir = resolved
            UserDefaults.standard.set(resolved, forKey: Self.projectDirKey)
        }
        requestNotificationPermission()
        llmProvider = Self.readLLMProvider(from: projectDir)
    }

    func saveProjectDir(_ path: String) {
        UserDefaults.standard.set(path, forKey: Self.projectDirKey)
        projectDir = path
        llmProvider = Self.readLLMProvider(from: path)
    }

    private static func readLLMProvider(from dir: String) -> String {
        let configPath = "\(dir)/config.yaml"
        guard let contents = try? String(contentsOfFile: configPath, encoding: .utf8) else { return "LLM" }
        for line in contents.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            if trimmed.hasPrefix("provider:") {
                let value = trimmed.replacingOccurrences(of: "provider:", with: "").trimmingCharacters(in: .whitespaces)
                switch value {
                case "anthropic": return "Claude"
                case "gemini":    return "Gemini"
                case "openai":    return "OpenAI"
                case "ollama":    return "Ollama"
                default:          return value
                }
            }
        }
        return "LLM"
    }

    private static func resolveProjectDir() -> String {
        let appDir = Bundle.main.bundlePath
        let appParent = URL(fileURLWithPath: appDir).deletingLastPathComponent().path

        var dir = URL(fileURLWithPath: appParent)
        for _ in 0..<5 {
            let candidate = dir.appendingPathComponent("config.yaml").path
            if FileManager.default.fileExists(atPath: candidate) {
                return dir.path
            }
            dir = dir.deletingLastPathComponent()
        }

        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let fallbacks = [
            "\(home)/scribe-notes",
            "\(home)/App_Development/scribe-notes",
            "\(home)/Documents/scribe-notes",
            "\(home)/codes/scribe-notes",
        ]
        for path in fallbacks {
            if FileManager.default.fileExists(atPath: path + "/config.yaml") {
                return path
            }
        }

        return appParent
    }

    func toggle() async {
        if isRecording {
            await stopRecording()
        } else {
            await startRecording()
        }
    }

    func startRecording(mode: CaptureMode = .meeting) async {
        guard !isRecording else { return }
        guard !isProcessing, !isCorrecting else {
            sendNotification(title: "Scribe", body: "Still working on the last recording — try again in a moment.")
            return
        }
        captureMode = mode

        // Snapshot existing recordings so a later cancel deletes only what we create.
        recordingsAtStart = currentRecordingFilenames()

        // Reset meter state before the icon's first paint this session.
        currentBands = Array(repeating: 0, count: 5)
        silentSince = nil
        lastMeterRender = .distantPast

        // Launch the recorder FIRST; only enter the recording state if it starts,
        // so a misconfigured env can't leave the app stuck in a phantom recording.
        guard let proc = launchRecorder() else {
            recordingsAtStart = []
            lastError = "Failed to launch the recorder. Check the project folder and conda env in Settings."
            sendNotification(title: "Scribe", body: "Couldn't start recording — check Settings (project folder / conda env).")
            return
        }
        recordProcess = proc

        isRecording = true
        isPaused = false
        pauseReason = ""
        elapsedSeconds = 0

        // Per-second updates go to statusIcon only (not the dropdown's @Published graph).
        // The live meter (driven by the recorder's stdout) repaints the same icon
        // between ticks; this 1 Hz timer keeps the clock advancing during silence.
        renderRecordingIcon()
        timer = Self.makeCommonModeTimer(interval: 1) { [weak self] in
            guard let self else { return }
            self.elapsedSeconds += 1
            self.renderRecordingIcon()
        }

        sleepAssertion = ProcessInfo.processInfo.beginActivity(
            options: [.idleSystemSleepDisabled, .suddenTerminationDisabled],
            reason: "Scribe is recording audio"
        )

        let what = mode == .idea ? "Idea session" : "Recording"
        sendNotification(title: "Scribe", body: "\(what) started. Press Cmd+Shift+R to stop.")
    }

    // MARK: - Live meter

    /// Launch `src.cli record --meter`, streaming the recorder's stdout (one
    /// `{"lvl":[…]}` JSON object per line) to drive the menu-bar equalizer. Uses
    /// `exec` (via shellCommand) so interrupt()/terminate() reach Python directly.
    private func launchRecorder() -> Process? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-c", shellCommand(args: ["record", "--duration", "0", "--meter"])]
        process.currentDirectoryURL = URL(fileURLWithPath: projectDir)

        var env = ProcessInfo.processInfo.environment
        let extraPaths = ["/opt/homebrew/bin", "/usr/local/bin"]
        env["PATH"] = extraPaths.joined(separator: ":") + ":" + (env["PATH"] ?? "/usr/bin:/bin")
        if let dotenvVars = loadDotEnv() { env.merge(dotenvVars) { _, new in new } }
        process.environment = env

        let outPipe = Pipe()
        process.standardOutput = outPipe
        // Discard the recorder's stderr so it can't fill a pipe buffer and stall
        // the child over a long meeting (we don't surface recorder logs here).
        process.standardError = FileHandle.nullDevice

        let meterAcc = LineAccumulator()
        outPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            if data.isEmpty { handle.readabilityHandler = nil; return }  // EOF: self-cleaning
            guard let chunk = String(data: data, encoding: .utf8) else { return }
            for line in meterAcc.feed(chunk) { self?.handleMeterLine(line) }
        }

        do {
            try process.run()
        } catch {
            lastError = "Failed to launch: \(error.localizedDescription)"
            return nil
        }
        return process
    }

    /// Parse one `{"lvl":[…]}` line off the recorder's stdout (background thread),
    /// then hand the band levels to the main actor.
    nonisolated private func handleMeterLine(_ line: String) {
        guard line.hasPrefix("{"),
              let data = line.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: data) as? [String: Any] else { return }
        if let arr = obj["lvl"] as? [Any] {
            let bands = arr.compactMap { ($0 as? NSNumber)?.floatValue }
            if !bands.isEmpty { Task { @MainActor [weak self] in self?.updateMeter(bands) } }
            return
        }
        if let paused = obj["paused"] as? Bool {
            let reason = obj["reason"] as? String ?? ""
            Task { @MainActor [weak self] in self?.updatePauseState(paused, reason: reason) }
            return
        }
        if let capped = obj["capped"] as? Bool, capped {
            let limit = (obj["limit_min"] as? NSNumber)?.intValue ?? 0
            Task { @MainActor [weak self] in self?.handleCapReached(limitMin: limit) }
        }
    }

    /// The recorder self-stopped at its max-duration cap. Notify the user and
    /// process what was captured — don't interrupt (it's already finishing), just
    /// wait for it to exit, then run the pipeline, so nothing is silently lost.
    private func handleCapReached(limitMin: Int) {
        guard isRecording else { return }
        let limitText = (limitMin > 0 && limitMin % 60 == 0) ? "\(limitMin / 60)-hour"
                      : (limitMin > 0 ? "\(limitMin)-minute" : "maximum")
        sendNotification(title: "Scribe",
                         body: "Recording hit the \(limitText) limit and stopped. Saving what was recorded — start a new recording to keep going.")
        let proc = recordProcess
        recordProcess = nil
        timer?.invalidate(); timer = nil
        isRecording = false
        isPaused = false
        pauseReason = ""
        endSleepAssertion()
        recordingsAtStart = []
        Task { @MainActor in
            if let proc, proc.isRunning { await Self.waitForExit(proc, timeout: 20) }
            await processLatest()
        }
    }

    /// Apply a pause-state transition from the recorder. Notifies once when the
    /// recording auto-pauses on silence so the user isn't surprised by the gap.
    private func updatePauseState(_ paused: Bool, reason: String) {
        guard isRecording else { return }
        let wasPaused = isPaused
        isPaused = paused
        pauseReason = reason
        renderRecordingIcon()
        if paused && !wasPaused && reason == "silent" {
            sendNotification(title: "Scribe",
                             body: "Recording paused — the room went quiet. It resumes as soon as you speak.")
        }
    }

    /// Toggle a deliberate pause by signalling the recorder (SIGUSR1). The recorder
    /// echoes the new state back on stdout, which updates the UI.
    func togglePause() {
        guard isRecording, let proc = recordProcess, proc.isRunning else { return }
        kill(proc.processIdentifier, SIGUSR1)
    }

    /// Apply new band levels: track silence and repaint the icon (throttled).
    private func updateMeter(_ bands: [Float]) {
        guard isRecording else { return }
        currentBands = bands
        let active = bands.contains { $0 > 0.03 }
        if active {
            silentSince = nil
        } else if silentSince == nil {
            silentSince = Date()
        }
        let now = Date()
        if now.timeIntervalSince(lastMeterRender) >= 0.08 {  // ~12 Hz
            lastMeterRender = now
            renderRecordingIcon()
        }
    }

    /// True once the input has been quiet long enough to flag a likely dead/muted
    /// mic (drives the icon's warning tint — visual only, no notifications).
    private func isSilent() -> Bool {
        guard let since = silentSince else { return false }
        return Date().timeIntervalSince(since) >= Self.silenceWarnAfter
    }

    private func renderRecordingIcon() {
        if isPaused {
            statusIcon.image = Self.makePausedPillIcon(time: formattedTime)
        } else {
            statusIcon.image = Self.makeRecordingPillIcon(
                time: formattedTime, bands: currentBands, warning: isSilent(),
                idea: captureMode == .idea)
        }
    }

    /// Filenames of *.wav currently in the recordings folder.
    private func currentRecordingFilenames() -> Set<String> {
        let path = "\(projectDir)/recordings"
        let items = (try? FileManager.default.contentsOfDirectory(atPath: path)) ?? []
        return Set(items.filter { $0.hasSuffix(".wav") })
    }

    /// A repeating timer scheduled in `.common` run-loop mode so it keeps firing
    /// while the menu bar popover is open (which runs in event-tracking mode).
    private static func makeCommonModeTimer(interval: TimeInterval, _ tick: @escaping @MainActor () -> Void) -> Timer {
        let t = Timer(timeInterval: interval, repeats: true) { _ in
            Task { @MainActor in tick() }
        }
        RunLoop.main.add(t, forMode: .common)
        return t
    }

    private func endSleepAssertion() {
        if let token = sleepAssertion {
            ProcessInfo.processInfo.endActivity(token)
            sleepAssertion = nil
        }
    }

    func stopRecording() async {
        guard isRecording else { return }

        // Update UI immediately so buttons respond at once
        let proc = recordProcess
        recordProcess = nil
        timer?.invalidate()
        timer = nil
        isRecording = false
        isPaused = false
        pauseReason = ""
        endSleepAssertion()

        // Signal Python and wait for it to flush the WAV to disk before processing
        if let proc, proc.isRunning {
            proc.interrupt()
            await Self.waitForExit(proc, timeout: 15)
        }

        recordingsAtStart = []
        sendNotification(title: "Scribe", body: "Recording stopped. Processing...")
        await processLatest()
    }

    /// Wait for a process to exit, but force-terminate it after `timeout` seconds
    /// so a hung child can't block the UI flow forever.
    nonisolated static func waitForExit(_ proc: Process, timeout: TimeInterval) async {
        let waitTask = Task.detached { proc.waitUntilExit() }
        let killTask = Task.detached {
            try? await Task.sleep(nanoseconds: UInt64(timeout * 1_000_000_000))
            if proc.isRunning { proc.terminate() }
        }
        await waitTask.value
        killTask.cancel()
    }

    func cancelRecording() async {
        guard isRecording else { return }

        // Update UI immediately
        let proc = recordProcess
        recordProcess = nil
        timer?.invalidate()
        timer = nil
        isRecording = false
        isPaused = false
        pauseReason = ""
        endSleepAssertion()

        if let proc, proc.isRunning {
            proc.interrupt()
            await Self.waitForExit(proc, timeout: 15)
        }

        // Delete only WAVs that appeared during this session — never a prior
        // recording (which is what happens if this session produced no file).
        let recordingsPath = "\(projectDir)/recordings"
        let created = currentRecordingFilenames().subtracting(recordingsAtStart)
        for name in created {
            try? FileManager.default.removeItem(atPath: "\(recordingsPath)/\(name)")
        }
        recordingsAtStart = []
        statusIcon.image = ScribeApp.idleMicIcon

        let msg = created.isEmpty ? "Recording cancelled." : "Recording cancelled and deleted."
        sendNotification(title: "Scribe", body: msg)
    }

    func processLatest() async {
        let capturedProjectDir = projectDir
        let capturedDiarize = UserDefaults.standard.bool(forKey: "diarizeEnabled")
        let python = Self.pythonPath()
        let diarizeFlag = capturedDiarize ? "--diarize" : "--no-diarize"
        // An idea session is processed idea-shaped; reset to meeting for next time.
        let typeFlag = captureMode == .idea ? " --type idea" : ""
        captureMode = .meeting
        let cmd = "cd \"\(capturedProjectDir)\" && exec \"\(python)\" -m src.cli process-latest \(diarizeFlag)\(typeFlag)"
        await runPipeline(cmd: cmd, projectDir: capturedProjectDir)
    }

    func pickAndProcessAudioFile() {
        // Delay lets the MenuBarExtra popup fully dismiss before the panel appears,
        // which prevents the menu bar icon becoming unresponsive afterwards.
        DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) { [weak self] in
            guard let self else { return }
            let panel = NSOpenPanel()
            panel.allowedContentTypes = [.audio, .wav, .mp3, .mpeg4Audio]
            panel.canChooseFiles = true
            panel.canChooseDirectories = false
            panel.allowsMultipleSelection = false
            panel.prompt = "Process"
            panel.message = "Select a recording to transcribe and process"
            panel.directoryURL = URL(fileURLWithPath: "\(self.projectDir)/recordings")
            panel.level = .modalPanel
            panel.begin { [weak self] response in
                guard response == .OK, let url = panel.url, let self else { return }
                Task { @MainActor in
                    await self.processAudioFile(at: url.path)
                }
            }
        }
    }

    @discardableResult
    func processAudioFile(at path: String) async -> Bool {
        let capturedProjectDir = projectDir
        let capturedDiarize = UserDefaults.standard.bool(forKey: "diarizeEnabled")
        let python = Self.pythonPath()
        let diarizeFlag = capturedDiarize ? "--diarize" : "--no-diarize"
        let escapedPath = path.replacingOccurrences(of: "\"", with: "\\\"")
        // Imported files carry no capture mode, so let the pipeline infer meeting
        // vs idea from the audio's content (--type auto).
        let cmd = "cd \"\(capturedProjectDir)\" && exec \"\(python)\" -m src.cli process \"\(escapedPath)\" \(diarizeFlag) --type auto"
        return await runPipeline(cmd: cmd, projectDir: capturedProjectDir)
    }

    // MARK: - Corrections

    /// Apply a plain-English correction to a note and regenerate it via
    /// `src.cli correct`. Reuses the streaming subprocess runner; shows a distinct
    /// "correcting" state (not the 3-step processing UI).
    func applyCorrection(file: String, message: String) async {
        guard !isRecording, !isProcessing, !isCorrecting else {
            sendNotification(title: "Scribe", body: "Busy — try again in a moment.")
            return
        }
        let trimmed = message.trimmingCharacters(in: .whitespacesAndNewlines)
        guard !trimmed.isEmpty else { return }

        isCorrecting = true
        lastError = nil
        statusIcon.image = ScribeApp.processingPillIcon

        let capturedProjectDir = projectDir
        let python = Self.pythonPath()
        let cmd = "cd \"\(capturedProjectDir)\" && exec \"\(python)\" -m src.cli correct "
                + "-f \"\(Self.shellEscape(file))\" -m \"\(Self.shellEscape(message))\""

        let (output, exitCode, stderrLog) = await Task.detached { [self] in
            self.runSubprocessWithProgress(cmd: cmd, projectDir: capturedProjectDir)
        }.value

        isCorrecting = false
        statusIcon.image = ScribeApp.idleMicIcon

        if exitCode == 0, let output, !output.isEmpty {
            lastError = nil
            lastNote = output
            sendNotification(title: "Scribe: Note Corrected", body: output)
        } else {
            let errorLine = stderrLog
                .components(separatedBy: .newlines)
                .filter { !$0.hasPrefix("{") && !$0.isEmpty }
                .last ?? "Unknown error (exit \(exitCode))"
            lastError = errorLine
            sendNotification(title: "Scribe: Correction Failed", body: errorLine)
        }
    }

    /// Escape a string for inclusion inside a zsh double-quoted argument.
    nonisolated static func shellEscape(_ s: String) -> String {
        var r = s.replacingOccurrences(of: "\\", with: "\\\\")
        r = r.replacingOccurrences(of: "\"", with: "\\\"")
        r = r.replacingOccurrences(of: "$", with: "\\$")
        r = r.replacingOccurrences(of: "`", with: "\\`")
        return r
    }

    // MARK: - Watch folder (phone imports via a shared/iCloud folder)

    static let watchEnabledKey = "watchFolderEnabled"
    static let watchPathKey = "watchFolderPath"
    private static let importAudioExts: Set<String> = ["m4a", "mp3", "wav", "aac", "caf", "mp4", "m4v"]

    /// Begin polling the configured import folder for new phone recordings.
    func startWatchFolder() {
        watchTimer?.invalidate()
        watchTimer = Self.makeCommonModeTimer(interval: 20) {
            Task { await RecordingManager.shared.scanWatchFolder() }
        }
        Task { await scanWatchFolder() }  // also scan immediately
    }

    /// Scan the import folder; import one ready file per pass. iCloud placeholders
    /// are triggered to download and picked up on a later pass.
    func scanWatchFolder() async {
        guard UserDefaults.standard.bool(forKey: Self.watchEnabledKey),
              let folder = UserDefaults.standard.string(forKey: Self.watchPathKey), !folder.isEmpty,
              !isRecording, !isProcessing else { return }

        let fm = FileManager.default
        let dir = URL(fileURLWithPath: folder)
        let keys: [URLResourceKey] = [.isDirectoryKey, .isUbiquitousItemKey, .ubiquitousItemDownloadingStatusKey]
        guard let items = try? fm.contentsOfDirectory(at: dir, includingPropertiesForKeys: keys,
                                                      options: [.skipsHiddenFiles]) else { return }

        for url in items.sorted(by: { $0.lastPathComponent < $1.lastPathComponent }) {
            guard Self.importAudioExts.contains(url.pathExtension.lowercased()) else { continue }
            let rv = try? url.resourceValues(forKeys: Set(keys))
            if rv?.isDirectory == true { continue }
            // iCloud: trigger download if the file isn't local yet, then wait for a later pass.
            if rv?.isUbiquitousItem == true, rv?.ubiquitousItemDownloadingStatus != .current {
                try? fm.startDownloadingUbiquitousItem(at: url)
                continue
            }
            sendNotification(title: "Scribe", body: "Importing “\(url.lastPathComponent)” from your phone…")
            let ok = await processAudioFile(at: url.path)
            handleImported(url, success: ok, in: dir)
            return  // one per pass; the timer picks up the rest
        }
    }

    private func handleImported(_ url: URL, success: Bool, in dir: URL) {
        if success {
            // The audio is now preserved in recordings/ (governed by "Keep Audio
            // Files") plus the note/transcript, and the original Voice Memo is still
            // on the phone — so the shared copy is redundant. Remove it to avoid
            // bloating the iCloud folder.
            try? FileManager.default.removeItem(at: url)
        } else {
            // Keep failures aside so they're visible and can be retried.
            let sub = dir.appendingPathComponent("_failed")
            try? FileManager.default.createDirectory(at: sub, withIntermediateDirectories: true)
            let dest = sub.appendingPathComponent(url.lastPathComponent)
            try? FileManager.default.removeItem(at: dest)
            try? FileManager.default.moveItem(at: url, to: dest)
        }
    }

    @discardableResult
    private func runPipeline(cmd: String, projectDir: String) async -> Bool {
        // Reentrancy guard: a single processing pipeline at a time. Without this,
        // a second record/stop cycle could run two pipelines concurrently and
        // leak/clobber the shared timer and state.
        guard !isProcessing else {
            sendNotification(title: "Scribe", body: "Already processing — please wait for the current note to finish.")
            return false
        }

        isProcessing = true
        processingStage = ""
        processingDetail = ""
        completedStages = []
        transcribeProgress = -1
        lastError = nil
        statusIcon.image = ScribeApp.processingPillIcon

        // Tick a per-stage elapsed clock so steps without a determinate percentage
        // (e.g. the single Claude call) still show forward motion.
        stageStartedAt = Date()
        stageElapsed = 0
        processingTimer?.invalidate()
        processingTimer = Self.makeCommonModeTimer(interval: 1) { [weak self] in
            guard let self, let started = self.stageStartedAt else { return }
            self.stageElapsed = Int(Date().timeIntervalSince(started))
        }

        let capturedProjectDir = projectDir
        let (output, exitCode, stderrLog) = await Task.detached { [self] in
            return self.runSubprocessWithProgress(cmd: cmd, projectDir: capturedProjectDir)
        }.value

        processingTimer?.invalidate()
        processingTimer = nil
        isProcessing = false
        statusIcon.image = ScribeApp.idleMicIcon

        if exitCode == 0, let output, !output.isEmpty {
            lastError = nil
            lastNote = output
            sendNotification(title: "Scribe: Note Saved", body: output)
            return true
        } else {
            let errorLine = stderrLog
                .components(separatedBy: .newlines)
                .filter { !$0.hasPrefix("{") && !$0.isEmpty }
                .last ?? "Unknown error (exit \(exitCode))"
            lastError = errorLine
            sendNotification(title: "Scribe: Processing Failed", body: errorLine)
            return false
        }
    }

    /// Runs a pipeline command, streams stderr for stage markers, returns (stdout, exitCode, fullStderr).
    nonisolated private func runSubprocessWithProgress(cmd: String, projectDir: String) -> (String?, Int32, String) {

        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-c", cmd]
        process.currentDirectoryURL = URL(fileURLWithPath: projectDir)

        var env = ProcessInfo.processInfo.environment
        // Prepend Homebrew and conda bin dirs so tools like ffmpeg are found
        let extraPaths = ["/opt/homebrew/bin", "/usr/local/bin"]
        let existingPath = env["PATH"] ?? "/usr/bin:/bin"
        env["PATH"] = extraPaths.joined(separator: ":") + ":" + existingPath
        if let dotenvVars = loadDotEnvSync(projectDir: projectDir) {
            env.merge(dotenvVars) { _, new in new }
        }
        process.environment = env

        let outPipe = Pipe()
        let errPipe = Pipe()
        process.standardOutput = outPipe
        process.standardError = errPipe

        let stderr = LineAccumulator()

        errPipe.fileHandleForReading.readabilityHandler = { [weak self] handle in
            let data = handle.availableData
            guard !data.isEmpty,
                  let chunk = String(data: data, encoding: .utf8) else { return }
            for trimmed in stderr.feed(chunk) {
                self?.handleStderrLine(trimmed)
            }
        }

        do {
            try process.run()
        } catch {
            return (nil, -1, "Failed to launch process: \(error.localizedDescription)")
        }
        pipelineProcessBox.set(process)  // allow termination on app quit
        process.waitUntilExit()
        pipelineProcessBox.set(nil)
        errPipe.fileHandleForReading.readabilityHandler = nil

        // Drain any remaining stderr (parse stage markers from it too, then flush
        // the final partial line).
        let remaining = errPipe.fileHandleForReading.readDataToEndOfFile()
        if let s = String(data: remaining, encoding: .utf8), !s.isEmpty {
            for trimmed in stderr.feed(s) { handleStderrLine(trimmed) }
        }
        stderr.flush()

        let stdout = String(data: outPipe.fileHandleForReading.readDataToEndOfFile(), encoding: .utf8)?
            .trimmingCharacters(in: .whitespacesAndNewlines)
        return (stdout, process.terminationStatus, stderr.joined)
    }

    /// Parse a single stderr line as a JSON stage marker and update progress UI.
    nonisolated private func handleStderrLine(_ trimmed: String) {
        guard let jsonData = trimmed.data(using: .utf8),
              let obj = try? JSONSerialization.jsonObject(with: jsonData) as? [String: Any],
              let stage = obj["stage"] as? String else { return }

        let detail = obj["detail"] as? String ?? ""
        let note = obj["note"] as? String
        let pct = (obj["pct"] as? NSNumber)?.doubleValue
        Task { @MainActor [weak self] in
            guard let self else { return }
            // The "done" marker carries the saved note's filename so we can offer
            // a correction on exactly this note (not just "the latest").
            if let note, !note.isEmpty { self.lastNoteFile = note }
            // Mark every display step *before* the current one done, so collapsed
            // sub-stages (diarizing/classifying → transcribing) don't prematurely
            // complete a step or leave a gap with nothing active.
            let current = Self.displayStepFor[stage] ?? stage
            if let idx = Self.displayOrder.firstIndex(of: current) {
                for i in 0..<idx { self.completedStages.insert(Self.displayOrder[i]) }
            }
            if stage == "done" { Self.displayOrder.forEach { self.completedStages.insert($0) } }
            if self.processingStage != stage {   // new stage → restart its elapsed clock
                self.stageStartedAt = Date()
                self.stageElapsed = 0
            }
            self.processingStage = stage
            self.processingDetail = detail
            // Real transcription progress (parakeet); other stages stay indeterminate.
            if stage == "transcribing", let pct { self.transcribeProgress = pct / 100.0 }
            else if stage != "transcribing" { self.transcribeProgress = -1 }
        }
    }

    // MARK: - Folder Access

    func openNotesFolder() {
        let path = "\(projectDir)/notes"
        ensureDir(path)
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    func openRecordingsFolder() {
        let path = "\(projectDir)/recordings"
        ensureDir(path)
        NSWorkspace.shared.open(URL(fileURLWithPath: path))
    }

    private func ensureDir(_ path: String) {
        try? FileManager.default.createDirectory(atPath: path, withIntermediateDirectories: true)
    }

    // MARK: - CLI Helpers

    private func shellCommand(args: [String]) -> String {
        let python = Self.pythonPath()
        let cliArgs = args.map { "\"\($0)\"" }.joined(separator: " ")
        // `exec` replaces the zsh wrapper with Python (same PID) after the cd, so
        // interrupt()/terminate() reach the Python process directly instead of
        // killing only the shell and orphaning Python (which would keep the mic on).
        return "cd \"\(projectDir)\" && exec \"\(python)\" -m src.cli \(cliArgs)"
    }

    /// Launch a CLI subprocess. Returns nil if the process fails to launch, so
    /// callers can avoid entering a phantom "running" state.
    private func runCLI(args: [String], background: Bool = false) -> Process? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-c", shellCommand(args: args)]
        process.currentDirectoryURL = URL(fileURLWithPath: projectDir)

        var env = ProcessInfo.processInfo.environment
        let extraPaths = ["/opt/homebrew/bin", "/usr/local/bin"]
        let existingPath = env["PATH"] ?? "/usr/bin:/bin"
        env["PATH"] = extraPaths.joined(separator: ":") + ":" + existingPath
        if let dotenvVars = loadDotEnv() {
            env.merge(dotenvVars) { _, new in new }
        }
        process.environment = env

        if !background {
            process.standardOutput = Pipe()
            process.standardError = Pipe()
        }

        do {
            try process.run()
        } catch {
            lastError = "Failed to launch: \(error.localizedDescription)"
            return nil
        }
        return process
    }

    // MARK: - Web dashboard

    /// Start the local web dashboard (uvicorn) and open it in the browser.
    ///
    /// The server is launched DETACHED (`nohup … &`, reparented to launchd) with its
    /// logs redirected to a file. That keeps it alive independent of this menu-bar
    /// app's lifecycle (App Nap, process-group signals) and prevents a SIGPIPE death
    /// from the inherited stderr — both of which were killing it, so a browser
    /// refresh would hit a dead port. Any stale instance on the port is replaced.
    ///
    /// Runs with `--reload` scoped to the code dirs (src/, web/) so edits to the
    /// Python or templates are picked up live — without watching notes/ or
    /// recordings/, where a new note or recording would otherwise restart the server.
    func openDashboard() {
        let python = Self.pythonPath()
        let log = NSTemporaryDirectory() + "scribe-dashboard.log"
        let cmd = "pkill -f 'uvicorn web.app:app --port \(dashboardPort)' 2>/dev/null; sleep 0.3; "
                + "cd \"\(projectDir)\" && nohup \"\(python)\" -m uvicorn web.app:app --port \(dashboardPort) "
                + "--reload --reload-dir src --reload-dir web > \"\(log)\" 2>&1 &"
        _ = runBackgroundShell(cmd)
        // Give uvicorn a moment to bind before opening the browser.
        DispatchQueue.main.asyncAfter(deadline: .now() + 1.6) { [weak self] in
            guard let self, let url = URL(string: "http://localhost:\(self.dashboardPort)") else { return }
            NSWorkspace.shared.open(url)
        }
    }

    private func stopDashboardServer() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        p.arguments = ["-f", "uvicorn web.app:app --port \(dashboardPort)"]
        try? p.run()
        p.waitUntilExit()
    }

    /// Apply the audio-retention policy now (deletes recordings past their age).
    /// No-op when retention_days is 0. Cheap; safe to call on launch.
    func runAudioCleanup() {
        _ = runCLI(args: ["cleanup-audio"], background: true)
    }

    /// Launch a background shell command with the project env (PATH + .env merged).
    private func runBackgroundShell(_ cmd: String) -> Process? {
        let process = Process()
        process.executableURL = URL(fileURLWithPath: "/bin/zsh")
        process.arguments = ["-c", cmd]
        process.currentDirectoryURL = URL(fileURLWithPath: projectDir)
        var env = ProcessInfo.processInfo.environment
        env["PATH"] = ["/opt/homebrew/bin", "/usr/local/bin"].joined(separator: ":") + ":" + (env["PATH"] ?? "/usr/bin:/bin")
        if let dotenvVars = loadDotEnv() { env.merge(dotenvVars) { _, new in new } }
        process.environment = env
        do { try process.run() } catch { return nil }
        return process
    }

    // MARK: - Helpers

    private func loadDotEnv() -> [String: String]? {
        return Self.parseDotEnv(at: "\(projectDir)/.env")
    }

    nonisolated private func loadDotEnvSync(projectDir: String) -> [String: String]? {
        return Self.parseDotEnv(at: "\(projectDir)/.env")
    }

    nonisolated static func parseDotEnvStatic(at path: String) -> [String: String]? {
        return parseDotEnv(at: path)
    }

    nonisolated private static func parseDotEnv(at path: String) -> [String: String]? {
        guard let contents = try? String(contentsOfFile: path, encoding: .utf8) else { return nil }
        var vars: [String: String] = [:]
        for line in contents.components(separatedBy: .newlines) {
            let trimmed = line.trimmingCharacters(in: .whitespaces)
            guard !trimmed.isEmpty, !trimmed.hasPrefix("#"), trimmed.contains("=") else { continue }
            let parts = trimmed.split(separator: "=", maxSplits: 1)
            if parts.count == 2 {
                let key = String(parts[0]).trimmingCharacters(in: .whitespaces)
                var value = String(parts[1]).trimmingCharacters(in: .whitespaces)
                if (value.hasPrefix("\"") && value.hasSuffix("\"")) ||
                   (value.hasPrefix("'") && value.hasSuffix("'")) {
                    value = String(value.dropFirst().dropLast())
                }
                vars[key] = value
            }
        }
        return vars
    }

    nonisolated static func findCondaBase() -> String {
        let home = FileManager.default.homeDirectoryForCurrentUser.path
        let candidates = [
            "\(home)/miniconda3",
            "\(home)/anaconda3",
            "\(home)/miniforge3",
            "/opt/homebrew/Caskroom/miniforge/base",
            "/opt/homebrew/Caskroom/miniconda/base",
        ]
        for path in candidates {
            if FileManager.default.fileExists(atPath: "\(path)/envs") {
                return path
            }
        }
        return "\(home)/miniconda3"
    }

    /// The conda environment name. Single source of truth — overridable without a
    /// rebuild via `defaults write com.local.scribe condaEnv <name>`. Defaults to
    /// `scribe`; set the override to `chikki` to point at a legacy env.
    nonisolated static func condaEnvName() -> String {
        let stored = UserDefaults.standard.string(forKey: condaEnvKey)
        if let stored, !stored.isEmpty { return stored }
        return defaultCondaEnv
    }

    /// Absolute path to the Python interpreter inside the configured conda env.
    nonisolated static func pythonPath() -> String {
        "\(findCondaBase())/envs/\(condaEnvName())/bin/python"
    }

    private func requestNotificationPermission() {
        UNUserNotificationCenter.current().requestAuthorization(options: [.alert, .sound]) { _, _ in }
    }

    private func sendNotification(title: String, body: String) {
        let content = UNMutableNotificationContent()
        content.title = title
        content.body = body
        content.sound = .default
        let request = UNNotificationRequest(identifier: UUID().uuidString, content: content, trigger: nil)
        UNUserNotificationCenter.current().add(request)
    }

    var formattedTime: String {
        let m = elapsedSeconds / 60
        let s = elapsedSeconds % 60
        return String(format: "%02d:%02d", m, s)
    }

    // The Python pipeline emits fine-grained stages (importing, transcribing,
    // diarizing, classifying, processing, saving, done) but the menu shows three
    // steps. Diarization and auto-classification are post-transcription work, so
    // they map back onto "transcribing" — otherwise the Transcribing step reads
    // as done while Extracting is still pending and *nothing* looks active, which
    // looks frozen even though pyannote is churning away.
    static let displayStepFor: [String: String] = [
        "importing": "transcribing",
        "transcribing": "transcribing",
        "diarizing": "transcribing",
        "classifying": "transcribing",
        "processing": "processing",
        "saving": "saving",
        "done": "done",
    ]
    static let displayOrder = ["transcribing", "processing", "saving"]

    func stepState(for step: String) -> StepState {
        if completedStages.contains(step) { return .done }
        let current = Self.displayStepFor[processingStage] ?? processingStage
        if current == step { return .active }
        return .pending
    }

    func terminateSubprocesses() {
        recordProcess?.terminate()
        recordProcess = nil
        pipelineProcessBox.terminate()
        stopDashboardServer()
    }

    /// Kill any orphaned `src.cli record` processes (e.g. left by a prior crash)
    /// so the microphone isn't held across app restarts. Safe to call on launch.
    nonisolated static func killOrphanedRecorders() {
        let p = Process()
        p.executableURL = URL(fileURLWithPath: "/usr/bin/pkill")
        p.arguments = ["-f", "src.cli record"]
        try? p.run()
        p.waitUntilExit()
    }

    /// A muted pill with a pause glyph, shown while the recording is paused
    /// (silence auto-pause or a manual pause).
    static func makePausedPillIcon(time: String) -> NSImage {
        let view = ZStack {
            Capsule().fill(Color(red: 0.42, green: 0.42, blue: 0.45))
            HStack(spacing: 4) {
                HStack(spacing: 2) {
                    Capsule().fill(Color.white).frame(width: 2.5, height: 10)
                    Capsule().fill(Color.white).frame(width: 2.5, height: 10)
                }
                Text(time)
                    .font(.system(size: 10, weight: .semibold).monospacedDigit())
                    .foregroundColor(.white)
            }
        }
        .frame(width: 74, height: 20)
        let renderer = ImageRenderer(content: view)
        renderer.scale = 2.0
        let img = renderer.nsImage ?? NSImage()
        img.isTemplate = false
        return img
    }

    static func makeRecordingPillIcon(time: String, bands: [Float] = [], warning: Bool = false,
                                      idea: Bool = false) -> NSImage {
        // Amber when the input has gone quiet (likely dead/muted mic); otherwise
        // blue for an idea session and red for a meeting.
        let fill = warning ? Color(red: 0.92, green: 0.58, blue: 0.0)
                           : (idea ? Color(red: 0.18, green: 0.45, blue: 0.85) : Color.red)
        let view = ZStack {
            Capsule().fill(fill)
            HStack(spacing: 4) {
                EqualizerBars(bands: bands)
                    .frame(width: 16, height: 12)
                Text(time)
                    .font(.system(size: 10, weight: .semibold).monospacedDigit())
                    .foregroundColor(.white)
            }
        }
        .frame(width: 74, height: 20)
        let renderer = ImageRenderer(content: view)
        renderer.scale = 2.0
        let img = renderer.nsImage ?? NSImage()
        img.isTemplate = false
        return img
    }
}

/// A compact 5-bar equalizer that grows from the center, driven by the recorder's
/// live band levels (0–1). Flat bars = no audio reaching the mic.
struct EqualizerBars: View {
    let bands: [Float]
    private let count = 5
    private let maxHeight: CGFloat = 12

    var body: some View {
        HStack(alignment: .center, spacing: 1.5) {
            ForEach(0..<count, id: \.self) { i in
                let level = i < bands.count ? CGFloat(max(0, min(1, bands[i]))) : 0
                Capsule()
                    .fill(Color.white)
                    .frame(width: 2, height: max(2, level * maxHeight))
            }
        }
    }
}
