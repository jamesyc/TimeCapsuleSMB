import SwiftUI

struct MaintenanceView: View {
    @ObservedObject var store: MaintenanceStore
    @Binding var password: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("screen.maintenance"))
                .font(.title2.weight(.semibold))

            Picker("Maintenance", selection: $store.selectedWorkflow) {
                ForEach(MaintenanceWorkflow.allCases) { workflow in
                    Text(workflow.title).tag(workflow)
                }
            }
            .pickerStyle(.segmented)

            sharedOptions

            switch store.selectedWorkflow {
            case .activate:
                activatePanel
            case .uninstall:
                uninstallPanel
            case .fsck:
                fsckPanel
            case .repairXattrs:
                repairPanel
            }

            if let stage = store.currentStage {
                StageLine(stage: stage)
            }

            if let error = store.error {
                MaintenanceErrorView(error: error)
            }
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var sharedOptions: some View {
        HStack {
            TextField(L10n.string("field.mount_wait"), text: $store.mountWait)
                .frame(width: 150)
            Toggle(L10n.string("toggle.no_reboot"), isOn: $store.noReboot)
            Toggle(L10n.string("toggle.no_wait"), isOn: $store.noWait)
        }
    }

    private var activatePanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button {
                    store.planActivation(password: password)
                } label: {
                    Label("Plan Activation", systemImage: "doc.text.magnifyingglass")
                }
                .disabled(store.isRunning)

                Button {
                    store.runActivation(password: password)
                } label: {
                    Label(L10n.string("button.activate"), systemImage: "power")
                }
                .disabled(!store.canRunActivation)

                StatusLabel(state: store.activateState)
            }

            if let plan = store.activationPlan {
                Text("\(plan.actions.count) action(s), \(plan.postActivationChecks.count) post-check(s)")
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
            if let result = store.activationResult {
                Text(result.summary)
                    .font(.caption)
                if let message = result.message {
                    Text(message)
                        .font(.caption)
                        .foregroundStyle(.secondary)
                }
            }
        }
    }

    private var uninstallPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button {
                    store.planUninstall(password: password)
                } label: {
                    Label(L10n.string("button.uninstall_plan"), systemImage: "doc.text.magnifyingglass")
                }
                .disabled(store.isRunning || store.mountWaitValue == nil)

                Button {
                    store.runUninstall(password: password)
                } label: {
                    Label(L10n.string("button.uninstall"), systemImage: "xmark.bin.fill")
                }
                .disabled(!store.canRunUninstall)

                StatusLabel(state: store.uninstallState)
            }

            if let plan = store.uninstallPlan {
                Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
                    GridRow {
                        Text("Host").foregroundStyle(.secondary)
                        Text(plan.host)
                    }
                    GridRow {
                        Text("Reboot").foregroundStyle(.secondary)
                        Text(plan.requiresReboot ? "required" : "not required")
                    }
                    GridRow {
                        Text("Payload Dirs").foregroundStyle(.secondary)
                        Text(plan.payloadDirs.joined(separator: ", "))
                            .lineLimit(1)
                            .truncationMode(.middle)
                    }
                }
                .font(.caption)
            }
            if let result = store.uninstallResult {
                Text("\(result.summary) rebooted: \(yesNo(result.rebooted)), waited: \(yesNo(result.waited)), verified: \(yesNo(result.verified))")
                    .font(.caption)
            }
        }
    }

    private var fsckPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            HStack {
                Button {
                    store.refreshFsckTargets(password: password)
                } label: {
                    Label(L10n.string("button.list_fsck_volumes"), systemImage: "list.bullet.rectangle")
                }
                .disabled(store.isRunning || store.mountWaitValue == nil)

                Button {
                    store.planFsck(password: password)
                } label: {
                    Label(L10n.string("button.plan_fsck"), systemImage: "doc.text.magnifyingglass")
                }
                .disabled(!store.canPlanFsck)

                Button {
                    store.runFsck(password: password)
                } label: {
                    Label(L10n.string("button.run_fsck"), systemImage: "externaldrive.badge.checkmark")
                }
                .disabled(!store.canRunFsck)

                StatusLabel(state: store.fsckState)
            }

            if !store.fsckTargets.isEmpty {
                Picker("Volume", selection: $store.selectedFsckTargetID) {
                    Text("Select volume").tag(Optional<FsckTargetViewModel.ID>.none)
                    ForEach(store.fsckTargets) { target in
                        Text("\(target.device) on \(target.mountpoint)").tag(Optional(target.id))
                    }
                }
                .frame(maxWidth: 520)
            }
            if let plan = store.fsckPlan {
                Text("Plan: \(plan.device) on \(plan.mountpoint), reboot: \(yesNo(plan.rebootRequired)), wait: \(yesNo(plan.waitAfterReboot))")
                    .font(.caption)
            }
            if let result = store.fsckResult {
                Text("Result: \(result.device) return \(result.returncode.map(String.init) ?? "n/a"), waited: \(yesNo(result.waited)), verified: \(yesNo(result.verified))")
                    .font(.caption)
            }
        }
    }

    private var repairPanel: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField(L10n.string("field.repair_xattrs_path"), text: $store.repairPath)
            HStack {
                Button {
                    store.scanRepairXattrs()
                } label: {
                    Label(L10n.string("button.scan_xattrs"), systemImage: "wand.and.stars")
                }
                .disabled(store.isRunning || store.repairPath.trimmingCharacters(in: .whitespacesAndNewlines).isEmpty)

                Button {
                    store.runRepairXattrs()
                } label: {
                    Label(L10n.string("button.repair_xattrs"), systemImage: "wand.and.stars.inverse")
                }
                .disabled(!store.canRepairXattrs)

                StatusLabel(state: store.repairState)
            }

            if let scan = store.repairScan {
                Text("Scan: \(scan.findingCount) finding(s), \(scan.repairableCount) repairable.")
                    .font(.caption)
                if let report = scan.report, !report.isEmpty {
                    Text(report)
                        .font(.system(.caption, design: .monospaced))
                        .lineLimit(4)
                        .foregroundStyle(.secondary)
                }
            }
            if let result = store.repairResult {
                Text("Repair: \(result.summary)")
                    .font(.caption)
            }
        }
    }

    private func yesNo(_ value: Bool?) -> String {
        value == true ? "yes" : "no"
    }
}

private struct StatusLabel: View {
    let state: MaintenanceOperationState

    var body: some View {
        Label(state.title, systemImage: icon)
            .foregroundStyle(color)
    }

    private var icon: String {
        switch state {
        case .idle:
            return "circle"
        case .loading, .planning, .scanning, .running, .repairing:
            return "hourglass"
        case .listReady, .planReady, .scanReady, .succeeded, .repaired:
            return "checkmark.circle"
        case .planStale, .scanStale, .awaitingConfirmation:
            return "exclamationmark.circle"
        case .failed:
            return "exclamationmark.triangle"
        }
    }

    private var color: Color {
        switch state {
        case .listReady, .planReady, .scanReady, .succeeded, .repaired:
            return .green
        case .planStale, .scanStale, .awaitingConfirmation:
            return .orange
        case .failed:
            return .red
        default:
            return .secondary
        }
    }
}

private struct StageLine: View {
    let stage: OperationStageState

    var body: some View {
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
}

private struct MaintenanceErrorView: View {
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
