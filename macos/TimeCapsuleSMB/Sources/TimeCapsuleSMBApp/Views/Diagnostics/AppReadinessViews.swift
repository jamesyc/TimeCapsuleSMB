import AppKit
import SwiftUI
import UniformTypeIdentifiers

struct AppReadinessBannerView: View {
    @ObservedObject var store: AppReadinessStore
    let showDiagnostics: () -> Void

    var body: some View {
        switch store.state {
        case .idle, .ready:
            EmptyView()
        case .resolvingBundle, .checkingVersion, .checkingCapabilities, .validatingInstall:
            HStack(spacing: 10) {
                ProgressView()
                    .controlSize(.small)
                Text(title)
                    .font(.caption)
                if let stage = store.currentStage?.description ?? store.currentStage?.stage {
                    Text(stage)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
            .background(Color.secondary.opacity(0.08))
        case .degraded(_, let issues):
            HStack(spacing: 10) {
                Image(systemName: "exclamationmark.triangle")
                    .foregroundStyle(.yellow)
                Text(issues.first?.message ?? L10n.string("readiness.warning.default"))
                    .font(.caption)
                Spacer()
                Button(L10n.string("toolbar.diagnostics"), action: showDiagnostics)
            }
            .padding(.horizontal)
            .padding(.vertical, 8)
            .background(Color.yellow.opacity(0.12))
        case .blocked:
            EmptyView()
        }
    }

    private var title: String {
        switch store.state.kind {
        case .resolvingBundle:
            return L10n.string("readiness.state.resolving_bundle")
        case .checkingVersion:
            return L10n.string("readiness.state.checking_version")
        case .checkingCapabilities:
            return L10n.string("readiness.state.checking_capabilities")
        case .validatingInstall:
            return L10n.string("readiness.state.validating_install")
        default:
            return ""
        }
    }
}

struct AppReadinessBlockedView: View {
    @ObservedObject var store: AppReadinessStore
    let showDiagnostics: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            Label(L10n.string("readiness.blocked.title"), systemImage: "exclamationmark.octagon")
                .font(.title2.weight(.semibold))
                .foregroundStyle(.red)
            if case .blocked(let issue) = store.state {
                Text(issue.message)
                Text(issue.recovery)
                    .foregroundStyle(.secondary)
            }
            HStack {
                Button {
                    store.start()
                } label: {
                    Label(L10n.string("recovery.action.retry"), systemImage: "arrow.clockwise")
                }
                .disabled(!store.canRetry)

                Button {
                    showDiagnostics()
                } label: {
                    Label(L10n.string("toolbar.diagnostics"), systemImage: "wrench.and.screwdriver")
                }
            }
        }
        .padding()
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
    }
}

struct AppDiagnosticsView: View {
    @ObservedObject var store: AppReadinessStore
    let exportContext: (_ includeBackendEvents: Bool) -> DiagnosticsExportContext
    @Binding var showBackendEvents: Bool
    @Binding var helperPath: String
    @Environment(\.dismiss) private var dismiss
    @State private var exportStatus: String?

    var body: some View {
        VStack(alignment: .leading, spacing: 14) {
            HStack {
                Text(L10n.string("diagnostics.title"))
                    .font(.title2.weight(.semibold))
                Spacer()
                if let exportStatus {
                    Text(exportStatus)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Button {
                    copyDiagnostics()
                } label: {
                    Label(L10n.string("diagnostics.copy"), systemImage: "doc.on.doc")
                }
                Button {
                    saveDiagnostics()
                } label: {
                    Label(L10n.string("diagnostics.save"), systemImage: "square.and.arrow.down")
                }
                Button(L10n.string("action.done")) {
                    dismiss()
                }
                .keyboardShortcut(.defaultAction)
            }

            TextField(L10n.string("field.helper"), text: $helperPath)

            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 6) {
                GridRow {
                    Text(L10n.string("diagnostics.state")).foregroundStyle(.secondary)
                    Text(store.state.kind.title)
                }
                if let capabilities = store.capabilities {
                    GridRow {
                        Text(L10n.string("diagnostics.helper")).foregroundStyle(.secondary)
                        Text(capabilities.helperVersion)
                    }
                    GridRow {
                        Text(L10n.string("diagnostics.distribution")).foregroundStyle(.secondary)
                        Text(capabilities.distributionRoot)
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
                if let validation = store.validation {
                    GridRow {
                        Text(L10n.string("diagnostics.validation")).foregroundStyle(.secondary)
                        Text(validation.summary)
                    }
                }
            }
            .font(.caption)

            if !store.issues.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    Text(L10n.string("diagnostics.runtime_issues"))
                        .font(.headline)
                    ForEach(store.issues) { issue in
                        VStack(alignment: .leading, spacing: 2) {
                            Text(issue.message)
                            Text(issue.recovery)
                                .foregroundStyle(.secondary)
                        }
                        .font(.caption)
                    }
                }
            }

            Toggle(L10n.string("diagnostics.backend_events"), isOn: $showBackendEvents)
                .font(.headline)
            if showBackendEvents {
                EventList(events: exportContext(true).events)
            }
        }
        .padding()
        .frame(minWidth: 720, minHeight: 520)
    }

    private func copyDiagnostics() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(exportText(), forType: .string)
        exportStatus = L10n.string("diagnostics.copied")
    }

    private func saveDiagnostics() {
        let panel = NSSavePanel()
        panel.nameFieldStringValue = "TimeCapsuleSMB-Diagnostics.txt"
        panel.allowedContentTypes = [.plainText]
        let text = exportText()
        panel.begin { response in
            guard response == .OK, let url = panel.url else {
                return
            }
            do {
                try text.write(to: url, atomically: true, encoding: .utf8)
                Task { @MainActor in
                    exportStatus = L10n.string("diagnostics.saved")
                }
            } catch {
                Task { @MainActor in
                    exportStatus = error.localizedDescription
                }
            }
        }
    }

    private func exportText() -> String {
        DiagnosticsExportBuilder().build(context: exportContext(showBackendEvents))
    }
}

struct EventList: View {
    let events: [BackendEvent]

    var body: some View {
        List(events) { event in
            VStack(alignment: .leading, spacing: 4) {
                Text(event.summary)
                    .font(.body)
                if let payload = event.payload, event.type == "result" {
                    Text(payload.displayText)
                        .font(.system(.caption, design: .monospaced))
                        .foregroundStyle(.secondary)
                        .lineLimit(6)
                }
            }
            .padding(.vertical, 3)
        }
    }
}
