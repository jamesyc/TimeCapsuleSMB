import AppKit
import SwiftUI

struct ErrorBlock: View {
    let error: BackendErrorViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(error.recovery?.title ?? error.code)
                .font(.body.weight(.medium))
            Text(error.message)
                .font(.caption)
        }
        .foregroundStyle(.red)
    }
}

struct ErrorRecoveryView: View {
    let error: BackendErrorViewModel
    let guidance: String?
    let onAction: (RecoveryAction) -> Void
    let diagnosticsText: (() -> String)?

    init(
        error: BackendErrorViewModel,
        guidance: String? = nil,
        diagnosticsText: (() -> String)? = nil,
        onAction: @escaping (RecoveryAction) -> Void
    ) {
        self.error = error
        self.guidance = guidance
        self.diagnosticsText = diagnosticsText
        self.onAction = onAction
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ErrorBlock(error: error)
            if let guidance {
                Text(guidance)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
            let actions = RecoveryActionMapper.actions(for: error)
            if !actions.isEmpty {
                HStack {
                    ForEach(actions) { action in
                        Button {
                            if action.kind == .copyDiagnostics {
                                copyDiagnostics()
                            } else {
                                onAction(action)
                            }
                        } label: {
                            Label(action.title, systemImage: icon(for: action.kind))
                        }
                        .disabled(!isActionable(action))
                    }
                }
            }
        }
    }

    private func isActionable(_ action: RecoveryAction) -> Bool {
        action.kind != .generic
    }

    private func copyDiagnostics() {
        let pasteboard = NSPasteboard.general
        pasteboard.clearContents()
        pasteboard.setString(
            diagnosticsText?() ?? "\(error.operation) \(error.code): \(error.message)",
            forType: .string
        )
    }

    private func icon(for kind: RecoveryActionKind) -> String {
        switch kind {
        case .retry:
            return "arrow.clockwise"
        case .runCheckup:
            return "stethoscope"
        case .installSMB:
            return "square.and.arrow.down.on.square"
        case .startSMB:
            return "play.circle"
        case .uninstall:
            return "trash"
        case .diskRepair:
            return "externaldrive.badge.exclamationmark"
        case .metadataRepair:
            return "tag"
        case .openFinder:
            return "folder"
        case .replacePassword:
            return "key"
        case .copyDiagnostics:
            return "doc.on.doc"
        case .diagnostics:
            return "wrench.and.screwdriver"
        case .generic:
            return "arrow.right.circle"
        }
    }
}
