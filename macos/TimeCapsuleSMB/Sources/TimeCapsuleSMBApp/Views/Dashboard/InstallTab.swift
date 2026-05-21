import SwiftUI

struct InstallTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let showDiagnostics: () -> Void

    var body: some View {
        let store = session.deployStore
        let presentation = InstallWorkflowPresentation(
            state: store.state,
            plan: store.plan,
            result: store.result,
            error: store.error,
            events: store.events,
            currentStage: store.currentStage,
            profile: profile,
            hostWarning: HostCompatibilityPolicy.warning()
        )

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
                    InstallCompletionView(presentation: completion) { action in
                        session.performInstallAction(action, profile: profile, showDiagnostics: showDiagnostics)
                    }
                }

                InstallAdvancedOptionsView(store: store)

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

    private func isDisabled(_ action: InstallUserAction, store: DeployWorkflowStore) -> Bool {
        switch action {
        case .createPlan, .regeneratePlan:
            return store.isRunning || store.mountWaitValue == nil
        case .installUpdate:
            return !store.canDeploy
        case .openFinder, .runCheckup, .viewDiagnostics:
            return false
        }
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

private struct InstallCompletionView: View {
    let presentation: InstallCompletionPresentation
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
                }
            }
        }
    }
}

private struct InstallAdvancedOptionsView: View {
    @ObservedObject var store: DeployWorkflowStore

    var body: some View {
        DisclosureGroup(L10n.string("install.advanced_options")) {
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
                GridRow {
                    Toggle(L10n.string("toggle.enable_nbns"), isOn: $store.nbnsEnabled)
                    Toggle(L10n.string("toggle.force_debug_logging"), isOn: $store.debugLogging)
                }
                GridRow {
                    Toggle(L10n.string("toggle.no_reboot"), isOn: $store.noReboot)
                    Toggle(L10n.string("toggle.no_wait"), isOn: $store.noWait)
                }
                GridRow {
                    Text(L10n.string("field.mount_wait"))
                        .foregroundStyle(.secondary)
                    TextField(L10n.string("field.mount_wait"), text: $store.mountWait)
                        .frame(width: 150)
                }
            }
            .padding(.top, 8)
        }
    }
}
