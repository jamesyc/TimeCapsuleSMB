import SwiftUI

struct DoctorView: View {
    @ObservedObject var store: DoctorStore
    @Binding var password: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("screen.doctor"))
                .font(.title2.weight(.semibold))

            HStack {
                TextField(L10n.string("field.bonjour_timeout"), text: $store.bonjourTimeout)
                    .frame(width: 180)
                Toggle("Skip SSH", isOn: $store.skipSSH)
                Toggle("Skip Bonjour", isOn: $store.skipBonjour)
                Toggle("Skip SMB", isOn: $store.skipSMB)
            }

            HStack {
                Button {
                    store.runDoctor(password: password)
                } label: {
                    Label(L10n.string("button.run_doctor"), systemImage: "stethoscope")
                }
                .disabled(store.isRunning || store.bonjourTimeoutValue == nil)

                Label(store.state.title, systemImage: statusIcon)
                    .foregroundStyle(statusColor)
            }

            if let stage = store.currentStage {
                HStack(spacing: 8) {
                    Text(stage.stage)
                        .font(.system(.caption, design: .monospaced))
                    if let description = stage.description {
                        Text(description)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                    }
                }
            }

            if let summary = store.summary {
                DoctorSummaryView(summary: summary)
            }

            if let error = store.error {
                DoctorErrorView(error: error)
            }
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var statusIcon: String {
        switch store.state {
        case .idle:
            return "circle"
        case .running:
            return "hourglass"
        case .passed:
            return "checkmark.circle"
        case .warning:
            return "exclamationmark.circle"
        case .failed, .runFailed:
            return "exclamationmark.triangle"
        }
    }

    private var statusColor: Color {
        switch store.state {
        case .passed:
            return .green
        case .warning:
            return .orange
        case .failed, .runFailed:
            return .red
        default:
            return .secondary
        }
    }
}

private struct DoctorSummaryView: View {
    let summary: DoctorSummary

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack(spacing: 12) {
                Text("PASS \(summary.passCount)").foregroundStyle(.green)
                Text("WARN \(summary.warnCount)").foregroundStyle(.orange)
                Text("FAIL \(summary.failCount)").foregroundStyle(.red)
                Text("INFO \(summary.infoCount)").foregroundStyle(.secondary)
            }
            .font(.caption.weight(.medium))

            ForEach(summary.groups) { group in
                VStack(alignment: .leading, spacing: 4) {
                    Text(group.domain)
                        .font(.body.weight(.medium))
                    ForEach(Array(group.checks.enumerated()), id: \.offset) { _, check in
                        HStack(alignment: .top) {
                            Text(check.status)
                                .font(.system(.caption, design: .monospaced))
                                .foregroundStyle(color(for: check.status))
                                .frame(width: 44, alignment: .leading)
                            Text(check.message)
                                .font(.caption)
                        }
                    }
                }
            }
        }
    }

    private func color(for status: String) -> Color {
        switch status {
        case "PASS":
            return .green
        case "WARN":
            return .orange
        case "FAIL":
            return .red
        default:
            return .secondary
        }
    }
}

private struct DoctorErrorView: View {
    let error: BackendErrorViewModel

    var body: some View {
        VStack(alignment: .leading, spacing: 4) {
            Text(error.recovery?.title ?? error.code)
                .font(.body.weight(.medium))
            Text(error.message)
                .font(.caption)
            if let recovery = error.recovery, !recovery.actions.isEmpty {
                ForEach(recovery.actions, id: \.self) { action in
                    Text(action)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
        .foregroundStyle(.red)
    }
}
