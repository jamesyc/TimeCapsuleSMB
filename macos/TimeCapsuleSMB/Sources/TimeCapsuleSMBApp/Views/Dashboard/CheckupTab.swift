import SwiftUI

struct CheckupTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let appSettings: AppSettings
    let showDiagnostics: () -> Void
    let diagnosticsText: () -> String

    var body: some View {
        let store = session.doctorStore
        let presentation = CheckupPresentation(
            summary: store.summary,
            state: store.state,
            events: store.events,
            currentStage: store.currentStage,
            hostWarning: HostCompatibilityPolicy.warning(enabled: appSettings.timeMachineWarningsEnabled)
        )
        let progress = CheckupProgressPresentation(state: store.state, currentStage: store.currentStage)

        ZStack {
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
                        .disabled(store.isRunning)
                    }

                    if !presentation.timeline.isEmpty {
                        OperationTimelineListView(
                            title: L10n.string("checkup.timeline.title"),
                            items: presentation.timeline
                        )
                    }

                    if !presentation.summaryRows.isEmpty {
                        SummaryGrid(rows: presentation.summaryRows.map { ($0.label, $0.value) })
                    }

                    ForEach(presentation.domains) { domain in
                        CheckupDomainView(domain: domain)
                    }

                    CheckupAdvancedOptionsView(store: store)

                    if let error = store.error {
                        ErrorRecoveryView(error: error, diagnosticsText: diagnosticsText) { action in
                            handleRecovery(action: action, error: error)
                        }
                    }
                }
                .frame(maxWidth: .infinity, alignment: .leading)
            }

            if let progress {
                BlockingProgressOverlay(progress: progress, allowsBackgroundInteraction: true)
            }
        }
        .frame(maxWidth: .infinity, maxHeight: .infinity, alignment: .topLeading)
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

private struct CheckupDomainView: View {
    let domain: CheckupDomainPresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                HStack(spacing: 6) {
                    Image(systemName: domain.status.systemImage)
                        .foregroundStyle(iconColor(for: domain.status))
                        .accessibilityLabel(domain.status.title)
                    Text(domain.title)
                }
                .font(.headline)
                Spacer()
                Text(domain.countSummary)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            ForEach(domain.rows) { row in
                HStack(alignment: .top, spacing: 8) {
                    Image(systemName: row.status.systemImage)
                        .foregroundStyle(iconColor(for: row.status))
                        .accessibilityLabel(row.status.title)
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

    private func iconColor(for status: CheckupStatusPresentation) -> Color {
        status == .passed ? .green : .primary
    }
}

private struct CheckupAdvancedOptionsView: View {
    @ObservedObject var store: DoctorStore

    var body: some View {
        DashboardDisclosureSection(title: L10n.string("checkup.advanced_options")) {
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                GridRow {
                    Toggle(L10n.string("checkup.option.skip_ssh"), isOn: $store.skipSSH)
                    Toggle(L10n.string("checkup.option.skip_bonjour"), isOn: $store.skipBonjour)
                }
                GridRow {
                    Toggle(L10n.string("checkup.option.skip_smb"), isOn: $store.skipSMB)
                    EmptyView()
                }
            }
        }
        .disabled(store.isRunning)
    }
}
