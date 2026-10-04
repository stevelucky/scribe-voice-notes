import SwiftUI
import KeyboardShortcuts

struct MenuBarView: View {
    @EnvironmentObject var recorder: RecordingManager

    private var showTranscribeBar: Bool {
        recorder.stepState(for: "transcribing") == .active && recorder.transcribeProgress >= 0
    }
    private var transcribeBarValue: Double {
        min(max(recorder.transcribeProgress, 0), 1)
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            if recorder.isRecording {
                HStack {
                    Circle()
                        .fill(recorder.isPaused ? Color.gray : .red)
                        .frame(width: 8, height: 8)
                    // Live elapsed time is shown in the menu-bar icon itself; keeping
                    // it out of the dropdown means this menu never re-renders while
                    // open, so hover highlighting stays stable.
                    Text(recorder.isPaused
                         ? recorder.pauseStatusText
                         : (recorder.captureMode == .idea ? "Recording idea" : "Recording"))
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .allowsHitTesting(false)

                Button(recorder.isPaused ? "Resume Recording" : "Pause Recording") {
                    recorder.togglePause()
                }

                Button("Stop Recording") {
                    Task { await recorder.stopRecording() }
                }
                // Shows the user's *current* Toggle Recording shortcut and updates
                // live when they change it in Settings (not a hardcoded ⌘⇧R).
                .globalKeyboardShortcut(.toggleRecording)

                Button("Stop & Cancel") {
                    DispatchQueue.main.asyncAfter(deadline: .now() + 0.2) {
                        NSApp.activate(ignoringOtherApps: true)
                        let alert = NSAlert()
                        alert.messageText = "Cancel Recording?"
                        alert.informativeText = "The recording will be deleted and not processed."
                        alert.alertStyle = .warning
                        alert.addButton(withTitle: "Delete Recording")
                        alert.addButton(withTitle: "Keep Recording")
                        if alert.runModal() == .alertFirstButtonReturn {
                            Task { await recorder.cancelRecording() }
                        }
                    }
                }

            } else if recorder.isProcessing {
                // Multi-step progress
                VStack(alignment: .leading, spacing: 6) {
                    Text("Processing")
                        .fontWeight(.semibold)

                    ProcessingStepView(
                        label: "Transcribing audio",
                        state: recorder.stepState(for: "transcribing"),
                        trailing: recorder.stepState(for: "transcribing") == .active ? recorder.stageElapsedText : ""
                    )
                    ProcessingStepView(
                        label: "Extracting notes (\(recorder.llmProvider))",
                        state: recorder.stepState(for: "processing"),
                        trailing: recorder.stepState(for: "processing") == .active ? recorder.stageElapsedText : ""
                    )
                    ProcessingStepView(
                        label: "Saving & exporting",
                        state: recorder.stepState(for: "saving"),
                        trailing: recorder.stepState(for: "saving") == .active ? recorder.stageElapsedText : ""
                    )

                    // One real progress bar (transcription only — the sole step with
                    // determinate progress). Reserve its height even when hidden so
                    // menu items below don't shift as it appears/disappears.
                    ProgressView(value: transcribeBarValue)
                        .progressViewStyle(.linear)
                        .controlSize(.small)
                        .opacity(showTranscribeBar ? 1 : 0)
                        .frame(height: 4)

                    // Always render this row (even when empty) at a fixed height.
                    // If it toggled in/out, every menu item below it would shift by
                    // a row as stages advance, and while the menu is open that frame
                    // change scrambles AppKit's hover highlighting (items "bounce").
                    Text(recorder.processingDetail.isEmpty ? " " : recorder.processingDetail)
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(1)
                        .frame(height: 14, alignment: .leading)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .frame(minWidth: 260)
                .allowsHitTesting(false)

            } else if recorder.isCorrecting {
                VStack(alignment: .leading, spacing: 4) {
                    Text("Applying your correction…")
                        .fontWeight(.semibold)
                    Text("Re-reading the transcript and regenerating the note.")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                        .lineLimit(2)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 6)
                .frame(minWidth: 260, alignment: .leading)
                .allowsHitTesting(false)

            } else {
                Button("Record Meeting") {
                    Task { await recorder.startRecording() }
                }
                .globalKeyboardShortcut(.toggleRecording)

                // Capture a long-term idea: same recording flow, but processed
                // idea-shaped (no to-do hunting). Surfaces under the Someday tab.
                Button("Record Idea") {
                    Task { await recorder.startRecording(mode: .idea) }
                }

                Button("Process Audio File...") {
                    recorder.pickAndProcessAudioFile()
                }
                .globalKeyboardShortcut(.quickProcess)

                if recorder.lastNoteFile != nil {
                    Button("Fix Last Note…") {
                        CorrectionWindowController.shared.show()
                    }
                }
            }

            Divider()

            if let error = recorder.lastError {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Processing failed:")
                        .font(.caption2)
                        .foregroundStyle(.red)
                    Text(error)
                        .font(.caption)
                        .foregroundStyle(.red)
                        .lineLimit(4)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 4)
                .allowsHitTesting(false)
                Divider()
            } else if let lastNote = recorder.lastNote, !lastNote.isEmpty {
                VStack(alignment: .leading, spacing: 2) {
                    Text("Last note:")
                        .font(.caption2)
                        .foregroundStyle(.tertiary)
                    Text(lastNote)
                        .font(.caption)
                        .lineLimit(3)
                        .fixedSize(horizontal: false, vertical: true)
                }
                .padding(.horizontal, 8)
                .padding(.vertical, 2)
                .frame(maxWidth: 260, alignment: .leading)
                .allowsHitTesting(false)
                Divider()
            }

            Button("Open Dashboard") {
                recorder.openDashboard()
            }

            Button("Open Notes Folder") {
                recorder.openNotesFolder()
            }

            Button("Open Recordings Folder") {
                recorder.openRecordingsFolder()
            }

            Divider()

            Button("Manage Speakers...") {
                SpeakerWindowController.shared.show()
            }

            Button("Settings...") {
                SettingsWindowController.shared.show()
            }
            .keyboardShortcut(",")

            Button("Quit Scribe") {
                NSApplication.shared.terminate(nil)
            }
            .keyboardShortcut("q")
        }
        .padding(.vertical, 4)
    }
}


enum StepState {
    case pending
    case active
    case done
}

struct ProcessingStepView: View {
    let label: String
    let state: StepState
    var trailing: String = ""   // e.g. an elapsed timer on the active step

    var body: some View {
        HStack(spacing: 6) {
            icon
                .frame(width: 13, height: 13)   // fixed, so labels stay aligned
            Text(label)
                .font(.caption)
                .fontWeight(state == .active ? .semibold : .regular)
                .foregroundStyle(labelColor)
            if !trailing.isEmpty {
                Text("· \(trailing)")
                    .font(.caption)
                    .foregroundStyle(state == .active ? .secondary : .tertiary)
            }
            Spacer(minLength: 0)
        }
    }

    @ViewBuilder private var icon: some View {
        switch state {
        case .pending:
            Text("○").font(.caption).foregroundStyle(.quaternary)
        case .active:
            // A live spinner is the clearest "this is actually running" signal.
            ProgressView()
                .controlSize(.small)
                .scaleEffect(0.65)
        case .done:
            Text("✓").font(.caption).foregroundStyle(.green)
        }
    }

    private var labelColor: Color {
        switch state {
        case .active:  return .primary          // black/high-contrast: the live step
        case .done:    return .secondary
        case .pending: return Color.secondary.opacity(0.55)
        }
    }
}
