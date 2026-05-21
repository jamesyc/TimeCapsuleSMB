import AppKit
import SwiftUI

struct MaintenanceTab: View {
    let profile: DeviceProfile
    @ObservedObject var session: DeviceDashboardSession
    let showDiagnostics: () -> Void

    var body: some View {
        let store = session.maintenanceStore
        let presentation = MaintenanceWorkflowPresentation.presentation(for: store.selectedWorkflow)
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("dashboard.tab.maintenance"))
                .font(.title2.weight(.semibold))
            Picker(L10n.string("dashboard.tab.maintenance"), selection: $session.maintenanceStore.selectedWorkflow) {
                Text(L10n.string("maintenance.workflow.activate")).tag(MaintenanceWorkflow.activate)
                Text(L10n.string("maintenance.workflow.uninstall")).tag(MaintenanceWorkflow.uninstall)
                Text(L10n.string("maintenance.workflow.fsck")).tag(MaintenanceWorkflow.fsck)
                Text(L10n.string("maintenance.workflow.repair_xattrs")).tag(MaintenanceWorkflow.repairXattrs)
            }
            .pickerStyle(.segmented)

            VStack(alignment: .leading, spacing: 4) {
                Text(presentation.title)
                    .font(.headline)
                Text(presentation.subtitle)
                    .font(.caption)
                    .foregroundStyle(.secondary)
                Label(presentation.risk, systemImage: "exclamationmark.triangle")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }

            HStack {
                TextField(L10n.string("field.mount_wait"), text: $session.maintenanceStore.mountWait)
                    .frame(width: 150)
                Toggle(L10n.string("toggle.no_reboot"), isOn: $session.maintenanceStore.noReboot)
                Toggle(L10n.string("toggle.no_wait"), isOn: $session.maintenanceStore.noWait)
            }

            maintenanceControls(store: store)
            FlashBootHookSection(profile: profile)

            if let stage = store.currentStage {
                StageLine(stage: stage)
            }
            if let error = store.error {
                ErrorRecoveryView(error: error) { action in
                    handleRecovery(action: action, error: error)
                }
            }
        }
    }

    private func handleRecovery(action: RecoveryAction, error: BackendErrorViewModel) {
        if action.kind == .diagnostics {
            showDiagnostics()
            return
        }
        _ = session.handleRecoveryAction(action, error: error, profile: profile)
    }

    @ViewBuilder
    private func maintenanceControls(store: MaintenanceStore) -> some View {
        switch store.selectedWorkflow {
        case .activate:
            HStack {
                Button(L10n.string("maintenance.action.plan_start_smb")) {
                    if let password = session.maintenancePassword(for: profile) {
                        store.planActivation(password: password, profile: profile)
                    }
                }
                Button(L10n.string("maintenance.action.start_smb")) {
                    if let password = session.maintenancePassword(for: profile) {
                        store.runActivation(password: password, profile: profile)
                    }
                }
                .disabled(!store.canRunActivation)
                Label(store.activateState.title, systemImage: "circle")
            }
        case .uninstall:
            HStack {
                Button(L10n.string("maintenance.action.plan_uninstall")) {
                    if let password = session.maintenancePassword(for: profile) {
                        store.planUninstall(password: password, profile: profile)
                    }
                }
                Button(L10n.string("maintenance.action.uninstall")) {
                    if let password = session.maintenancePassword(for: profile) {
                        store.runUninstall(password: password, profile: profile)
                    }
                }
                .disabled(!store.canRunUninstall)
                Label(store.uninstallState.title, systemImage: "circle")
            }
        case .fsck:
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    Button(L10n.string("maintenance.action.find_volumes")) {
                        if let password = session.maintenancePassword(for: profile) {
                            store.refreshFsckTargets(password: password, profile: profile)
                        }
                    }
                    Button(L10n.string("maintenance.action.plan_disk_repair")) {
                        if let password = session.maintenancePassword(for: profile) {
                            store.planFsck(password: password, profile: profile)
                        }
                    }
                    .disabled(!store.canPlanFsck)
                    Button(L10n.string("maintenance.action.run_disk_repair")) {
                        if let password = session.maintenancePassword(for: profile) {
                            store.runFsck(password: password, profile: profile)
                        }
                    }
                    .disabled(!store.canRunFsck)
                    Label(store.fsckState.title, systemImage: "circle")
                }
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
        case .repairXattrs:
            VStack(alignment: .leading, spacing: 8) {
                HStack {
                    TextField(L10n.string("field.repair_xattrs_path"), text: $session.maintenanceStore.repairPath)
                    Button {
                        chooseRepairPath(store: store)
                    } label: {
                        Label(L10n.string("maintenance.action.choose_folder"), systemImage: "folder")
                    }
                }
                HStack {
                    Button(L10n.string("maintenance.action.scan_metadata")) {
                        store.scanRepairXattrs()
                    }
                    Button(L10n.string("maintenance.action.repair_metadata")) {
                        store.runRepairXattrs()
                    }
                    .disabled(!store.canRepairXattrs)
                    Label(store.repairState.title, systemImage: "circle")
                }
                if let scan = store.repairScan {
                    Text(L10n.format("maintenance.repairable_count", scan.repairableCount))
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private func chooseRepairPath(store: MaintenanceStore) {
        let panel = NSOpenPanel()
        panel.canChooseFiles = false
        panel.canChooseDirectories = true
        panel.allowsMultipleSelection = false
        panel.prompt = L10n.string("maintenance.action.choose")
        if panel.runModal() == .OK, let url = panel.url {
            store.repairPath = url.path
        }
    }
}
