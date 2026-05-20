import SwiftUI

struct ReadinessView: View {
    @ObservedObject var store: ReadinessStore
    @Binding var helperPath: String

    var body: some View {
        VStack(alignment: .leading, spacing: 12) {
            Text(L10n.string("screen.readiness"))
                .font(.title2.weight(.semibold))

            TextField(L10n.string("field.helper"), text: $helperPath)

            HStack {
                readinessButton(
                    L10n.string("button.capabilities"),
                    icon: "info.circle",
                    state: store.capabilitiesState,
                    action: store.runCapabilities
                )
                readinessButton(
                    L10n.string("button.paths"),
                    icon: "folder",
                    state: store.pathsState,
                    action: store.runPaths
                )
                readinessButton(
                    L10n.string("button.validate"),
                    icon: "checkmark.seal",
                    state: store.validationState,
                    action: store.runValidateInstall
                )
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

            if let capabilities = store.capabilities {
                CapabilitiesSummaryView(payload: capabilities)
            }

            if let paths = store.paths {
                PathsSummaryView(payload: paths)
            }

            if let validation = store.validation {
                ValidationSummaryView(payload: validation)
            }

            if let error = store.error {
                ReadinessErrorView(error: error)
            }
        }
        .padding()
        .frame(maxWidth: .infinity, alignment: .leading)
    }

    private func readinessButton(
        _ title: String,
        icon: String,
        state: ReadinessOperationState,
        action: @escaping () -> Void
    ) -> some View {
        Button(action: action) {
            Label("\(title) (\(state.title))", systemImage: icon)
        }
        .disabled(store.isRunning)
    }
}

private struct CapabilitiesSummaryView: View {
    let payload: CapabilitiesPayload

    var body: some View {
        Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
            GridRow {
                Text("Helper").foregroundStyle(.secondary)
                Text("\(payload.helperVersion) (\(payload.helperVersionCode))")
            }
            GridRow {
                Text("API Schema").foregroundStyle(.secondary)
                Text(String(payload.apiSchemaVersion))
            }
            GridRow {
                Text("Confirmations").foregroundStyle(.secondary)
                Text(String(payload.confirmationSchemaVersion))
            }
            GridRow {
                Text("Operations").foregroundStyle(.secondary)
                Text(payload.operations.joined(separator: ", "))
                    .lineLimit(2)
            }
        }
        .font(.caption)
    }
}

private struct PathsSummaryView: View {
    let payload: PathsPayload

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Grid(alignment: .leading, horizontalSpacing: 12, verticalSpacing: 4) {
                GridRow {
                    Text("Distribution").foregroundStyle(.secondary)
                    Text(payload.distributionRoot).lineLimit(1).truncationMode(.middle)
                }
                GridRow {
                    Text("Config").foregroundStyle(.secondary)
                    Text(payload.configPath).lineLimit(1).truncationMode(.middle)
                }
                GridRow {
                    Text("State").foregroundStyle(.secondary)
                    Text(payload.stateDir).lineLimit(1).truncationMode(.middle)
                }
            }
            if !payload.artifacts.isEmpty {
                Text("Artifacts")
                    .font(.body.weight(.medium))
                ForEach(payload.artifacts, id: \.name) { artifact in
                    HStack {
                        Image(systemName: artifact.ok ? "checkmark.circle" : "xmark.circle")
                            .foregroundStyle(artifact.ok ? .green : .red)
                        Text(artifact.name)
                        Text(artifact.repoRelativePath)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                            .truncationMode(.middle)
                        Spacer()
                        Text(artifact.message)
                            .foregroundStyle(.secondary)
                            .lineLimit(1)
                    }
                    .font(.caption)
                }
            }
        }
        .font(.caption)
    }
}

private struct ValidationSummaryView: View {
    let payload: InstallValidationPayload

    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            HStack {
                Image(systemName: payload.ok ? "checkmark.seal" : "xmark.seal")
                    .foregroundStyle(payload.ok ? .green : .red)
                Text(payload.summary)
                Text("\(payload.counts["pass"] ?? 0) passed, \(payload.counts["fail"] ?? 0) failed")
                    .foregroundStyle(.secondary)
            }
            ForEach(payload.checks, id: \.id) { check in
                HStack {
                    Image(systemName: check.ok ? "checkmark.circle" : "xmark.circle")
                        .foregroundStyle(check.ok ? .green : .red)
                    Text(check.id)
                    Text(check.message)
                        .foregroundStyle(.secondary)
                        .lineLimit(1)
                }
                .font(.caption)
            }
        }
        .font(.caption)
    }
}

private struct ReadinessErrorView: View {
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
