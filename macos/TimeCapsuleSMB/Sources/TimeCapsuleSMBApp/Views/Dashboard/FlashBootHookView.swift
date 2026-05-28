import SwiftUI

struct FlashBootHookSection: View {
    let profile: DeviceProfile
    @ObservedObject var store: FlashWorkflowStore
    let performAction: (FlashUserAction) -> Void
    let chooseFirmwareTemplate: () -> Void

    var body: some View {
        let presentation = FlashPresentation(store: store)
        VStack(alignment: .leading, spacing: 12) {
            Divider()
            HStack(alignment: .firstTextBaseline) {
                VStack(alignment: .leading, spacing: 4) {
                    Text(presentation.title)
                        .font(.headline)
                    AnimatedProgressText(message: presentation.message, isRunning: store.isRunning)
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

            if !presentation.warnings.isEmpty {
                VStack(alignment: .leading, spacing: 4) {
                    ForEach(presentation.warnings, id: \.self) { warning in
                        Label(warning, systemImage: "exclamationmark.triangle")
                            .font(.caption)
                            .foregroundStyle(.orange)
                    }
                }
            }

            DisclosureGroup {
                FlashFirmwareOptionsView(store: store, chooseFirmwareTemplate: chooseFirmwareTemplate)
            } label: {
                Label(L10n.string("flash.options.apple_firmware"), systemImage: "gearshape")
                    .font(.subheadline.weight(.medium))
            }

            HStack {
                ForEach(presentation.primaryActions) { action in
                    Button {
                        performAction(action)
                    } label: {
                        Label(presentation.title(for: action), systemImage: action.systemImage)
                    }
                    .disabled(!presentation.isEnabled(action))
                }
            }

            HStack {
                ForEach(presentation.secondaryActions) { action in
                    Button {
                        performAction(action)
                    } label: {
                        Label(presentation.title(for: action), systemImage: action.systemImage)
                    }
                    .disabled(!presentation.isEnabled(action))
                }
            }

            if !presentation.rows.isEmpty {
                VStack(alignment: .leading, spacing: 6) {
                    ForEach(presentation.rows) { row in
                        HStack(alignment: .firstTextBaseline) {
                            Text(row.label)
                                .foregroundStyle(.secondary)
                            Spacer()
                            Text(row.value)
                                .multilineTextAlignment(.trailing)
                                .textSelection(.enabled)
                        }
                        .font(.caption)
                    }
                }
            }

            if let timeline = FlashTimelinePresentation(events: store.events, currentStage: store.currentStage),
               !timeline.items.isEmpty {
                MaintenanceTimelineView(presentation: MaintenanceTimelinePresentation(items: timeline.items))
            }
        }
        .onAppear {
            store.refresh(profile: profile)
        }
        .onChange(of: profile.id) { _, _ in
            store.refresh(profile: profile)
        }
    }
}

private struct FlashFirmwareOptionsView: View {
    @ObservedObject var store: FlashWorkflowStore
    let chooseFirmwareTemplate: () -> Void

    var body: some View {
        VStack(alignment: .leading, spacing: 8) {
            TextField(L10n.string("field.firmware_version"), text: $store.firmwareVersion)
            HStack {
                TextField(L10n.string("field.firmware_template"), text: $store.firmwareTemplatePath)
                Button {
                    chooseFirmwareTemplate()
                } label: {
                    Label(L10n.string("flash.action.choose_template"), systemImage: "doc")
                }
            }
        }
        .textFieldStyle(.roundedBorder)
    }
}

private struct FlashTimelinePresentation: Equatable {
    let items: [OperationTimelineItem]

    init?(events: [BackendEvent], currentStage: OperationStageState?) {
        var items = OperationTimelineBuilder.timeline(from: events)
            .filter { $0.operation == "flash" }
        if items.isEmpty, let currentStage, currentStage.operation == "flash" {
            items = [
                OperationTimelineItem(
                    id: "current:\(currentStage.operation):\(currentStage.stage)",
                    operation: currentStage.operation,
                    title: OperationTimelineBuilder.stageTitle(for: currentStage.operation, stage: currentStage.stage),
                    detail: OperationTimelineBuilder.stageDetail(
                        for: currentStage.operation,
                        stage: currentStage.stage,
                        fallback: nil
                    ),
                    state: .running,
                    risk: currentStage.risk,
                    cancellable: currentStage.cancellable
                )
            ]
        }
        guard !items.isEmpty else {
            return nil
        }
        self.items = items
    }
}
