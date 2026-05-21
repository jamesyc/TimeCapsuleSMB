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
    let onAction: (RecoveryAction) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            ErrorBlock(error: error)
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
        pasteboard.setString("\(error.operation) \(error.code): \(error.message)", forType: .string)
    }

    private func icon(for kind: RecoveryActionKind) -> String {
        switch kind {
        case .retry:
            return "arrow.clockwise"
        case .runCheckup:
            return "stethoscope"
        case .installSMB:
            return "square.and.arrow.up"
        case .startSMB:
            return "play.circle"
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
