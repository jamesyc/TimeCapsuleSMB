import SwiftUI

struct DeployView: View {
    @ObservedObject var store: DeployWorkflowStore
    @Binding var password: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("screen.deploy"))
                .font(.title2.weight(.semibold))

            HStack {
                Toggle(L10n.string("toggle.enable_nbns"), isOn: $store.nbnsEnabled)
                Toggle(L10n.string("toggle.no_reboot"), isOn: $store.noReboot)
                Toggle(L10n.string("toggle.no_wait"), isOn: $store.noWait)
                Toggle(L10n.string("toggle.force_debug_logging"), isOn: $store.debugLogging)
                TextField(L10n.string("field.mount_wait"), text: $store.mountWait)
                    .frame(width: 150)
            }

            HStack {
                Button {
                    store.runPlan(password: password)
                } label: {
                    Label(L10n.string("button.plan_deploy"), systemImage: "doc.text.magnifyingglass")
                }
                .disabled(store.isRunning || store.mountWaitValue == nil)

                Button {
                    store.runDeploy(password: password)
                } label: {
                    Label(L10n.string("button.deploy"), systemImage: "square.and.arrow.up")
                }
                .disabled(!store.canDeploy)

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

            if let plan = store.plan {
                DeployPlanSummaryView(plan: plan, stale: store.state == .planStale)
            }

            if let result = store.result {
                DeployResultSummaryView(result: result)
            }

            if let error = store.error {
                DeployErrorView(error: error)
            }
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private var statusIcon: String {
        switch store.state {
        case .idle:
            return "circle"
        case .planning, .deploying:
            return "hourglass"
        case .planReady, .deployed:
            return "checkmark.circle"
        case .planStale, .awaitingConfirmation:
            return "exclamationmark.circle"
        case .planFailed, .deployFailed:
            return "exclamationmark.triangle"
        }
    }

    private var statusColor: Color {
        switch store.state {
        case .planReady, .deployed:
            return .green
        case .planStale, .awaitingConfirmation:
            return .orange
        case .planFailed, .deployFailed:
            return .red
        default:
            return .secondary
        }
    }
}

private struct DeployPlanSummaryView: View {
    let plan: DeployPlanPayload
    let stale: Bool

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text(stale ? "Deploy Plan Stale" : "Deploy Plan")
                .font(.body.weight(.medium))
                .foregroundStyle(stale ? .orange : .primary)
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
                GridRow {
                    Text("Host").foregroundStyle(.secondary)
                    Text(plan.host)
                }
                GridRow {
                    Text("Payload").foregroundStyle(.secondary)
                    Text(plan.payloadFamily ?? "unknown")
                }
                GridRow {
                    Text("NetBSD4").foregroundStyle(.secondary)
                    Text(plan.netbsd4 ? "yes" : "no")
                }
                GridRow {
                    Text("Reboot").foregroundStyle(.secondary)
                    Text(plan.requiresReboot ? "required" : "not required")
                }
                GridRow {
                    Text("Payload Dir").foregroundStyle(.secondary)
                    Text(plan.payloadDir)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                GridRow {
                    Text("Actions").foregroundStyle(.secondary)
                    Text("\(plan.preUploadActions.count) pre, \(plan.uploads.count) uploads, \(plan.postUploadActions.count) post, \(plan.activationActions.count) activation")
                }
            }
            if !plan.postDeployChecks.isEmpty {
                Text("Post-deploy checks: \(plan.postDeployChecks.map(\.description).joined(separator: ", "))")
                    .font(.caption)
                    .foregroundStyle(.secondary)
                    .lineLimit(2)
            }
        }
        .font(.caption)
    }
}

private struct DeployResultSummaryView: View {
    let result: DeployResultPayload

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("Deploy Result")
                .font(.body.weight(.medium))
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
                GridRow {
                    Text("Payload Dir").foregroundStyle(.secondary)
                    Text(result.payloadDir)
                        .lineLimit(1)
                        .truncationMode(.middle)
                }
                GridRow {
                    Text("Reboot Requested").foregroundStyle(.secondary)
                    Text(result.rebootRequested == true ? "yes" : "no")
                }
                GridRow {
                    Text("Waited").foregroundStyle(.secondary)
                    Text(result.waited == true ? "yes" : "no")
                }
                GridRow {
                    Text("Verified").foregroundStyle(.secondary)
                    Text(result.verified == true ? "yes" : "no")
                }
            }
            if let message = result.message {
                Text(message)
                    .font(.caption)
                    .foregroundStyle(.secondary)
            }
        }
        .font(.caption)
    }
}

private struct DeployErrorView: View {
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
