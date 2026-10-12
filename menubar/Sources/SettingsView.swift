import SwiftUI
import KeyboardShortcuts
import ServiceManagement

struct SettingsView: View {
    @State private var selectedTab = "general"

    var body: some View {
        VStack(spacing: 0) {
            TabView(selection: $selectedTab) {
                GeneralTab()
                    .tabItem { Label("General", systemImage: "gear") }
                    .tag("general")

                TranscriptionTab()
                    .tabItem { Label("Transcription", systemImage: "waveform") }
                    .tag("transcription")

                NotesTab()
                    .tabItem { Label("Notes & AI", systemImage: "sparkles") }
                    .tag("notes")

                ShortcutsTab()
                    .tabItem { Label("Shortcuts", systemImage: "keyboard") }
                    .tag("shortcuts")
            }
            Divider()
            Text(Self.versionString)
                .font(.caption2)
                .foregroundStyle(.tertiary)
                .textSelection(.enabled)
                .frame(maxWidth: .infinity, alignment: .center)
                .padding(.vertical, 6)
        }
        .frame(width: 460, height: 348)
    }

    /// "Scribe <version> · build <stamp> · <commit>" from the bundle's Info.plist
    /// (build stamp + commit are injected by build.sh; blank in a plain dev run).
    static var versionString: String {
        let info = Bundle.main.infoDictionary ?? [:]
        let short = (info["CFBundleShortVersionString"] as? String) ?? "?"
        let build = (info["CFBundleVersion"] as? String) ?? ""
        let commit = (info["ScribeGitCommit"] as? String) ?? ""
        var s = "Scribe \(short)"
        if !build.isEmpty && build != short { s += " · build \(build)" }
        if !commit.isEmpty { s += " · \(commit)" }
        return s
    }
}

// MARK: - General Tab

struct GeneralTab: View {
    @AppStorage("projectDir") private var projectDir: String = ""
    @AppStorage("watchFolderEnabled") private var watchEnabled: Bool = false
    @AppStorage("watchFolderPath") private var watchPath: String = ""
    @State private var launchAtLogin: Bool = (SMAppService.mainApp.status == .enabled)
    @State private var retentionDays: Int = 0
    @State private var userName: String = ""
    @State private var userAliases: String = ""

    var body: some View {
        Form {
            Section("Identity") {
                TextField("Your name", text: $userName)
                    .onSubmit { _ = writeUserIdentity(projectDir, name: userName, aliases: userAliases) }
                TextField("Also known as (comma-separated)", text: $userAliases)
                    .onSubmit { _ = writeUserIdentity(projectDir, name: userName, aliases: userAliases) }
                Text("Used to file action items as “Mine” in the dashboard. Include nicknames the AI might use for you. Saved when you press Return.")
                    .font(.caption2).foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Section("Startup") {
                Toggle("Open at Login", isOn: $launchAtLogin)
                    .onChange(of: launchAtLogin) { _, enabled in
                        do {
                            if enabled { try SMAppService.mainApp.register() }
                            else { try SMAppService.mainApp.unregister() }
                        } catch { launchAtLogin = !enabled }
                    }
            }

            Section("Storage") {
                Picker("Keep audio files", selection: $retentionDays) {
                    Text("Forever").tag(0)
                    Text("30 days").tag(30)
                    Text("60 days").tag(60)
                    Text("90 days").tag(90)
                    Text("180 days").tag(180)
                    Text("1 year").tag(365)
                }
                .onChange(of: retentionDays) { _, v in _ = writeRetentionDays(projectDir, v) }
                Text("Older recordings are deleted automatically when you record, process, or launch Scribe. \"Forever\" never deletes.")
                    .font(.caption2).foregroundStyle(.tertiary)
                    .fixedSize(horizontal: false, vertical: true)
            }

            Section("Phone Recordings") {
                Toggle("Import recordings from a folder", isOn: $watchEnabled)
                    .onChange(of: watchEnabled) { _, _ in RecordingManager.shared.startWatchFolder() }
                if watchEnabled {
                    LabeledContent("Folder") {
                        HStack(spacing: 8) {
                            Text(watchPath.isEmpty ? "Not set" : (watchPath as NSString).abbreviatingWithTildeInPath)
                                .font(.caption).foregroundColor(watchPath.isEmpty ? .red : .primary)
                                .lineLimit(1).truncationMode(.middle)
                            Spacer()
                            Button("Choose…") { pickWatchFolder() }.controlSize(.small)
                        }
                    }
                    Text("Record in Voice Memos, then share the memo into this iCloud folder. Scribe downloads, transcribes, and files each one automatically, then deletes the shared copy (your Voice Memos original stays on the phone; the processed audio in recordings/ is kept per “Keep audio files” above).")
                        .font(.caption2).foregroundStyle(.tertiary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }

            Section("Project Folder") {
                LabeledContent("Location") {
                    HStack(spacing: 8) {
                        Text(projectDir.isEmpty ? "Not set" : (projectDir as NSString).abbreviatingWithTildeInPath)
                            .font(.caption)
                            .foregroundColor(isValidProject ? .primary : .red)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Spacer()
                        Button("Change...") { pickFolder() }
                            .controlSize(.small)
                    }
                }
                if !isValidProject {
                    Label("config.yaml not found in this folder.", systemImage: "exclamationmark.triangle.fill")
                        .font(.caption)
                        .foregroundColor(.red)
                }
            }
        }
        .formStyle(.grouped)
        .padding(.top, 4)
        .onAppear {
            retentionDays = readRetentionDays(projectDir)
            let id = readUserIdentity(projectDir)
            userName = id.name
            userAliases = id.aliases
        }
    }

    private var isValidProject: Bool {
        !projectDir.isEmpty && FileManager.default.fileExists(atPath: projectDir + "/config.yaml")
    }

    private func pickFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Select"
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            projectDir = url.path
            Task { @MainActor in RecordingManager.shared.saveProjectDir(url.path) }
        }
    }

    private func pickWatchFolder() {
        let panel = NSOpenPanel()
        panel.canChooseDirectories = true
        panel.canChooseFiles = false
        panel.allowsMultipleSelection = false
        panel.prompt = "Select"
        panel.message = "Pick a folder (e.g. an iCloud Drive folder) to import phone recordings from"
        panel.begin { response in
            guard response == .OK, let url = panel.url else { return }
            watchPath = url.path
            Task { @MainActor in RecordingManager.shared.startWatchFolder() }
        }
    }
}

// MARK: - Transcription Tab

struct TranscriptionTab: View {
    @AppStorage("projectDir") private var projectDir: String = ""
    @AppStorage("diarizeEnabled") private var diarizeEnabled: Bool = false
    @State private var selectedEngine = ""
    @State private var engineModel = ""
    @State private var saveStatus = ""

    private let engines = [("parakeet", "Parakeet"), ("whisper", "Whisper"), ("indicwhisper", "IndicWhisper")]

    var body: some View {
        Form {
            Section("Engine") {
                Picker("Engine", selection: $selectedEngine) {
                    ForEach(engines, id: \.0) { id, label in Text(label).tag(id) }
                }
                LabeledContent("Model") {
                    TextField("e.g. mlx-community/parakeet-tdt-0.6b-v3", text: $engineModel)
                        .textFieldStyle(.roundedBorder)
                        .font(.caption)
                }
            }

            Section("Speaker Diarization") {
                Toggle("Enable Speaker Diarization", isOn: $diarizeEnabled)
                if diarizeEnabled {
                    Text("Requires pyannote.audio — install with:\npip install pyannote.audio\nSet HF_TOKEN in the Notes & AI tab.")
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                }
            }
        }
        .formStyle(.grouped)
        .padding(.top, 4)
        .safeAreaInset(edge: .bottom) { saveBar }
        .onAppear { load() }
    }

    private var saveBar: some View {
        HStack {
            Spacer()
            if !saveStatus.isEmpty {
                Text(saveStatus).font(.caption).foregroundStyle(.secondary)
            }
            Button("Save") { save() }.buttonStyle(.borderedProminent).controlSize(.small)
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 12)
    }

    private func load() {
        let cfg = parseConfig(projectDir)
        selectedEngine = cfg.engine.isEmpty ? "parakeet" : cfg.engine
        engineModel = cfg.engineModel
    }

    private func save() {
        let ok = writeConfig(projectDir, engine: selectedEngine, engineModel: engineModel, provider: nil, llmModel: nil)
        RecordingManager.shared.saveProjectDir(projectDir)
        flash($saveStatus, ok ? "Saved!" : "Save failed — check the project folder.")
    }
}

// MARK: - Notes & AI Tab

struct NotesTab: View {
    @AppStorage("projectDir") private var projectDir: String = ""
    @State private var selectedProvider = ""
    @State private var llmModel = ""
    @State private var anthropicKey = ""
    @State private var openaiKey = ""
    @State private var geminiKey = ""
    @State private var hfToken = ""
    @State private var saveStatus = ""

    private let providers = [("anthropic", "Claude (Anthropic)"), ("openai", "OpenAI"), ("gemini", "Gemini"), ("ollama", "Ollama")]

    var body: some View {
        Form {
            Section("LLM Provider") {
                Picker("Provider", selection: $selectedProvider) {
                    ForEach(providers, id: \.0) { id, label in Text(label).tag(id) }
                }
                LabeledContent("Model") {
                    TextField("e.g. claude-sonnet-4-6", text: $llmModel)
                        .textFieldStyle(.roundedBorder)
                        .font(.caption)
                }
            }

            Section("API Keys") {
                LabeledContent("Anthropic") {
                    SecureField("sk-ant-...", text: $anthropicKey).textFieldStyle(.roundedBorder).font(.caption)
                }
                LabeledContent("OpenAI") {
                    SecureField("sk-...", text: $openaiKey).textFieldStyle(.roundedBorder).font(.caption)
                }
                LabeledContent("Gemini") {
                    SecureField("AIza...", text: $geminiKey).textFieldStyle(.roundedBorder).font(.caption)
                }
                LabeledContent("HuggingFace") {
                    SecureField("hf_...", text: $hfToken).textFieldStyle(.roundedBorder).font(.caption)
                }
                Text("Stored in .env in your project folder.")
                    .font(.caption2).foregroundStyle(.tertiary)
            }
        }
        .formStyle(.grouped)
        .padding(.top, 4)
        .safeAreaInset(edge: .bottom) { saveBar }
        .onAppear { load() }
    }

    private var saveBar: some View {
        HStack {
            Spacer()
            if !saveStatus.isEmpty {
                Text(saveStatus).font(.caption).foregroundStyle(.secondary)
            }
            Button("Save") { save() }.buttonStyle(.borderedProminent).controlSize(.small)
        }
        .padding(.horizontal, 16)
        .padding(.bottom, 12)
    }

    private func load() {
        let cfg = parseConfig(projectDir)
        selectedProvider = cfg.provider.isEmpty ? "anthropic" : cfg.provider
        llmModel = cfg.llmModel
        let env = parseDotEnv(projectDir) ?? [:]
        anthropicKey = env["ANTHROPIC_API_KEY"] ?? ""
        openaiKey    = env["OPENAI_API_KEY"] ?? ""
        geminiKey    = env["GOOGLE_API_KEY"] ?? env["GEMINI_API_KEY"] ?? ""
        hfToken      = env["HF_TOKEN"] ?? ""
    }

    private func save() {
        let okConfig = writeConfig(projectDir, engine: nil, engineModel: nil, provider: selectedProvider, llmModel: llmModel)
        let okEnv = saveDotEnv(projectDir, keys: [
            "ANTHROPIC_API_KEY": anthropicKey,
            "OPENAI_API_KEY": openaiKey,
            "GOOGLE_API_KEY": geminiKey,
            "HF_TOKEN": hfToken,
        ], remove: ["GEMINI_API_KEY"])
        RecordingManager.shared.saveProjectDir(projectDir)
        flash($saveStatus, (okConfig && okEnv) ? "Saved!" : "Save failed — check the project folder.")
    }
}

// MARK: - Shortcuts Tab

struct ShortcutsTab: View {
    var body: some View {
        Form {
            Section("Recording") {
                KeyboardShortcuts.Recorder("Toggle Recording", name: .toggleRecording)
                    .padding(.vertical, 2)
                KeyboardShortcuts.Recorder("Process Audio File", name: .quickProcess)
                    .padding(.vertical, 2)
            }
        }
        .formStyle(.grouped)
        .padding(.top, 4)
    }
}

// MARK: - Shared config helpers (file-level functions)

private func parseConfig(_ projectDir: String) -> (engine: String, engineModel: String, provider: String, llmModel: String) {
    guard let contents = try? String(contentsOfFile: projectDir + "/config.yaml", encoding: .utf8) else {
        return ("", "", "", "")
    }
    var engine = "", engineModel = "", provider = "", llmModel = ""
    var section = "", inEngines = false, currentEngine = "", targetEngine = ""

    for line in contents.components(separatedBy: .newlines) {
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        guard !trimmed.isEmpty, !trimmed.hasPrefix("#") else { continue }
        let indent = line.prefix(while: { $0 == " " }).count

        if indent == 0 {
            section = trimmed.hasPrefix("transcription:") ? "transcription" : trimmed.hasPrefix("processing:") ? "processing" : ""
            inEngines = false; currentEngine = ""
            continue
        }
        if section == "transcription" {
            if indent == 2 {
                inEngines = trimmed.hasPrefix("engines:")
                if trimmed.hasPrefix("engine:") { engine = yamlVal(trimmed); targetEngine = engine }
            } else if inEngines && indent == 4 {
                currentEngine = trimmed.hasSuffix(":") ? String(trimmed.dropLast()) : trimmed
            } else if inEngines && indent == 6 && currentEngine == targetEngine {
                if trimmed.hasPrefix("model:") { engineModel = yamlVal(trimmed) }
            }
        } else if section == "processing" && indent == 2 {
            if trimmed.hasPrefix("provider:") { provider = yamlVal(trimmed) }
            else if trimmed.hasPrefix("model:") { llmModel = yamlVal(trimmed) }
        }
    }
    return (engine, engineModel, provider, llmModel)
}

private func yamlVal(_ line: String) -> String {
    guard let colon = line.firstIndex(of: ":") else { return "" }
    return String(line[line.index(after: colon)...])
        .trimmingCharacters(in: .whitespaces)
        .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
}

@discardableResult
private func writeConfig(_ projectDir: String, engine: String?, engineModel: String?, provider: String?, llmModel: String?) -> Bool {
    let path = projectDir + "/config.yaml"
    guard let text = try? String(contentsOfFile: path, encoding: .utf8) else { return false }
    var lines = text.components(separatedBy: "\n")
    var section = "", inEngines = false, currentEngine = ""

    for i in 0..<lines.count {
        let line = lines[i]
        let trimmed = line.trimmingCharacters(in: .whitespaces)
        // Skip blank lines and comments WITHOUT resetting section state — a blank
        // line has indent 0 and would otherwise drop us out of the current section,
        // so keys after a blank line (e.g. engines:) would never be rewritten.
        guard !trimmed.isEmpty, !trimmed.hasPrefix("#") else { continue }
        let indent = line.prefix(while: { $0 == " " }).count
        let pad = String(repeating: " ", count: indent)

        if indent == 0 {
            section = trimmed.hasPrefix("transcription:") ? "transcription" : trimmed.hasPrefix("processing:") ? "processing" : ""
            inEngines = false; currentEngine = ""
            continue
        }
        if section == "transcription" {
            if indent == 2 {
                inEngines = trimmed.hasPrefix("engines:")
                if trimmed.hasPrefix("engine:"), let e = engine { lines[i] = "\(pad)engine: \(e)" }
            } else if inEngines && indent == 4 {
                currentEngine = trimmed.hasSuffix(":") ? String(trimmed.dropLast()) : trimmed
            } else if inEngines && indent == 6 && currentEngine == (engine ?? "") {
                if trimmed.hasPrefix("model:"), let m = engineModel, !m.isEmpty { lines[i] = "\(pad)model: \(m)" }
            }
        } else if section == "processing" && indent == 2 {
            if trimmed.hasPrefix("provider:"), let p = provider { lines[i] = "\(pad)provider: \(p)" }
            else if trimmed.hasPrefix("model:"), let m = llmModel, !m.isEmpty { lines[i] = "\(pad)model: \(m)" }
        }
    }
    do {
        try lines.joined(separator: "\n").write(toFile: path, atomically: true, encoding: .utf8)
        return true
    } catch {
        return false
    }
}

private func readUserIdentity(_ projectDir: String) -> (name: String, aliases: String) {
    guard let text = try? String(contentsOfFile: projectDir + "/config.yaml", encoding: .utf8),
          let block = text.range(of: #"(?m)^user:\n(?:[ \t]+.*\n?)*"#, options: .regularExpression) else {
        return ("", "")
    }
    let userBlock = String(text[block])
    func value(_ key: String) -> String {
        guard let r = userBlock.range(of: "(?m)^[ \\t]+\(key):[ \\t]*(.*)$", options: .regularExpression),
              let colon = userBlock[r].firstIndex(of: ":") else { return "" }
        return String(userBlock[r][userBlock[r].index(after: colon)...])
            .trimmingCharacters(in: .whitespaces)
            .trimmingCharacters(in: CharacterSet(charactersIn: "\"'"))
    }
    return (value("name"), value("aliases"))
}

@discardableResult
private func writeUserIdentity(_ projectDir: String, name: String, aliases: String) -> Bool {
    let path = projectDir + "/config.yaml"
    guard var text = try? String(contentsOfFile: path, encoding: .utf8) else { return false }
    func esc(_ s: String) -> String { "\"" + s.replacingOccurrences(of: "\"", with: "\\\"") + "\"" }
    let block = "user:\n  name: \(esc(name))\n  aliases: \(esc(aliases))\n"
    if let r = text.range(of: #"(?m)^user:\n(?:[ \t]+.*\n?)*"#, options: .regularExpression) {
        text.replaceSubrange(r, with: block)
    } else {
        if !text.hasSuffix("\n") { text += "\n" }
        text += "\n" + block
    }
    return (try? text.write(toFile: path, atomically: true, encoding: .utf8)) != nil
}

private func readRetentionDays(_ projectDir: String) -> Int {
    guard let text = try? String(contentsOfFile: projectDir + "/config.yaml", encoding: .utf8),
          let r = text.range(of: #"retention_days:\s*\d+"#, options: .regularExpression) else {
        return 0
    }
    return Int(String(text[r].filter { $0.isNumber })) ?? 0
}

@discardableResult
private func writeRetentionDays(_ projectDir: String, _ days: Int) -> Bool {
    let path = projectDir + "/config.yaml"
    guard var text = try? String(contentsOfFile: path, encoding: .utf8) else { return false }
    if let r = text.range(of: #"(?m)^[ \t]*retention_days:.*$"#, options: .regularExpression) {
        let indent = text[r].prefix(while: { $0 == " " || $0 == "\t" })
        text.replaceSubrange(r, with: "\(indent)retention_days: \(days)")
    } else {
        return false  // config.yaml ships with the key under recording:
    }
    return (try? text.write(toFile: path, atomically: true, encoding: .utf8)) != nil
}

private func parseDotEnv(_ projectDir: String) -> [String: String]? {
    guard let contents = try? String(contentsOfFile: projectDir + "/.env", encoding: .utf8) else { return nil }
    var vars: [String: String] = [:]
    for line in contents.components(separatedBy: .newlines) {
        let t = line.trimmingCharacters(in: .whitespaces)
        guard !t.isEmpty, !t.hasPrefix("#"), t.contains("=") else { continue }
        let parts = t.split(separator: "=", maxSplits: 1)
        guard parts.count == 2 else { continue }
        let k = String(parts[0]).trimmingCharacters(in: .whitespaces)
        var v = String(parts[1]).trimmingCharacters(in: .whitespaces)
        if (v.hasPrefix("\"") && v.hasSuffix("\"")) || (v.hasPrefix("'") && v.hasSuffix("'")) { v = String(v.dropFirst().dropLast()) }
        vars[k] = v
    }
    return vars
}

@discardableResult
private func saveDotEnv(_ projectDir: String, keys: [String: String], remove: [String] = []) -> Bool {
    let path = projectDir + "/.env"
    var lines = (try? String(contentsOfFile: path, encoding: .utf8))?.components(separatedBy: "\n") ?? []
    for key in keys.keys + remove { lines = lines.filter { !$0.hasPrefix("\(key)=") } }
    for (key, value) in keys where !value.isEmpty { lines.append("\(key)=\(value)") }
    do {
        try lines.joined(separator: "\n").write(toFile: path, atomically: true, encoding: .utf8)
        return true
    } catch {
        return false
    }
}

private func flash(_ status: Binding<String>, _ message: String) {
    status.wrappedValue = message
    DispatchQueue.main.asyncAfter(deadline: .now() + 2) { status.wrappedValue = "" }
}
