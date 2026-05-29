import SwiftUI

struct InstallTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let appSettings: AppSettings
    let showDiagnostics: () -> Void
    let diagnosticsText: () -> String

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
            hostWarning: HostCompatibilityPolicy.warning(enabled: appSettings.timeMachineWarningsEnabled),
            isCheckupRunning: summary.displayStatus == .checking
        )
        let progress = InstallProgressPresentation(state: store.state, currentStage: store.currentStage)
        let isDeviceBusy = session.appStore.operationCoordinator.isDeviceBusy(profile)

        ZStack {
            ScrollView {
                VStack(alignment: .leading, spacing: 14) {
                    InstallHeaderView(presentation: presentation)

                    ForEach(presentation.notices, id: \.self) { notice in
                        Label(notice, systemImage: "exclamationmark.triangle")
                            .font(.caption)
                            .foregroundStyle(.yellow)
                    }

                    if !presentation.actions.isEmpty {
                        HStack {
                            ForEach(presentation.actions) { action in
                                InstallActionButton(action: action) {
                                    session.performInstallAction(action, profile: profile, showDiagnostics: showDiagnostics)
                                }
                                .disabled(isDisabled(action, store: store, isDeviceBusy: isDeviceBusy))
                            }
                        }
                    }

                    if let timeline = presentation.timeline {
                        InstallTimelineView(presentation: timeline)
                    }

                    if let error = presentation.error {
                        ErrorRecoveryView(
                            error: error,
                            guidance: presentation.failureGuidance,
                            diagnosticsText: diagnosticsText
                        ) { action in
                            handleRecovery(action: action, error: error)
                        }
                    }

                    if let plan = presentation.plan {
                        InstallPlanView(presentation: plan)
                    }

                    if let completion = presentation.completion {
                        InstallCompletionView(
                            presentation: completion,
                            isDisabled: { isDisabled($0, store: store, isDeviceBusy: isDeviceBusy) }
                        ) { action in
                            session.performInstallAction(action, profile: profile, showDiagnostics: showDiagnostics)
                        }
                    }

                    InstallExecutionOptionsView(store: store, isDeviceBusy: isDeviceBusy)
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

    private func isDisabled(_ action: InstallUserAction, store: DeployWorkflowStore, isDeviceBusy: Bool) -> Bool {
        !InstallActionAvailabilityPolicy.isEnabled(action, store: store, isDeviceBusy: isDeviceBusy)
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
        if action == .installUpdate {
            Button(action: perform) {
                Label(action.title, systemImage: action.systemImage)
            }
            .buttonStyle(.borderedProminent)
        } else {
            Button(action: perform) {
                Label(action.title, systemImage: action.systemImage)
            }
            .buttonStyle(.bordered)
        }
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
        OperationTimelineListView(
            title: L10n.string("install.timeline.title"),
            emptyMessage: L10n.string("install.timeline.waiting"),
            items: presentation.items
        )
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
    let isDeviceBusy: Bool

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
        .disabled(store.isBusy || isDeviceBusy)
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
