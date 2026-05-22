import SwiftUI

struct InstallTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let showDiagnostics: () -> Void

    var body: some View {
        let store = session.deployStore
        let summary = session.summary(for: profile)
        let presentation = InstallWorkflowPresentation(
            state: store.state,
            plan: store.plan,
            result: store.result,
            error: store.error,
            events: store.events,
            currentStage: store.currentStage,
            plannedOptions: store.plannedOptions,
            profile: profile,
            hostWarning: HostCompatibilityPolicy.warning(),
            isCheckupRunning: summary.displayStatus == .checking
        )
        let progress = InstallProgressPresentation(state: store.state, currentStage: store.currentStage)

        ZStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    InstallHeaderView(presentation: presentation)

                    ForEach(presentation.notices, id: \.self) { notice in
                        Label(notice, systemImage: "exclamationmark.triangle")
                            .font(.caption)
                            .foregroundStyle(.yellow)
                    }

                    if let action = presentation.primaryAction {
                        InstallActionButton(action: action) {
                            session.performInstallAction(action, profile: profile, showDiagnostics: showDiagnostics)
                        }
                        .disabled(isDisabled(action, store: store))
                    }

                    if let timeline = presentation.timeline {
                        InstallTimelineView(presentation: timeline)
                    }

                    if let plan = presentation.plan {
                        InstallPlanView(presentation: plan)
                    }

                    if let completion = presentation.completion {
                        InstallCompletionView(
                            presentation: completion,
                            isDisabled: { isDisabled($0, store: store) }
                        ) { action in
                            session.performInstallAction(action, profile: profile, showDiagnostics: showDiagnostics)
                        }
                    }

                    InstallExecutionOptionsView(store: store)

                    if let error = store.error {
                        ErrorRecoveryView(error: error) { action in
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

    private func isDisabled(_ action: InstallUserAction, store: DeployWorkflowStore) -> Bool {
        !InstallActionAvailabilityPolicy.isEnabled(action, store: store)
    }
}

private struct InstallHeaderView: View {
    let presentation: InstallWorkflowPresentation

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
            Text(presentation.statusMessage)
                .font(.callout)
                .foregroundStyle(.secondary)
        }
    }
}

private struct InstallActionButton: View {
    let action: InstallUserAction
    let perform: () -> Void

    var body: some View {
        Button(action: perform) {
            Label(action.title, systemImage: action.systemImage)
        }
        .buttonStyle(.borderedProminent)
    }
}

private struct InstallPlanView: View {
    let presentation: InstallPlanPresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(presentation.title)
                .font(.headline)

            ForEach(presentation.sections) { section in
                VStack(alignment: .leading, spacing: 6) {
                    Text(section.title)
                        .font(.subheadline.weight(.medium))
                    SummaryGrid(rows: section.rows.map { ($0.label, $0.value) })
                }
            }

            ForEach(presentation.warnings, id: \.self) { warning in
                Label(warning, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.yellow)
            }
        }
    }
}

private struct InstallTimelineView: View {
    let presentation: InstallTimelinePresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(L10n.string("install.timeline.title"))
                .font(.headline)
            if presentation.items.isEmpty {
                Text(L10n.string("install.timeline.waiting"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(presentation.items) { item in
                    HStack(alignment: .top, spacing: 8) {
                        OperationTimelineStateIcon(state: item.state)
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
    }
}

private struct InstallCompletionView: View {
    let presentation: InstallCompletionPresentation
    let isDisabled: (InstallUserAction) -> Bool
    let performAction: (InstallUserAction) -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 10) {
            Text(presentation.title)
                .font(.headline)
            SummaryGrid(rows: presentation.rows.map { ($0.label, $0.value) })
            ForEach(presentation.warnings, id: \.self) { warning in
                Label(warning, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.yellow)
            }
            HStack {
                ForEach(presentation.actions) { action in
                    Button {
                        performAction(action)
                    } label: {
                        Label(action.title, systemImage: action.systemImage)
                    }
                    .disabled(isDisabled(action))
                }
            }
        }
    }
}

private struct InstallExecutionOptionsView: View {
    @ObservedObject var store: DeployWorkflowStore

    var body: some View {
        DashboardDisclosureSection(title: L10n.string("install.advanced_options")) {
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                GridRow {
                    Toggle(L10n.string("toggle.no_reboot"), isOn: $store.noReboot)
                        .disabled(!allowsNoReboot)
                    Toggle(L10n.string("toggle.no_wait"), isOn: noWaitBinding)
                        .disabled(!allowsNoWait)
                }
                GridRow {
                    Text(L10n.string("install.advanced_options.no_wait_note"))
                        .font(.caption)
                        .foregroundStyle(.secondary)
                        .fixedSize(horizontal: false, vertical: true)
                        .gridCellColumns(2)
                }
                GridRow {
                    Text(L10n.string("field.mount_wait"))
                        .foregroundStyle(.secondary)
                    TextField(L10n.string("field.mount_wait"), text: $store.mountWait)
                        .frame(width: 150)
                }
            }
        }
        .disabled(store.isBusy)
    }

    private var allowsNoReboot: Bool {
        DeployExecutionOptionPolicy.allowsNoReboot(noWait: store.noWait)
    }

    private var allowsNoWait: Bool {
        DeployExecutionOptionPolicy.allowsNoWait(noReboot: store.noReboot)
    }

    private var noWaitBinding: Binding<Bool> {
        Binding {
            allowsNoWait ? store.noWait : false
        } set: { value in
            if allowsNoWait {
                store.noWait = value
            } else {
                store.noWait = false
            }
        }
    }
}
