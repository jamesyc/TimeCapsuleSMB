import AppKit
import SwiftUI

struct MaintenanceTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let showDiagnostics: () -> Void
    let diagnosticsText: () -> String

    var body: some View {
        let store = session.maintenanceStore
        let presentation = MaintenanceDashboardPresentation(store: store, profile: profile)

        ScrollView {
            VStack(alignment: .leading, spacing: 14) {
                Text(L10n.string("dashboard.tab.maintenance"))
                    .font(.title2.weight(.semibold))

                MaintenanceWorkflowCardsView(cards: presentation.cards) { workflow in
                    session.maintenanceStore.selectedWorkflow = workflow
                }

                MaintenanceDetailView(
                    presentation: presentation.detail,
                    store: store,
                    performAction: { action in
                        session.performMaintenanceAction(action, profile: profile, showDiagnostics: showDiagnostics)
                    },
                    chooseRepairPath: {
                        chooseRepairPath(store: store)
                    }
                )

                if FlashBootHookVisibilityPolicy.isVisible(for: profile) {
                    FlashBootHookSection(
                        profile: profile,
                        store: session.flashStore,
                        performAction: { action in
                            session.performFlashAction(action, profile: profile)
                        },
                        chooseFirmwareTemplate: {
                            chooseFirmwareTemplate(store: session.flashStore)
                        }
                    )
                }

                if let error = store.error(for: presentation.detail.workflow) {
                    ErrorRecoveryView(error: error, diagnosticsText: diagnosticsText) { action in
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

    private func chooseRepairPath(store: MaintenanceStore) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = L10n.string("maintenance.action.choose")
        panel.begin { response in
            guard response == .OK, let url = panel.url else {
                return
            }
            Task { @MainActor in
                store.repairPath = url.path
            }
        }
    }

    private func chooseFirmwareTemplate(store: FlashWorkflowStore) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = true
        panel.canChooseDirectories = false
        panel.allowsMultipleSelection = false
        panel.prompt = L10n.string("maintenance.action.choose")
        panel.begin { response in
            guard response == .OK, let url = panel.url else {
                return
            }
            Task { @MainActor in
                store.firmwareTemplatePath = url.path
            }
        }
    }
}

private struct MaintenanceWorkflowCardsView: View {
    let cards: [MaintenanceWorkflowCardPresentation]
    let select: (MaintenanceWorkflow) -> Void

    var body: some View {
        LazyVGrid(columns: [GridItem(.adaptive(minimum: 190), spacing: 10)], alignment: .leading, spacing: 10) {
            ForEach(cards) { card in
                Button {
                    select(card.workflow)
                } label: {
                    VStack(alignment: .leading, spacing: 6) {
                        HStack {
                            Text(card.title)
                                .font(.headline)
                            Spacer()
                            Image(systemName: card.isSelected ? "checkmark.circle.fill" : "circle")
                        }
                        Text(card.subtitle)
                            .font(.caption)
                            .foregroundStyle(.secondary)
                            .lineLimit(2)
                        Text(card.stateTitle)
                            .font(.caption.weight(.medium))
                            .foregroundStyle(.secondary)
                    }
                    .frame(maxWidth: .infinity, minHeight: 92, alignment: .topLeading)
                    .padding(10)
                    .background(card.isSelected ? Color.accentColor.opacity(0.14) : Color.secondary.opacity(0.08))
                    .clipShape(RoundedRectangle(cornerRadius: 6))
                }
                .buttonStyle(.plain)
            }
        }
    }
}

private struct MaintenanceDetailView: View {
    let presentation: MaintenanceWorkflowDetailPresentation
    @ObservedObject var store: MaintenanceStore
    let performAction: (MaintenanceUserAction) -> Void
    let chooseRepairPath: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(presentation.title)
                        .font(.headline)
                    Text(presentation.subtitle)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
                Spacer()
                Text(presentation.stateTitle)
                    .font(.caption.weight(.medium))
                    .padding(.horizontal, 8)
                    .padding(.vertical, 5)
                    .background(.quaternary)
                    .clipShape(Capsule())
            }

            Label(presentation.risk, systemImage: "exclamationmark.triangle")
                .font(.caption)
                .foregroundStyle(.secondary)
            Text(presentation.statusMessage)
                .font(.callout)
                .foregroundStyle(.secondary)

            if presentation.workflow == .repairXattrs {
                RepairPathPicker(store: store, chooseRepairPath: chooseRepairPath)
            }

            if presentation.workflow == .fsck {
                FsckTargetListView(store: store)
            }

            HStack {
                ForEach(presentation.actions) { action in
                    MaintenanceActionButton(
                        action: action,
                        isEnabled: presentation.isEnabled(action),
                        perform: performAction
                    )
                }
            }

            if let timeline = presentation.timeline, !timeline.items.isEmpty {
                MaintenanceTimelineView(presentation: timeline)
            }

            if let plan = presentation.plan {
                MaintenancePlanView(presentation: plan)
            }

            if let completion = presentation.completion {
                MaintenanceCompletionView(presentation: completion)
            }

            MaintenanceAdvancedOptionsView(workflow: presentation.workflow, store: store)
        }
        .padding(10)
        .background(Color.secondary.opacity(0.06))
        .clipShape(RoundedRectangle(cornerRadius: 6))
    }

}

private struct MaintenanceActionButton: View {
    let action: MaintenanceUserAction
    let isEnabled: Bool
    let perform: (MaintenanceUserAction) -> Void

    var body: some View {
        if action.isCommitAction {
            Button {
                perform(action)
            } label: {
                Label(action.title, systemImage: action.systemImage)
            }
            .buttonStyle(.borderedProminent)
            .disabled(!isEnabled)
        } else {
            Button {
                perform(action)
            } label: {
                Label(action.title, systemImage: action.systemImage)
            }
            .buttonStyle(.bordered)
            .disabled(!isEnabled)
        }
    }
}

private struct RepairPathPicker: View {
    @ObservedObject var store: MaintenanceStore
    let chooseRepairPath: () -> Void

    var body: some View {
        HStack {
            TextField(L10n.string("field.repair_xattrs_path"), text: $store.repairPath)
            Button {
                chooseRepairPath()
            } label: {
                Label(L10n.string("maintenance.action.choose_folder"), systemImage: "folder")
            }
        }
    }
}

private struct FsckTargetListView: View {
    @ObservedObject var store: MaintenanceStore

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            if store.fsckTargets.isEmpty {
                Text(L10n.string("maintenance.fsck.no_volumes"))
                    .font(.caption)
                    .foregroundStyle(.secondary)
            } else {
                ForEach(store.fsckTargets) { target in
                    Button {
                        store.selectedFsckTargetID = target.id
                    } label: {
                        HStack {
                            Image(systemName: store.selectedFsckTargetID == target.id ? "checkmark.circle.fill" : "circle")
                            Text(target.name ?? target.device)
                            Text(target.mountpoint).foregroundStyle(.secondary)
                        }
                    }
                    .buttonStyle(.plain)
                }
            }
        }
    }
}

private struct MaintenancePlanView: View {
    let presentation: MaintenancePlanPresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(presentation.title)
                .font(.headline)
            SummaryGrid(rows: presentation.rows.map { ($0.label, $0.value) })
            ForEach(presentation.warnings, id: \.self) { warning in
                Label(warning, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.yellow)
            }
        }
    }
}

private struct MaintenanceCompletionView: View {
    let presentation: MaintenanceCompletionPresentation

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            Text(presentation.title)
                .font(.headline)
            SummaryGrid(rows: presentation.rows.map { ($0.label, $0.value) })
        }
    }
}

struct MaintenanceTimelineView: View {
    let presentation: MaintenanceTimelinePresentation

    var body: some View {
        OperationTimelineListView(
            title: L10n.string("maintenance.timeline.title"),
            items: presentation.items
        )
    }
}

private struct MaintenanceAdvancedOptionsView: View {
    let workflow: MaintenanceWorkflow
    @ObservedObject var store: MaintenanceStore

    var body: some View {
        DashboardDisclosureSection(title: L10n.string("maintenance.advanced_options")) {
            if workflow == .repairXattrs {
                RepairXattrsAdvancedOptionsView(store: store)
            } else if workflow == .sshAccess {
                SSHAccessAdvancedOptionsView(store: store)
            } else {
                RemoteMaintenanceAdvancedOptionsView(store: store)
            }
        }
    }
}

private struct SSHAccessAdvancedOptionsView: View {
    @ObservedObject var store: MaintenanceStore

    var body: some View {
        Toggle(L10n.string("toggle.no_wait"), isOn: $store.noWait)
    }
}

private struct RemoteMaintenanceAdvancedOptionsView: View {
    @ObservedObject var store: MaintenanceStore

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
            GridRow {
                Text(L10n.string("field.mount_wait"))
                    .foregroundStyle(.secondary)
                TextField(L10n.string("field.mount_wait"), text: $store.mountWait)
                    .frame(width: 150)
            }
            GridRow {
                Toggle(L10n.string("toggle.no_reboot"), isOn: $store.noReboot)
                Toggle(L10n.string("toggle.no_wait"), isOn: $store.noWait)
            }
        }
    }
}

private struct RepairXattrsAdvancedOptionsView: View {
    @ObservedObject var store: MaintenanceStore

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 8) {
            GridRow {
                Toggle(L10n.string("toggle.repair_xattrs_recursive"), isOn: $store.repairRecursive)
                Toggle(L10n.string("toggle.repair_xattrs_include_hidden"), isOn: $store.repairIncludeHidden)
            }
            GridRow {
                Toggle(L10n.string("toggle.repair_xattrs_include_time_machine"), isOn: $store.repairIncludeTimeMachine)
                Toggle(L10n.string("toggle.repair_xattrs_fix_permissions"), isOn: $store.repairFixPermissions)
            }
            GridRow {
                Toggle(L10n.string("toggle.repair_xattrs_verbose"), isOn: $store.repairVerbose)
                HStack {
                    Text(L10n.string("field.repair_xattrs_max_depth"))
                        .foregroundStyle(.secondary)
                    TextField(L10n.string("field.repair_xattrs_max_depth"), text: $store.repairMaxDepth)
                        .frame(width: 80)
                }
            }
        }
    }
}
