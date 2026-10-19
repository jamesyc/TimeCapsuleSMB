import AppKit
import SwiftUI

struct ErrorBlock: View {
    let presentation: RecoveryGuidancePresentation

    init(error: BackendErrorViewModel) {
        self.presentation = RecoveryGuidancePresentation(error: error)
    }

    init(presentation: RecoveryGuidancePresentation) {
        self.presentation = presentation
    }

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(presentation.title)
                .font(.body.weight(.medium))
            if let message = presentation.message {
                Text(message)
                    .font(.caption)
            }
        }
        .foregroundStyle(.red)
    }
}

struct ErrorRecoveryView: View {
    let error: BackendErrorViewModel
    let guidance: String?
    let onAction: (RecoveryAction) -> Void
    let diagnosticsText: (() -> String)?
    /// False where nothing reruns the failed operation from this box: the
    /// screen's own buttons do that, and a Retry here would do nothing.
    let canRetry: Bool

    init(
        error: BackendErrorViewModel,
        guidance: String? = nil,
        diagnosticsText: (() -> String)? = nil,
        canRetry: Bool = true,
        onAction: @escaping (RecoveryAction) -> Void
    ) {
        self.error = error
        self.guidance = guidance
        self.diagnosticsText = diagnosticsText
        self.canRetry = canRetry
        self.onAction = onAction
    }

    var body: some View {
        let presentation = RecoveryGuidancePresentation(error: error)
        VStack(alignment: .leading, spacing: 6) {
            ErrorBlock(presentation: presentation)
            if let guidance {
                Text(guidance)
                    .font(.caption)
                    .foregroundStyle(.red)
            }
            if !presentation.steps.isEmpty {
                VStack(alignment: .leading, spacing: 3) {
                    Text(L10n.string("recovery.guidance.next_steps"))
                        .font(.caption.weight(.medium))
                        .foregroundStyle(.secondary)
                    ForEach(Array(presentation.steps.enumerated()), id: \.offset) { index, step in
                        Text("\(index + 1). \(step)")
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }
            if let details = presentation.details {
                Text(details)
                    .font(.caption2)
                    .foregroundStyle(.secondary)
                    .textSelection(.enabled)
            }
            let actions = RecoveryActionMapper.actions(for: error, canRetry: canRetry)
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
            diagnosticsText?() ?? "\(error.operation) \(error.code): \(error.diagnosticMessage ?? error.message)",
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
        case .replacePassword:
            return "key"
        case .copyDiagnostics:
            return "doc.on.doc"
        case .diagnostics:
            return "wrench.and.screwdriver"
        case .openSystemSettings:
            return "gearshape"
        case .openSSHAccess:
            return "key"
        case .generic:
            return "arrow.right.circle"
        }
    }
}
