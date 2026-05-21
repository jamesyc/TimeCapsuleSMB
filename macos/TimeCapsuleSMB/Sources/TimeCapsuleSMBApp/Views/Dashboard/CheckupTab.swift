import SwiftUI

struct CheckupTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let showDiagnostics: () -> Void

    var body: some View {
        let store = session.doctorStore
        let presentation = CheckupPresentation(
            summary: store.summary,
            state: store.state,
            events: store.events,
            currentStage: store.currentStage,
            hostWarning: HostCompatibilityPolicy.warning()
        )

        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                CheckupHeaderView(presentation: presentation)

                if let warning = presentation.hostWarning {
                    WarningBanner(warning: warning)
                }

                if let action = presentation.primaryAction {
                    Button {
                        session.performCheckupAction(action, profile: profile, showDiagnostics: showDiagnostics)
                    } label: {
                        Label(action.title, systemImage: action.systemImage)
                    }
                    .buttonStyle(.borderedProminent)
                    .disabled(store.isRunning || store.bonjourTimeoutValue == nil)
                }

                if !presentation.timeline.isEmpty {
                    CheckupTimelineView(items: presentation.timeline)
                }

                if !presentation.summaryRows.isEmpty {
                    SummaryGrid(rows: presentation.summaryRows.map { ($0.label, $0.value) })
                }

                ForEach(presentation.domains) { domain in
                    CheckupDomainView(domain: domain)
                }

                CheckupAdvancedOptionsView(store: store)

                if let error = store.error {
                    ErrorRecoveryView(error: error) { action in
                        handleRecovery(action: action, error: error)
                    }
                }
            }
            .frame(maxWidth: .infinity, alignment: .leading)
        }
    }

    private func handleRecovery(action: RecoveryAction, error: BackendErrorViewModel) {
        if action.kind == .diagnostics {
            showDiagnostics()
            return
        }
        _ = session.handleRecoveryAction(action, error: error, profile: profile)
    }
}

private struct CheckupHeaderView: View {
    let presentation: CheckupPresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack(alignment: .firstTextBaseline) {
                Text(presentation.title)
                    .font(.title2.weight(.semibold))
                Spacer()
                Text(presentation.stateTitle)
                    .font(.caption.weight(.medium))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 5)
                    .background(.quaternary)
                    .clipShape(Capsule())
            }
            Text(presentation.headline)
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }
}

private struct CheckupTimelineView: View {
    let items: [OperationTimelineItem]

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(L10n.string("checkup.timeline.title"))
                .font(.headline)
            ForEach(items) { item in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: icon(for: item.state))
                        .frame(width: 16)
                    VStack(alignment: .leading, spacing: 2) {
                        Text(item.title)
                            .font(.body.weight(.medium))
                        if let detail = item.detail {
                            Text(detail)
                                .font(.caption)
                                .foregroundStyle(.secondary)
                        }
                    }
                }
            }
        }
    }

    private func icon(for state: OperationTimelineItem.State) -> String {
        switch state {
        case .pending:
            return "circle"
        case .running:
            return "progress.indicator"
        case .succeeded:
            return "checkmark.circle"
        case .warning:
            return "exclamationmark.triangle"
        case .failed:
            return "xmark.octagon"
        }
    }
}

private struct CheckupDomainView: View {
    let domain: CheckupDomainPresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Label(domain.title, systemImage: domain.status.systemImage)
                    .font(.headline)
                Spacer()
                Text(domain.countSummary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ForEach(domain.rows) { row in
                HStack(alignment: .top, spacing: 8) {
                    Label(row.status.title, systemImage: row.status.systemImage)
                        .labelStyle(.iconOnly)
                        .frame(width: 16)
                    Text(row.statusText)
                        .font(.system(.caption, design: .monospaced))
                        .frame(width: 44, alignment: .leading)
                    Text(row.message)
                        .font(.caption)
                }
            }
        }
        .padding(10)
        .background(Color.secondary.opacity(0.08))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }
}

private struct CheckupAdvancedOptionsView: View {
    @ObservedObject var store: DoctorStore

    var body: some View {
        DisclosureGroup(L10n.string("checkup.advanced_options")) {
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                GridRow {
                    Text(L10n.string("field.bonjour_timeout"))
                        .foregroundStyle(.secondary)
                    TextField(L10n.string("field.bonjour_timeout"), text: $store.bonjourTimeout)
                        .frame(width: 180)
                }
                GridRow {
                    Toggle(L10n.string("checkup.option.skip_ssh"), isOn: $store.skipSSH)
                    Toggle(L10n.string("checkup.option.skip_bonjour"), isOn: $store.skipBonjour)
                }
                GridRow {
                    Toggle(L10n.string("checkup.option.skip_smb"), isOn: $store.skipSMB)
                    EmptyView()
                }
            }
            .padding(.top, 8)
        }
    }
}
